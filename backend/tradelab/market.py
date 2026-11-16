"""Live market state: latest quotes, halts, trading calendar, tradable assets, data slots.

Conservative defaults: if the calendar or the asset list is missing the market
counts as closed / the symbol as not tradable. A quote older than
`stale_after_s` is not usable for decisions or order checks.
"""
from __future__ import annotations

import re
from collections import OrderedDict, deque
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from .models import Quote

NY = ZoneInfo("America/New_York")
HALT_CODES = {"H", "P", "2"}            # trading halt / volatility pause
RESUME_CODES = {"T", "3"}               # trading resumption ("Q" is quotation only: still halted)
FUND_WORDS = re.compile(r"\b(ETF|ETN|Fund|Trust|Index|iShares|ProShares|SPDR|Vanguard|Invesco)\b", re.I)


class MarketState:
    def __init__(self, stale_after_s: float = 120.0) -> None:
        self.stale_after = timedelta(seconds=stale_after_s)
        self.quotes: dict[str, Quote] = {}
        self.halted: dict[str, dict[str, Any]] = {}
        self.assets: dict[str, dict[str, Any]] = {}
        self.calendar: dict[date, tuple[datetime, datetime]] = {}
        self.calendar_fetched_at: datetime | None = None
        self.bars: dict[str, deque] = {}
        self.etf_overrides = {"SPY", "QQQ"}
        self.daily: dict[str, list[float]] = {}        # adjusted daily closes, oldest first (completed days only)
        self.daily_received_at: datetime | None = None
        self.movers: dict[str, Any] = {}               # filtered screener snapshot, with its receive time

    # ---- calendar
    def set_calendar(self, rows: list[dict[str, str]], fetched_at: datetime) -> None:
        cal = {}
        for r in rows:
            day = date.fromisoformat(r["date"])
            o = datetime.combine(day, datetime.strptime(r["open"], "%H:%M").time(), NY).astimezone(timezone.utc)
            c = datetime.combine(day, datetime.strptime(r["close"], "%H:%M").time(), NY).astimezone(timezone.utc)
            cal[day] = (o, c)
        self.calendar, self.calendar_fetched_at = cal, fetched_at

    def calendar_ok(self, now: datetime) -> bool:
        return bool(self.calendar_fetched_at and now - self.calendar_fetched_at < timedelta(hours=36))

    def session(self, now: datetime) -> tuple[datetime, datetime] | None:
        return self.calendar.get(now.astimezone(NY).date()) if self.calendar_ok(now) else None

    def is_open(self, now: datetime) -> bool:
        s = self.session(now)
        return bool(s and s[0] <= now < s[1])

    def next_open(self, now: datetime) -> datetime | None:
        future = sorted(o for o, _ in self.calendar.values() if o > now)
        return future[0] if future else None

    def tax_year(self, now: datetime) -> int:
        return now.astimezone(NY).year

    def describe(self, now: datetime) -> dict[str, Any]:
        if not self.calendar_ok(now):
            return {"state": "unknown", "detail": "trading calendar not available: treated as closed"}
        s = self.session(now)
        if self.is_open(now):
            return {"state": "open", "detail": f"closes {s[1].isoformat()}", "closes_at_utc": s[1].isoformat()}
        nxt = self.next_open(now)
        return {"state": "closed", "detail": "opens " + nxt.isoformat() if nxt else "no session scheduled",
                "opens_at_utc": nxt.isoformat() if nxt else None}

    # ---- assets
    def set_assets(self, rows: list[dict[str, Any]]) -> None:
        self.assets = {r["symbol"]: {"name": r.get("name", ""), "exchange": r.get("exchange", ""),
                                     "tradable": bool(r.get("tradable")), "status": r.get("status")} for r in rows}

    def tradable(self, symbol: str) -> str | None:
        if not self.assets:
            return "asset list not loaded yet"
        a = self.assets.get(symbol)
        if a is None:
            return "unknown symbol"
        if not a["tradable"] or a["status"] != "active":
            return "symbol is not tradable"
        if a["exchange"] == "OTC":
            return "OTC symbols are not supported"
        return None

    def is_etf(self, symbol: str) -> bool:
        """Funds are taxed more harshly; when the name is ambiguous we assume fund (conservative)."""
        return symbol in self.etf_overrides or bool(FUND_WORDS.search(self.assets.get(symbol, {}).get("name", "")))

    def name(self, symbol: str) -> str:
        return self.assets.get(symbol, {}).get("name", "")

    # ---- quotes / halts / bars
    def on_quote(self, q: Quote) -> None:
        self.quotes[q.symbol] = q

    def fresh_quote(self, symbol: str, now: datetime) -> Quote | None:
        q = self.quotes.get(symbol)
        return q if q and now - q.recv_ts <= self.stale_after else None

    def last_bid(self, symbol: str) -> Decimal | None:
        q = self.quotes.get(symbol)
        return q.bid if q and q.bid > 0 else None

    def on_status(self, symbol: str, code: str, message: str, reason: str, recv_ts: datetime) -> str | None:
        """Returns 'halted' / 'resumed' when the state changed."""
        if code in HALT_CODES and symbol not in self.halted:
            self.halted[symbol] = {"since": recv_ts.isoformat(), "message": message, "reason": reason}
            return "halted"
        if code in RESUME_CODES and symbol in self.halted:
            del self.halted[symbol]
            return "resumed"
        return None

    def is_halted(self, symbol: str) -> bool:
        return symbol in self.halted

    def on_bar(self, symbol: str, bar: dict[str, Any]) -> None:
        self.bars.setdefault(symbol, deque(maxlen=800)).append(bar)

    def set_daily(self, closes: dict[str, list[float]], received_at: datetime) -> None:
        self.daily.update(closes)
        self.daily_received_at = received_at

    def history(self, symbol: str) -> dict[str, float] | None:
        """Past performance from completed daily bars (no value from today, nothing from the future)."""
        c = self.daily.get(symbol)
        if not c or len(c) < 21:
            return None
        ret = lambda n: round((c[-1] / c[-1 - n] - 1) * 100, 2) if len(c) > n and c[-1 - n] > 0 else None
        daily = [c[i] / c[i - 1] - 1 for i in range(len(c) - 20, len(c)) if c[i - 1] > 0]
        mean = sum(daily) / len(daily)
        vol = (sum((x - mean) ** 2 for x in daily) / (len(daily) - 1)) ** 0.5 * 252 ** 0.5
        high = max(c[-252:])
        out = {"last_close": round(c[-1], 2), "pct_5d": ret(5), "pct_20d": ret(20), "pct_60d": ret(60),
               "pct_from_1y_high": round((c[-1] / high - 1) * 100, 2), "volatility_pct_yearly": round(vol * 100, 1)}
        spy = self.daily.get("SPY")
        if symbol != "SPY" and spy and len(spy) > 60:          # how it did against the index we are judged on
            for n in (20, 60):
                if out[f"pct_{n}d"] is not None and spy[-1 - n] > 0:
                    out[f"vs_spy_{n}d"] = round(out[f"pct_{n}d"] - (spy[-1] / spy[-1 - n] - 1) * 100, 2)
        return out

    def set_movers(self, raw: dict[str, Any], received_at: datetime) -> None:
        """Keep only real, tradable common stocks and funds priced at 5 dollars or more (no warrants, no pennies)."""
        def ok(sym: str) -> bool:
            return sym.isalpha() and len(sym) <= 5 and self.tradable(sym) is None
        pick = lambda rows: [{"ticker": r["symbol"], "today_pct": round(float(r["percent_change"]), 1), "price": float(r["price"])}
                             for r in rows if ok(r["symbol"]) and float(r.get("price") or 0) >= 5
                             and abs(float(r["percent_change"])) <= 100][:8]     # beyond that it is a listing artefact
        self.movers = {"gainers": pick(raw.get("gainers", [])), "losers": pick(raw.get("losers", [])),
                       "most_traded": [r["symbol"] for r in raw.get("most_active", []) if ok(r["symbol"])][:15],
                       "received_at_utc": received_at.isoformat()}

    def session_return(self, symbol: str, now: datetime, min_bars: int = 30) -> Decimal | None:
        """Return since today's first received bar, using only bars already received."""
        s = self.session(now)
        if not s:
            return None
        today = [b for b in self.bars.get(symbol, ()) if b["recv_ts"] >= s[0]]
        if len(today) < min_bars or today[0]["o"] <= 0:
            return None
        return today[-1]["c"] / today[0]["o"] - 1


