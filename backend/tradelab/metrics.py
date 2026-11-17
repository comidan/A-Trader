"""Performance metrics and the weekly "do the agents beat the baselines?" report.

Statistics are deliberately conservative: nothing is called significant on
fewer than 20 trading days, and exact small-sample tests are used.
"""
from __future__ import annotations

import math
from collections import OrderedDict
from decimal import Decimal
from typing import Any

from .market import NY

MIN_DAYS = 20


# ---------------------------------------------------------------- statistics
def _betacf(a: float, b: float, x: float) -> float:
    tiny, qab, qap, qam = 1e-300, a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        for aa in (m * (b - m) * x / ((qam + m2) * (a + m2)), -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))):
            d = 1 + aa * d
            d = 1 / (d if abs(d) > tiny else tiny)
            c = 1 + aa / c
            c = c if abs(c) > tiny else tiny
            h *= d * c
        if abs(d * c - 1) < 1e-12:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x))
    return bt * _betacf(a, b, x) / a if x < (a + 1) / (a + b + 2) else 1 - bt * _betacf(b, a, 1 - x) / b


def t_test_p(values: list[float]) -> tuple[float, float] | None:
    """One-sample two-sided Student t-test of mean == 0. Returns (t, p)."""
    n = len(values)
    if n < 3:
        return None
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    if math.sqrt(var) <= 1e-12 * max(1.0, abs(mean)):
        return None                              # no variation: the test is undefined
    t = mean / math.sqrt(var / n)
    return t, _betai((n - 1) / 2, 0.5, (n - 1) / ((n - 1) + t * t))


def binomial_p(wins: int, n: int) -> float | None:
    """Exact two-sided test of win rate == 50%."""
    if n == 0:
        return None
    k = max(wins, n - wins)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n)


# ---------------------------------------------------------------- series
def daily_index(points: list[dict[str, Any]], starting: Decimal) -> "OrderedDict[str, float]":
    """Last value per New York day of (starting capital + net result): deposits never count as profit."""
    from datetime import datetime
    out: OrderedDict[str, float] = OrderedDict()
    for p in points:
        day = datetime.fromisoformat(p["ts"]).astimezone(NY).date().isoformat()
        out[day] = float(starting + Decimal(p["net_result"]))
    return out


def daily_returns(index: "OrderedDict[str, float]") -> "OrderedDict[str, float]":
    days, out = list(index), OrderedDict()
    for prev, cur in zip(days, days[1:]):
        if index[prev] > 0:
            out[cur] = index[cur] / index[prev] - 1
    return out


def max_drawdown(values: list[float]) -> float:
    peak, worst = -math.inf, 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, v / peak - 1)
    return worst


def sharpe(returns: list[float]) -> float | None:
    if len(returns) < 5:
        return None
    mean = sum(returns) / len(returns)
    sd = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))
    return mean / sd * math.sqrt(252) if sd > 0 else None


def strategy_metrics(account, value: dict[str, Any], points: list[dict[str, Any]], starting: Decimal) -> dict[str, Any]:
    series = [float(starting + Decimal(p["net_result"])) for p in points] + [float(starting + value["net_result"])]
    rets = list(daily_returns(daily_index(points, starting)).values())
    deposits = float(account.deposits) or 1.0
    return {
        "return_pct": float(value["net_result"]) / deposits * 100,
        "gross_return_pct": float(value["gross_result"]) / deposits * 100,
        "max_drawdown_pct": max_drawdown(series) * 100,
        "sharpe": sharpe(rets), "trading_days": len(rets),
        "win_rate_pct": account.winning_trades / account.closed_trades * 100 if account.closed_trades else None,
        "closed_trades": account.closed_trades, "orders": account.orders, "fills": account.fills,
        "turnover": float(account.traded_value) / deposits,
        "avg_cost_per_order": float(account.costs) / account.orders if account.orders else None,
        "token_spend": float(account.token_cost),
        "bets_compared": account.bench_bets, "bets_beating_index": account.bench_wins,
    }


# ---------------------------------------------------------------- weekly report
def weekly_report(rows: list[dict[str, Any]], benchmark: str = "buy_hold") -> dict[str, Any]:
    """rows: [{id, name, is_llm, net_result, return_pct, daily: {day: return}, closed_trades, winning_trades}]"""
    bench = next((r for r in rows if r["id"] == benchmark), None)
    out = []
    for r in rows:
        if bench is None or r["id"] == benchmark:
            continue
        days = [d for d in r["daily"] if d in bench["daily"]]
        excess = [r["daily"][d] - bench["daily"][d] for d in days]
        test = t_test_p(excess)
        ahead = r["return_pct"] > bench["return_pct"]
        if len(excess) < MIN_DAYS:
            verdict = (f"Too early to say: {len(excess)} trading days of data, at least {MIN_DAYS} are needed. "
                       f"So far {'ahead of' if ahead else 'behind'} buy & hold.")
            significant = None
        elif test and test[1] < 0.05:
            verdict = f"{'Beats' if test[0] > 0 else 'Loses to'} buy & hold, and the difference is statistically significant."
            significant = True
        else:
            verdict = (f"{'Ahead of' if ahead else 'Behind'} buy & hold, but the difference could be luck "
                       f"(p = {test[1]:.2f})." if test else "No measurable difference from buy & hold.")
            significant = False
        out.append({
            "id": r["id"], "name": r["name"], "is_llm": r["is_llm"], "return_pct": r["return_pct"],
            "excess_return_pct": r["return_pct"] - bench["return_pct"], "days": len(excess),
            "t_stat": test[0] if test else None, "p_value": test[1] if test else None, "significant": significant,
            "closed_trades": r["closed_trades"],
            "win_rate_pct": r["winning_trades"] / r["closed_trades"] * 100 if r["closed_trades"] else None,
            "win_rate_p_value": binomial_p(r["winning_trades"], r["closed_trades"]), "verdict": verdict})
    agents = [x for x in out if x["is_llm"]]
    if not rows:
        headline = "No simulation yet: create one to start the comparison."
    elif not agents:
        headline = "No AI agent is running."
    elif any(x["significant"] and (x["t_stat"] or 0) > 0 for x in agents):
        headline = "Yes: at least one AI agent beats buy & hold net of all costs, with statistical significance."
    elif all(x["significant"] is None for x in agents):
        headline = "Too early to say whether the AI agents beat the baselines."
    else:
        headline = "No: so far no AI agent beats buy & hold net of all costs with statistical significance."
    return {"benchmark": benchmark, "benchmark_return_pct": bench["return_pct"] if bench else None,
            "headline": headline, "strategies": out, "min_days_for_significance": MIN_DAYS}
