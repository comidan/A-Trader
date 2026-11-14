from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from tradelab.ingest.alpaca import parse_bar, parse_quote
from tradelab.market import MarketState, Universe

UTC = timezone.utc
CAL = [{"date": "2026-11-16", "open": "09:30", "close": "16:00"},
       {"date": "2026-11-27", "open": "09:30", "close": "13:00"}]   # day after Thanksgiving: early close


def market(fetched=datetime(2026, 11, 16, 12, tzinfo=UTC)):
    m = MarketState()
    m.set_calendar(CAL, fetched)
    return m


def test_open_only_inside_the_exchange_session():
    m = market()
    assert not m.is_open(datetime(2026, 11, 16, 14, 29, 59, tzinfo=UTC))
    assert m.is_open(datetime(2026, 11, 16, 14, 30, tzinfo=UTC))
    assert m.is_open(datetime(2026, 11, 16, 20, 59, 59, tzinfo=UTC))
    assert not m.is_open(datetime(2026, 11, 16, 21, 0, tzinfo=UTC))


def test_holidays_weekends_and_early_closes_follow_the_calendar():
    m = market(datetime(2026, 11, 27, 12, tzinfo=UTC))
    assert not m.is_open(datetime(2026, 11, 26, 16, tzinfo=UTC))        # Thanksgiving: not in the calendar
    assert m.is_open(datetime(2026, 11, 27, 17, 59, tzinfo=UTC))
    assert not m.is_open(datetime(2026, 11, 27, 18, 0, tzinfo=UTC))     # 13:00 New York early close


def test_missing_or_stale_calendar_means_closed():
    assert not MarketState().is_open(datetime(2026, 11, 16, 15, tzinfo=UTC))
    m = market(datetime(2026, 11, 14, 12, tzinfo=UTC))                  # fetched 2 days earlier
    assert not m.is_open(datetime(2026, 11, 16, 15, tzinfo=UTC))
    assert m.describe(datetime(2026, 11, 16, 15, tzinfo=UTC))["state"] == "unknown"


def test_stale_quote_is_not_usable():
    m = MarketState(stale_after_s=120)
    t = datetime(2026, 11, 16, 15, tzinfo=UTC)
    q = parse_quote({"T": "q", "S": "AMD", "bx": "V", "bp": 87.66, "bs": 100, "ax": "V", "ap": 87.68, "as": 400,
                     "t": "2026-11-16T15:00:00.1Z", "c": ["R"], "z": "C"}, t, "alpaca-iex")
    m.on_quote(q)
    assert (q.bid, q.ask, q.bid_size, q.ask_size) == (D("87.66"), D("87.68"), 100, 400)
    assert m.fresh_quote("AMD", t + timedelta(seconds=120)) is q
    assert m.fresh_quote("AMD", t + timedelta(seconds=121)) is None
    assert m.last_bid("AMD") == D("87.66")                             # still usable for valuation, flagged elsewhere


def test_malformed_messages_are_dropped_not_guessed():
    t = datetime(2026, 11, 16, 15, tzinfo=UTC)
    assert parse_quote({"T": "q", "S": "AMD", "bp": 1}, t, "x") is None
    assert parse_quote({"T": "q", "S": "AMD", "bp": "n/a", "bs": 1, "ap": 2, "as": 1}, t, "x") is None
    assert parse_bar({"T": "b", "S": "SPY", "o": 1}, t) is None


def test_quote_sequence_is_strictly_increasing():
    t = datetime(2026, 11, 16, 15, tzinfo=UTC)
    msg = {"S": "A", "bp": 1, "bs": 1, "ap": 2, "as": 1}
    a, b = parse_quote(msg, t, "x"), parse_quote(msg, t, "x")
    assert b.seq > a.seq


def test_halt_and_resume_codes():
    m, t = MarketState(), datetime(2026, 11, 16, 15, tzinfo=UTC)
    assert m.on_status("AAPL", "H", "Trading Halt", "T12", t) == "halted" and m.is_halted("AAPL")
    assert m.on_status("AAPL", "Q", "Quotation Resumption", "", t) is None and m.is_halted("AAPL")
    assert m.on_status("AAPL", "T", "Trading Resumption", "", t) == "resumed" and not m.is_halted("AAPL")
    assert m.on_status("IBM", "2", "Trading Halt", "M", t) == "halted"
    assert m.on_status("IBM", "3", "Resume", "", t) == "resumed"


def test_tradable_needs_a_known_active_asset():
    m = MarketState()
    assert m.tradable("AAPL") == "asset list not loaded yet"
    m.set_assets([{"symbol": "AAPL", "name": "Apple Inc.", "exchange": "NASDAQ", "tradable": True, "status": "active"},
                  {"symbol": "DEAD", "name": "x", "exchange": "NYSE", "tradable": False, "status": "active"},
                  {"symbol": "VTI", "name": "Vanguard Total Stock Market ETF", "exchange": "ARCA", "tradable": True, "status": "active"}])
    assert m.tradable("AAPL") is None and m.tradable("NOPE") == "unknown symbol" and m.tradable("DEAD")
    assert m.is_etf("VTI") and m.is_etf("SPY") and not m.is_etf("AAPL")


def test_universe_slots_evict_least_recent_but_never_pinned():
    u = Universe(["SPY", "AAPL"], capacity=4)
    assert u.request("SPY") == (True, None)
    assert u.request("X1") == (True, None) and u.request("X2") == (True, None)
    u.pin({"X1"})
    assert u.request("X3") == (True, "X2")
    u.pin({"X1", "X3"})
    assert u.request("X4") == (False, None)
    assert sorted(u.symbols()) == ["AAPL", "SPY", "X1", "X3"]
