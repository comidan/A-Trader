"""Strategy interface. A strategy only proposes actions; the engine and broker enforce reality."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable

from ..accounting import Account
from ..market import MarketState
from ..models import BrokerProfile, Order, OrderRequest, Quote


@dataclass
class Context:
    now: datetime
    strategy_id: str
    wakes: list[dict[str, Any]]
    account: Account                      # read-only by convention
    valuation: dict[str, Any]
    available_cash: Decimal
    market: MarketState
    universe: list[str]
    open_orders: list[Order]
    alerts: list[dict[str, Any]]
    news: list[dict[str, Any]]            # received since this strategy's previous decision
    profile: BrokerProfile
    session: tuple[datetime, datetime]
    last_decision_at: datetime | None
    starting_cash: Decimal
    charge_tokens: Callable[[dict[str, Any]], None]     # records an LLM call's cost against this account, immediately
    token_budget_left: Callable[[], Decimal | None]     # None = no ceiling
    notes: str = ""                                     # the strategy's own notes from its previous decision
    recent_trades: list[dict[str, Any]] = field(default_factory=list)
    relations: dict[str, Any] = field(default_factory=dict)   # company graph for held / watched tickers
    benchmark_net_result: Decimal | None = None             # what buy & hold has made so far, same start

    def quote(self, symbol: str) -> Quote | None:
        return self.market.fresh_quote(symbol, self.now)


@dataclass
class Decision:
    orders: list[OrderRequest] = field(default_factory=list)
    cancel: list[str] = field(default_factory=list)
    alerts: list[dict[str, Any]] = field(default_factory=list)      # {symbol, direction: above|below, price, note}
    watch: list[str] = field(default_factory=list)
    next_wake_minutes: float | None = None
    summary: str = ""
    notes: str | None = None                 # kept for the next decision
    detail: dict[str, Any] = field(default_factory=dict)            # anything worth keeping in the ledger


class Strategy:
    kind = "base"
    name = "Strategy"
    description = ""
    is_llm = False
    interval_s: float = 60.0
    wants_events = False

    async def decide(self, ctx: Context) -> Decision | None:
        raise NotImplementedError


def max_affordable(cash: Decimal, ask: Decimal, commission: Decimal) -> int:
    return int((cash - commission) / ask) if cash > commission and ask > 0 else 0
