"""Accounts, Italian tax ("regime amministrato") and the single state-mutation path.

Every change to an account happens in `apply_event`, driven by a ledger entry
that already contains the final numbers. Replaying the ledger therefore
rebuilds exactly the same state and never recomputes history.

Tax model (conservative simplifications are listed in the README):
- 26% on each realised gain, withheld at the moment of the sale.
- Realised losses become a credit usable for the 4 following calendar years.
- Gains on ETFs are "redditi di capitale": taxed in full, they can NOT be
  offset by earlier losses. ETF losses still create a credit.
- Cost basis is the weighted average cost, commissions included.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_UP, Decimal
from typing import Any

from .models import CENT, BrokerProfile, D, Order, d


@dataclass
class Position:
    qty: int = 0
    basis: Decimal = D(0)          # total cost of the shares held, commissions included

    @property
    def avg_cost(self) -> Decimal:
        return self.basis / self.qty if self.qty else D(0)


@dataclass
class Account:
    strategy_id: str
    cash: Decimal = D(0)
    positions: dict[str, Position] = field(default_factory=dict)
    losses: list[list] = field(default_factory=list)      # [[amount, expiry_year], ...] oldest first
    deposits: Decimal = D(0)
    commissions: Decimal = D(0)
    taxes: Decimal = D(0)
    token_cost: Decimal = D(0)
    stamp_duty: Decimal = D(0)
    realized_gain: Decimal = D(0)   # before tax, after commissions
    fills: int = 0
    orders: int = 0
    closed_trades: int = 0
    winning_trades: int = 0
    traded_value: Decimal = D(0)

    def qty(self, symbol: str) -> int:
        p = self.positions.get(symbol)
        return p.qty if p else 0

    def loss_credit(self, year: int) -> Decimal:
        return sum((d(a) for a, exp in self.losses if exp >= year), D(0))

    @property
    def costs(self) -> Decimal:
        return self.commissions + self.taxes + self.token_cost + self.stamp_duty


def tax_on_sale(acct: Account, gain: Decimal, is_etf: bool, year: int, profile: BrokerProfile) -> dict[str, Any]:
    """Pure: tax due and loss-credit movements for one realised gain/loss."""
    out = {"tax": D(0), "loss_used": D(0), "loss_created": D(0), "loss_expiry": None}
    if gain < 0:
        out["loss_created"] = -gain
        out["loss_expiry"] = year + profile.loss_carry_forward_years
        return out
    taxable = gain
    if not is_etf:
        out["loss_used"] = min(gain, acct.loss_credit(year))
        taxable = gain - out["loss_used"]
    out["tax"] = (taxable * profile.capital_gains_tax_rate).quantize(CENT, rounding=ROUND_UP)
    return out


def _consume_losses(acct: Account, amount: Decimal, year: int) -> None:
    acct.losses = [l for l in acct.losses if l[1] >= year]
    for l in acct.losses:
        if amount <= 0:
            break
        take = min(d(l[0]), amount)
        l[0] = d(l[0]) - take
        amount -= take
    acct.losses = [l for l in acct.losses if d(l[0]) > 0]


@dataclass
class SimState:
    accounts: dict[str, Account] = field(default_factory=dict)
    orders: dict[str, Order] = field(default_factory=dict)          # open orders only
    alerts: dict[str, dict[str, Any]] = field(default_factory=dict)
    strategies: dict[str, dict[str, Any]] = field(default_factory=dict)
    kill_switch: bool = False
    settings: dict[str, Any] = field(default_factory=dict)

    def open_orders(self, strategy_id: str | None = None, symbol: str | None = None) -> list[Order]:
        return [o for o in self.orders.values()
                if (strategy_id is None or o.strategy_id == strategy_id) and (symbol is None or o.symbol == symbol)]


def apply_event(state: SimState, kind: str, strategy_id: str | None, p: dict[str, Any]) -> None:
    """The ONLY place simulation state changes. Used live and when rebuilding from the ledger."""
    if kind == "strategy_registered":
        state.strategies[strategy_id] = p
        state.accounts.setdefault(strategy_id, Account(strategy_id))
    elif kind == "cash_deposit":
        a = state.accounts[strategy_id]
        a.cash += d(p["amount"])
        a.deposits += d(p["amount"])
    elif kind == "order_accepted":
        o = Order.from_dict(p["order"])
        state.orders[o.id] = o
        state.accounts[strategy_id].orders += 1
    elif kind == "order_triggered":
        state.orders[p["order_id"]].triggered_seq = p["quote"]["seq"]
    elif kind in ("order_cancelled", "order_expired"):
        state.orders.pop(p["order_id"], None)
    elif kind == "fill":
        _apply_fill(state, state.accounts[strategy_id], p)
    elif kind == "token_cost":
        a = state.accounts[strategy_id]
        a.cash -= d(p["usd"])
        a.token_cost += d(p["usd"])
    elif kind == "stamp_duty":
        a = state.accounts[strategy_id]
        a.cash -= d(p["usd"])
        a.stamp_duty += d(p["usd"])
    elif kind == "alert_set":
        state.alerts[p["id"]] = {**p, "strategy_id": strategy_id}
    elif kind in ("alert_triggered", "alert_cleared"):
        state.alerts.pop(p["id"], None)
    elif kind == "kill_switch":
        state.kill_switch = bool(p["on"])
    elif kind == "settings_changed":
        state.settings.update(p["settings"])
    # every other kind (decision, order_rejected, ...) is a record without state effect


def _apply_fill(state: SimState, a: Account, p: dict[str, Any]) -> None:
    qty, price, commission = int(p["qty"]), d(p["price"]), d(p["commission"])
    sym = p["symbol"]
    pos = a.positions.setdefault(sym, Position())
    a.commissions += commission
    a.fills += 1
    a.traded_value += qty * price
    if p["side"] == "buy":
        a.cash -= qty * price + commission
        pos.qty += qty
        pos.basis += qty * price + commission
    else:
        cost = d(p["cost_basis"])
        a.cash += qty * price - commission - d(p["tax"])
        pos.qty -= qty
        pos.basis -= cost
        a.taxes += d(p["tax"])
        gain = d(p["realized_gain"])
        a.realized_gain += gain
        a.closed_trades += 1
        a.winning_trades += 1 if gain > 0 else 0
        if d(p["loss_used"]) > 0:
            _consume_losses(a, d(p["loss_used"]), int(p["tax_year"]))
        if d(p["loss_created"]) > 0:
            a.losses.append([d(p["loss_created"]), int(p["loss_expiry"])])
        if pos.qty == 0:
            del a.positions[sym]
    o = state.orders.get(p["order_id"])
    if o:
        o.filled_qty += qty
        o.commission_charged = True
        if o.remaining == 0:
            del state.orders[o.id]


def valuation(a: Account, bids: dict[str, Decimal], is_etf, year: int, profile: BrokerProfile) -> dict[str, Any]:
    """Value an account from current BID prices (what a seller would actually get).

    `net_liquidation` answers "how much would I have if I cashed out right now":
    positions sold at the bid, minus one exit commission per position and the tax
    on each gain. Unrealised losses are NOT netted against unrealised gains
    (a real account can only use losses realised earlier): conservative.
    Positions without a price are valued at zero and reported in `unpriced`.
    """
    market_value = D(0)
    exit_costs = D(0)
    credit = a.loss_credit(year)
    unpriced: list[str] = []
    unrealized = D(0)
    for sym, pos in a.positions.items():
        bid = bids.get(sym)
        if bid is None:
            unpriced.append(sym)
            exit_costs += D(0)
            continue
        value = pos.qty * bid
        market_value += value
        gain = value - profile.commission_per_order - pos.basis
        unrealized += value - pos.basis
        exit_costs += profile.commission_per_order
        if gain > 0:
            used = D(0) if is_etf(sym) else min(gain, credit)
            credit -= used
            exit_costs += ((gain - used) * profile.capital_gains_tax_rate).quantize(CENT, rounding=ROUND_UP)
    equity = a.cash + market_value
    net_liq = equity - exit_costs
    return {
        "cash": a.cash, "market_value": market_value, "equity": equity, "exit_costs": exit_costs,
        "net_liquidation": net_liq, "net_result": net_liq - a.deposits,
        "gross_result": equity + a.costs - a.deposits,    # P&L before any cost
        "unrealized": unrealized, "unpriced": unpriced, "costs": a.costs,
    }
