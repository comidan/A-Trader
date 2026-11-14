"""Core value types. Money and prices are Decimal; quantities are whole shares."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

D = Decimal
CENT = D("0.01")


def d(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


@dataclass(frozen=True)
class Quote:
    """Top of book as received. `recv_ts` is OUR receive time: the only time used for look-ahead rules."""
    symbol: str
    bid: Decimal
    bid_size: int
    ask: Decimal
    ask_size: int
    recv_ts: datetime
    seq: int
    source: str = ""
    exch_ts: str | None = None

    def valid(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "bid": str(self.bid), "bid_size": self.bid_size, "ask": str(self.ask),
                "ask_size": self.ask_size, "recv_ts": self.recv_ts.isoformat(), "seq": self.seq,
                "source": self.source, "exch_ts": self.exch_ts}


@dataclass
class OrderRequest:
    symbol: str
    side: str                      # buy | sell
    qty: int
    type: str = "market"           # market | limit | stop
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    tif: str = "day"               # day | gtc
    meta: dict[str, Any] = field(default_factory=dict)   # rationale, triggering news, decision id


@dataclass
class Order:
    id: str
    strategy_id: str
    symbol: str
    side: str
    qty: int
    type: str
    limit_price: Decimal | None
    stop_price: Decimal | None
    tif: str
    ready_ts: datetime             # moment the decision was ready (after any LLM latency)
    reserve_price: Decimal         # price used for the buying-power check at submission
    filled_qty: int = 0
    commission_charged: bool = False
    triggered_seq: int | None = None   # for stop orders: quote that triggered it
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def remaining(self) -> int:
        return self.qty - self.filled_qty

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "strategy_id": self.strategy_id, "symbol": self.symbol, "side": self.side,
                "qty": self.qty, "type": self.type,
                "limit_price": str(self.limit_price) if self.limit_price is not None else None,
                "stop_price": str(self.stop_price) if self.stop_price is not None else None,
                "tif": self.tif, "ready_ts": self.ready_ts.isoformat(), "reserve_price": str(self.reserve_price),
                "filled_qty": self.filled_qty, "meta": self.meta}

    @staticmethod
    def from_dict(p: dict[str, Any]) -> "Order":
        return Order(p["id"], p["strategy_id"], p["symbol"], p["side"], int(p["qty"]), p["type"],
                     d(p["limit_price"]) if p.get("limit_price") is not None else None,
                     d(p["stop_price"]) if p.get("stop_price") is not None else None,
                     p["tif"], datetime.fromisoformat(p["ready_ts"]), d(p["reserve_price"]),
                     meta=p.get("meta") or {})


@dataclass(frozen=True)
class BrokerProfile:
    """Fee schedule of the reference broker. Source documented in README."""
    name: str = "Directa SIM (US market, regime amministrato)"
    commission_per_order: Decimal = D("9")       # USD, flat, per executed order
    min_price: Decimal = D("0.50")               # stocks below this are not tradable
    fractional_shares: bool = False
    capital_gains_tax_rate: Decimal = D("0.26")
    loss_carry_forward_years: int = 4
    stamp_duty_annual_rate: Decimal = D("0.002")  # "imposta di bollo" on securities value
    dividend_us_withholding: Decimal = D("0.15")  # US-Italy treaty rate, withheld at source
    dividend_italian_tax: Decimal = D("0.26")     # applied on what is left after the US withholding
    source: str = "https://www.directa.it/api/media/file/commissioni-mercato-USA.pdf (read 2026-11-14)"
