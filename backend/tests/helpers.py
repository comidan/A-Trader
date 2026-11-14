from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from tradelab.accounting import SimState, apply_event
from tradelab.broker import SimBroker
from tradelab.models import OrderRequest, Quote

T0 = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)   # a Monday, 10:00 New York


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


class FakeMarket:
    def __init__(self):
        self.open, self.halted, self.untradable, self.quotes, self.etfs = True, set(), {}, {}, {"SPY"}

    def is_open(self, now): return self.open
    def is_halted(self, s): return s in self.halted
    def tradable(self, s): return self.untradable.get(s)
    def fresh_quote(self, s, now): return self.quotes.get(s)
    def is_etf(self, s): return s in self.etfs
    def tax_year(self, now): return now.year


class Rig:
    """A broker wired to an in-memory event log, as the engine wires it to the ledger."""

    def __init__(self, cash="5000", **broker_kw):
        self.state, self.market, self.events, self._seq = SimState(), FakeMarket(), [], 0
        self.broker = SimBroker(self.state, self.emit, self.market, **broker_kw)
        self.emit("strategy_registered", {"name": "s"}, "s")
        self.emit("cash_deposit", {"amount": cash}, "s")

    def emit(self, kind, payload, sid):
        self.events.append((kind, sid, payload))
        apply_event(self.state, kind, sid, payload)

    @property
    def acct(self): return self.state.accounts["s"]

    def quote(self, t, bid, ask, bid_size=1000, ask_size=1000, symbol="AAPL"):
        """A quote is first known to the market (as on the wire), then offered to the broker."""
        self._seq += 1
        q = Quote(symbol, D(str(bid)), bid_size, D(str(ask)), ask_size, at(t), self._seq, "test")
        self.market.quotes[symbol] = q
        self.broker.on_quote(q)
        return q

    def order(self, t, side, qty, symbol="AAPL", **kw):
        for k in ("limit_price", "stop_price"):
            if k in kw:
                kw[k] = D(str(kw[k]))
        return self.broker.submit("s", OrderRequest(symbol, side, qty, **kw), at(t))

    def fills(self): return [p for k, _, p in self.events if k == "fill"]
    def kinds(self): return [k for k, _, _ in self.events]
