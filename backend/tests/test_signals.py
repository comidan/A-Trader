import json
import asyncio
from datetime import date, timedelta
from decimal import Decimal as D

from tradelab.ingest.extras import finnhub_earnings, finnhub_rating, options_summary, stocktwits_summary
from tradelab.signals import MIN_BARS, backtest, entry_signal, exit_signal, indicators, rsi, sma, track_record
from tradelab.models import OrderRequest


def test_rsi_and_averages_on_known_series():
    up = [float(i) for i in range(1, 40)]
    assert rsi(up, 14) == 100.0 and rsi(list(reversed(up)), 14) == 0.0
    # Wilder's worked example (StockCharts): first RSI(14) of this series is 70.5
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
    assert abs(rsi(closes, 14) - 70.5) < 0.2
    assert sma([1, 2, 3, 4], 2) == 3.5 and sma([1], 2) is None and rsi([1.0, 2.0], 14) is None


def uptrend_with_dip():
    c = [100 + i * 0.3 for i in range(230)]          # long steady uptrend
    return c + [c[-1] * 0.97, c[-1] * 0.94]           # then two sharp down days


def test_pullback_rule_fires_only_in_an_uptrend_and_exits_on_the_bounce():
    c = uptrend_with_dip()
    assert entry_signal("pullback_in_uptrend", c) and not entry_signal("breakout_55d", c)
    assert not entry_signal("pullback_in_uptrend", [200 - i * 0.3 for i in range(230)] + [120, 110])   # downtrend: no
    assert not exit_signal("pullback_in_uptrend", c)
    assert exit_signal("pullback_in_uptrend", c + [c[-1] * 1.08])
    ind = indicators(c)
    assert ind["signals"] == ["pullback_in_uptrend"] and ind["rsi2"] < 10 and ind["above_200d_average"] is True


def test_breakout_rule():
    c = [100 + i * 0.3 for i in range(230)]
    assert entry_signal("breakout_55d", c) and exit_signal("breakout_55d", c + [c[-1] * 0.9])


def bars_from(closes):
    return [{"o": c, "h": c * 1.01, "l": c * 0.99, "c": c} for c in closes]


def test_backtest_acts_on_the_next_open_and_charges_commissions_and_tax():
    c = uptrend_with_dip()
    closes = c + [170.0, 175.0, 176.0]                # bounce: closes back above the 5-day average
    bars = bars_from(closes)
    for i, b in enumerate(bars):
        b["o"] = b["c"] - 0.5                         # opens differ from closes, so using the wrong one would show
    trades = backtest("pullback_in_uptrend", bars, commission=9.0, tax_rate=0.26, stake=1500.0)
    assert len(trades) == 1
    signal_day = next(i for i in range(MIN_BARS - 1, len(closes)) if entry_signal("pullback_in_uptrend", closes[:i + 1]))
    exit_signal_day = next(i for i in range(signal_day + 1, len(closes)) if exit_signal("pullback_in_uptrend", closes[:i + 1]))
    entry, exit_ = bars[signal_day + 1]["o"], bars[exit_signal_day + 1]["o"]      # the opens AFTER each signal
    shares = int(1500 // entry)
    expected = (shares * (exit_ - entry) - 18) * (1 - 0.26)
    assert expected > 0 and abs(trades[0]["net_usd"] - expected) < 1e-9
    assert trades[0]["days"] == exit_signal_day - signal_day


def test_backtest_never_uses_a_price_from_the_future():
    c = uptrend_with_dip()
    a = backtest("pullback_in_uptrend", bars_from(c + [170.0, 175.0, 176.0, 177.0]), 9.0, 0.26)
    b = backtest("pullback_in_uptrend", bars_from(c + [170.0, 175.0, 176.0, 177.0, 5.0, 900.0]), 9.0, 0.26)
    assert b[0] == a[0]                               # later prices cannot change an earlier trade


def test_track_record_summarises_all_stocks_and_compares_with_the_index():
    c = uptrend_with_dip() + [170.0, 175.0, 176.0] + [176.0 + i for i in range(30)]
    rec = track_record({"AAA": bars_from(c), "SPY": bars_from([100 + i * 0.1 for i in range(len(c))]), "QQQ": bars_from(c)},
                       D("9"), D("0.26"), skip={"SPY", "QQQ"})
    r = rec["rules"]["pullback_in_uptrend"]
    assert r["trades"] == 1 and r["stocks_tested"] == 1 and r["win_rate_pct"] == 100.0
    assert rec["tested_trading_days"] == len(c) - MIN_BARS and rec["sp500_over_the_tested_period_pct"] > 0
    assert "does not guarantee" in rec["note"]


def snap(bid, ask, vol, iv=0.3):
    return {"latestQuote": {"bp": bid, "ap": ask}, "dailyBar": {"v": vol}, "impliedVolatility": iv}


def test_option_implied_move_from_the_nearest_at_the_money_straddle():
    today = date(2026, 11, 16)
    snaps = {"AAPL261120C00352500": snap(4.0, 4.2, 100), "AAPL261120P00352500": snap(3.8, 4.0, 150),
             "AAPL261120C00360000": snap(1.0, 1.2, 50), "AAPL261127C00352500": snap(6.0, 6.4, 10),
             "AAPL261120P00340000": snap(0, 0, 7), "garbage": {}}
    o = options_summary(snaps, 352.85, today)
    assert o["by_date"] == "2026-11-20" and o["days"] == 4
    assert o["implied_move_pct"] == round((4.1 + 3.9) / 352.85 * 100, 2)
    assert o["put_call_volume_ratio"] == round(157 / 160, 2) and o["implied_volatility_pct_yearly"] == 30.0
    assert options_summary({"AAPL261120C00352500": snap(4.0, 4.2, 1)}, 352.85, today) is None      # no put: no straddle


def test_crowd_sentiment_counts_only_tagged_messages():
    msgs = [{"created_at": f"2026-11-16T17:{31 - i:02d}:00Z", "entities": {"sentiment": {"basic": t} if t else None}}
            for i, t in enumerate(["Bullish", "Bullish", None, "Bearish", "Bullish", "Bullish", None, "Bearish"])]
    s = stocktwits_summary({"messages": msgs})
    assert (s["bullish"], s["bearish"], s["untagged"], s["bullish_share_pct"]) == (4, 2, 2, 67)
    assert s["latest_messages_span_minutes"] == 7.0
    assert stocktwits_summary({"messages": []}) is None
    assert stocktwits_summary({"messages": msgs[:3]})["bullish_share_pct"] is None      # too few tagged to mean anything


def test_finnhub_parsers():
    cal = {"earningsCalendar": [{"symbol": "NVDA", "date": "2026-11-19", "hour": "amc", "epsEstimate": 1.2},
                                {"symbol": "NVDA", "date": "2027-02-20", "hour": "amc"}, {"symbol": "ZZZ", "date": "2026-11-18"}]}
    assert finnhub_earnings(cal, {"NVDA"}) == {"NVDA": {"date": "2026-11-19", "when": "after the market closes", "eps_estimate": 1.2}}
    r = finnhub_rating([{"period": "2026-10-01", "buy": 20, "hold": 5, "sell": 1, "strongBuy": 12, "strongSell": 0},
                        {"period": "2026-11-01", "buy": 22, "hold": 4, "sell": 1, "strongBuy": 13, "strongSell": 0}])
    assert r["as_of"] == "2026-11-01" and r["buy"] == 22 and finnhub_rating([]) is None
