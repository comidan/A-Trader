"""Simulated broker. Long only, no leverage, whole shares.

Fidelity rules enforced here (each has a test in tests/test_broker.py):
- A buy fills at the ASK and a sell at the BID, never at mid or at a bar close.
- An order can only fill on a quote RECEIVED strictly after the decision was
  ready (plus optional extra latency). The quote that the decision saw is
  never used.
- A fill is capped at the size quoted on that side; the rest stays queued for
  the next quote. Several orders of one strategy share the same quoted size.
- No fills while the market is closed or the symbol is halted; no orders are
  accepted while the market is closed.
- Cash can never go negative because of a purchase; shares never go negative.
- Limit buy fills only when ask <= limit (a real resting order could also be
  hit earlier: ignoring that is conservative). Stop orders trigger on the
  quote and fill from the NEXT quote.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Callable, Protocol

from .accounting import SimState, tax_on_sale
from .models import BrokerProfile, D, Order, OrderRequest, Quote

TICK = D("0.0001")


class Market(Protocol):
    def is_open(self, now: datetime) -> bool: ...
    def is_halted(self, symbol: str) -> bool: ...
    def tradable(self, symbol: str) -> str | None: ...        # None if tradable, else the reason
    def fresh_quote(self, symbol: str, now: datetime) -> Quote | None: ...
    def is_etf(self, symbol: str) -> bool: ...
    def tax_year(self, now: datetime) -> int: ...


Emit = Callable[[str, dict[str, Any], str | None], None]
RiskCheck = Callable[[str, OrderRequest, Quote, datetime], str | None]


class SimBroker:
    def __init__(self, state: SimState, emit: Emit, market: Market, profile: BrokerProfile = BrokerProfile(),
                 extra_latency_ms: int = 0, slippage_bps: Decimal = D(0), risk_check: RiskCheck | None = None) -> None:
        self.state, self.emit, self.market, self.profile = state, emit, market, profile
        self.latency = timedelta(milliseconds=extra_latency_ms)
        self.slippage = D(slippage_bps) / D(10000)
        self.risk_check = risk_check
        self._used: dict[tuple[str, str, str], tuple[int, int]] = {}   # (strategy, symbol, side) -> (quote seq, shares used)

    # ------------------------------------------------------------------ submit
    def available_cash(self, strategy_id: str) -> Decimal:
        a = self.state.accounts[strategy_id]
        reserved = D(0)
        for o in self.state.open_orders(strategy_id):
            if o.side == "buy":
                reserved += o.remaining * o.reserve_price
                if not o.commission_charged:
                    reserved += self.profile.commission_per_order
        return a.cash - reserved

    def sellable(self, strategy_id: str, symbol: str) -> int:
        held = self.state.accounts[strategy_id].qty(symbol)
        return held - sum(o.remaining for o in self.state.open_orders(strategy_id, symbol) if o.side == "sell")

    def submit(self, strategy_id: str, req: OrderRequest, now: datetime) -> tuple[Order | None, str | None]:
        """Validate like a real broker. Returns (order, None) or (None, rejection reason)."""
        reason = self._validate(strategy_id, req, now)
        base = {"symbol": req.symbol, "side": req.side, "qty": req.qty, "type": req.type,
                "limit_price": str(req.limit_price) if req.limit_price is not None else None,
                "stop_price": str(req.stop_price) if req.stop_price is not None else None,
                "tif": req.tif, "meta": req.meta}
        if reason:
            self.emit("order_rejected", {**base, "reason": reason, "at": now.isoformat()}, strategy_id)
            return None, reason
        q = self.market.fresh_quote(req.symbol, now)
        reserve = req.limit_price if req.type == "limit" else max(q.ask, req.stop_price or D(0))
        order = Order(uuid.uuid4().hex, strategy_id, req.symbol, req.side, req.qty, req.type, req.limit_price,
                      req.stop_price, req.tif, now, reserve, meta=req.meta)
        self.emit("order_accepted", {"order": order.to_dict(), "quote_seen": q.to_dict()}, strategy_id)
        return self.state.orders[order.id], None

    def _validate(self, sid: str, r: OrderRequest, now: datetime) -> str | None:
        if self.state.kill_switch:
            return "kill switch is on"
        if r.side not in ("buy", "sell") or r.type not in ("market", "limit", "stop") or r.tif not in ("day", "gtc"):
            return "malformed order"
        if not isinstance(r.qty, int) or isinstance(r.qty, bool) or r.qty < 1:
            return "quantity must be a whole number of shares, at least 1 (fractional shares are not offered)"
        if r.type == "limit" and (r.limit_price is None or r.limit_price <= 0):
            return "limit order needs a positive limit price"
        if r.type == "stop" and (r.stop_price is None or r.stop_price <= 0):
            return "stop order needs a positive stop price"
        if not self.market.is_open(now):
            return "market is closed"
        why = self.market.tradable(r.symbol)
        if why:
            return why
        q = self.market.fresh_quote(r.symbol, now)
        if q is None or not q.valid():
            return "no live quote for this symbol (data missing or late: ticker suspended)"
        if q.ask < self.profile.min_price:
            return f"price below the broker minimum of ${self.profile.min_price}"
        if r.side == "sell":
            if r.qty > self.sellable(sid, r.symbol):
                return "shares not held (short selling is not allowed)"
        else:
            ref = r.limit_price if r.type == "limit" else max(q.ask, r.stop_price or D(0))
            need = r.qty * ref + self.profile.commission_per_order
            if need > self.available_cash(sid):
                return "insufficient cash"
            if self.risk_check:
                return self.risk_check(sid, r, q, now)
        return None

    # ------------------------------------------------------------------ cancel / expire
    def cancel(self, order_id: str, reason: str = "cancelled by strategy") -> bool:
        o = self.state.orders.get(order_id)
        if not o:
            return False
        self.emit("order_cancelled", {"order_id": o.id, "reason": reason, "unfilled_qty": o.remaining}, o.strategy_id)
        return True

    def expire_day_orders(self) -> None:
        for o in list(self.state.orders.values()):
            if o.tif == "day":
                self.emit("order_expired", {"order_id": o.id, "reason": "market closed", "unfilled_qty": o.remaining},
                          o.strategy_id)

    # ------------------------------------------------------------------ fills
    def on_quote(self, q: Quote) -> None:
        if not q.valid() or not self.market.is_open(q.recv_ts) or self.market.is_halted(q.symbol):
            return
        for o in sorted(self.state.open_orders(symbol=q.symbol), key=lambda o: o.ready_ts):
            if o.id in self.state.orders:
                self._try_fill(o, q)

    def _try_fill(self, o: Order, q: Quote) -> None:
        if q.recv_ts <= o.ready_ts + self.latency:
            return                                    # this quote had already arrived: not usable
        if o.type == "stop":
            if o.triggered_seq is None:
                hit = q.bid <= o.stop_price if o.side == "sell" else q.ask >= o.stop_price
                if hit:
                    self.emit("order_triggered", {"order_id": o.id, "quote": q.to_dict()}, o.strategy_id)
                return                                # fills start from the next quote
            if q.seq <= o.triggered_seq:
                return
        buy = o.side == "buy"
        price, size = (q.ask, q.ask_size) if buy else (q.bid, q.bid_size)
        if o.type == "limit":
            if (buy and price > o.limit_price) or (not buy and price < o.limit_price):
                return
        elif self.slippage:
            price = (price * (1 + self.slippage)).quantize(TICK, ROUND_UP) if buy else \
                    (price * (1 - self.slippage)).quantize(TICK, ROUND_DOWN)
        key = (o.strategy_id, o.symbol, o.side)
        seq, used = self._used.get(key, (-1, 0))
        used = used if seq == q.seq else 0
        qty = min(o.remaining, size - used)
        acct = self.state.accounts[o.strategy_id]
        commission = D(0) if o.commission_charged else self.profile.commission_per_order
        if buy:
            affordable = int((acct.cash - commission) / price) if acct.cash > commission else 0
            if affordable < 1:
                self.cancel(o.id, "insufficient cash at the fill price")
                return
            qty = min(qty, affordable)
        else:
            qty = min(qty, acct.qty(o.symbol))
        if qty < 1:
            return
        payload: dict[str, Any] = {
            "order_id": o.id, "symbol": o.symbol, "side": o.side, "qty": qty, "price": str(price),
            "commission": str(commission), "quote": q.to_dict(), "order_ready_ts": o.ready_ts.isoformat(),
            "remaining_after": o.remaining - qty, "meta": o.meta,
        }
        if not buy:
            pos = acct.positions[o.symbol]
            cost = pos.basis * qty / pos.qty if qty < pos.qty else pos.basis
            gain = qty * price - commission - cost
            year = self.market.tax_year(q.recv_ts)
            t = tax_on_sale(acct, gain, self.market.is_etf(o.symbol), year, self.profile)
            payload.update(cost_basis=str(cost), realized_gain=str(gain), tax=str(t["tax"]), tax_year=year,
                           loss_used=str(t["loss_used"]), loss_created=str(t["loss_created"]),
                           loss_expiry=t["loss_expiry"])
        self._used[key] = (q.seq, used + qty)
        self.emit("fill", payload, o.strategy_id)