class Universe:
    """Which symbols get a live data slot. The free IEX plan streams at most 30 symbols.

    Core symbols always have a slot. Symbols that are held, have open orders or
    alerts are pinned. The remaining slots go to the most recently requested
    symbols; the least recently used one is dropped when a slot is needed.
    """

    def __init__(self, core: list[str], capacity: int) -> None:
        self.core = list(dict.fromkeys(core))[:capacity]
        self.capacity = capacity
        self.dynamic: OrderedDict[str, None] = OrderedDict()
        self.pinned: set[str] = set()

    def symbols(self) -> list[str]:
        return self.core + [s for s in self.dynamic if s not in self.core]

    def pin(self, symbols: set[str]) -> None:
        self.pinned = set(symbols)
        for s in symbols:
            self.request(s)

    def request(self, symbol: str) -> tuple[bool, str | None]:
        """Returns (has_slot, evicted_symbol)."""
        if symbol in self.core:
            return True, None
        if symbol in self.dynamic:
            self.dynamic.move_to_end(symbol)
            return True, None
        evicted = None
        if len(self.core) + len(self.dynamic) >= self.capacity:
            evicted = next((s for s in self.dynamic if s not in self.pinned), None)
            if evicted is None:
                return False, None
            del self.dynamic[evicted]
        self.dynamic[symbol] = None
        return True, evicted
