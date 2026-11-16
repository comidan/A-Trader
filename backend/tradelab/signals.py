"""Technical indicators, two classic rule-based signals, and an honest backtest of those rules.

A backtest is valid here because the rules are plain arithmetic on past prices (unlike an LLM, a rule cannot
"remember" what happened next). It is still an approximation: daily bars, entry and exit at the next day's open,
real commissions and tax, but no bid/ask spread. Live results on the single-exchange feed will be worse.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

MIN_BARS = 210


def sma(x: list[float], n: int) -> float | None:
    return sum(x[-n:]) / n if len(x) >= n else None


def rsi(closes: list[float], n: int) -> float | None:
    """Wilder's RSI."""
    if len(closes) < n + 1:
        return None
    window = closes[-(n * 12 + 1):]
    gains = [max(b - a, 0.0) for a, b in zip(window, window[1:])]
    losses = [max(a - b, 0.0) for a, b in zip(window, window[1:])]
    avg_g, avg_l = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        avg_g, avg_l = (avg_g * (n - 1) + g) / n, (avg_l * (n - 1) + l) / n
    return 100.0 if avg_l == 0 else round(100 - 100 / (1 + avg_g / avg_l), 1)


def indicators(closes: list[float], highs: list[float] | None = None, lows: list[float] | None = None) -> dict[str, Any] | None:
    if len(closes) < 60:
        return None
    c = closes[-1]
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    out: dict[str, Any] = {
        "rsi14": rsi(closes, 14), "rsi2": rsi(closes, 2),
        "above_50d_average": c > s50 if s50 else None, "above_200d_average": c > s200 if s200 else None,
        "pct_vs_20d_average": round((c / s20 - 1) * 100, 2) if s20 else None,
        "pct_from_55d_high": round((c / max(closes[-55:]) - 1) * 100, 2),
    }
    if highs and lows and len(highs) >= 15:
        trs = [max(h - l, abs(h - pc), abs(l - pc)) for h, l, pc in zip(highs[-14:], lows[-14:], closes[-15:-1])]
        out["average_daily_range_pct"] = round(sum(trs) / 14 / c * 100, 2)
    out["signals"] = [name for name in RULES if entry_signal(name, closes)]
    return out


# ---------------------------------------------------------------- rules
RULES = {
    "pullback_in_uptrend": "Stock above its 200-day average that has just dropped hard for a couple of days (2-day RSI below 10). "
                           "Buy the dip; sell when it closes back above its 5-day average.",
    "breakout_55d": "Stock above its 200-day average closing at a new 55-day high. Buy strength; sell when it closes below "
                    "its 20-day average.",
}


def entry_signal(rule: str, closes: list[float]) -> bool:
    s200 = sma(closes, 200)
    if s200 is None or closes[-1] <= s200:
        return False
    if rule == "pullback_in_uptrend":
        r = rsi(closes, 2)
        return r is not None and r < 10
    if rule == "breakout_55d":
        return closes[-1] >= max(closes[-55:]) and closes[-1] > closes[-2]
    return False


def exit_signal(rule: str, closes: list[float]) -> bool:
    if rule == "pullback_in_uptrend":
        return closes[-1] > sma(closes, 5)
    if rule == "breakout_55d":
        return closes[-1] < sma(closes, 20)
    return False


def backtest(rule: str, bars: list[dict[str, float]], commission: float, tax_rate: float, stake: float = 1500.0) -> list[dict[str, Any]]:
    """Trades the rule would have made on one stock. Signal on a day's close, action at the next day's open."""
    closes = [b["c"] for b in bars]
    trades, entry, i = [], None, MIN_BARS - 1
    while i < len(bars) - 1:
        seen = closes[:i + 1]
        if entry is None and entry_signal(rule, seen):
            shares = int(stake // bars[i + 1]["o"])
            if shares >= 1:
                entry = (i + 1, bars[i + 1]["o"], shares)
        elif entry is not None and exit_signal(rule, seen):
            day, price, shares = entry
            gross = shares * (bars[i + 1]["o"] - price)
            net = gross - 2 * commission
            net -= max(net, 0.0) * tax_rate                    # tax on gains; losses earn nothing back (conservative)
            trades.append({"net_usd": net, "net_pct": net / (shares * price) * 100, "days": i + 1 - day})
            entry = None
        i += 1
    return trades


def track_record(daily: dict[str, list[dict[str, float]]], commission: Decimal, tax_rate: Decimal,
                 skip: set[str] = frozenset()) -> dict[str, Any]:
    """How each rule did over the stored history, after commissions and tax, across all followed stocks."""
    out: dict[str, Any] = {"rules": {}, "note": "Past record on daily bars, entry and exit at the next open, after commissions "
                                               "and tax, no bid/ask spread. Live results on this price feed will be worse. "
                                               "A good past record does not guarantee the future."}
    usable = {s: b for s, b in daily.items() if s not in skip and len(b) > MIN_BARS + 20}
    for rule, description in RULES.items():
        trades = [t for bars in usable.values() for t in backtest(rule, bars, float(commission), float(tax_rate))]
        n = len(trades)
        out["rules"][rule] = {
            "description": description, "trades": n, "stocks_tested": len(usable),
            "win_rate_pct": round(sum(t["net_usd"] > 0 for t in trades) / n * 100, 1) if n else None,
            "average_net_pct_per_trade": round(sum(t["net_pct"] for t in trades) / n, 2) if n else None,
            "total_net_usd": round(sum(t["net_usd"] for t in trades), 2) if n else None,
            "average_days_held": round(sum(t["days"] for t in trades) / n, 1) if n else None}
    spy = daily.get("SPY")
    if spy and len(spy) > MIN_BARS + 20:
        out["sp500_over_the_tested_period_pct"] = round((spy[-1]["c"] / spy[MIN_BARS]["o"] - 1) * 100, 2)
        out["tested_trading_days"] = len(spy) - MIN_BARS
    return out
