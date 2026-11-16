"""Extra research inputs: option-implied moves (Alpaca), crowd sentiment (StockTwits), and, with a free
Finnhub key, earnings dates and analyst ratings. Shapes checked against the live services on 2026-11-16
(Finnhub from its documentation only: no key was available)."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from .alpaca import DATA_HOST, AlpacaRest

CONTRACT = re.compile(r"^[A-Z.]+(\d{6})([CP])(\d{8})$")


def _mid(snap: dict[str, Any]) -> float | None:
    q = snap.get("latestQuote") or {}
    bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
    return (bid + ask) / 2 if bid > 0 and ask > 0 else None


def options_summary(snapshots: dict[str, Any], spot: float, today: date) -> dict[str, Any] | None:
    """What the options market is pricing: the move implied by the nearest at-the-money straddle, and whether
    more puts or calls traded today."""
    by_expiry: dict[str, dict[float, dict[str, Any]]] = {}
    call_vol = put_vol = 0
    for sym, snap in snapshots.items():
        m = CONTRACT.match(sym)
        if not m:
            continue
        vol = int((snap.get("dailyBar") or {}).get("v") or 0)
        call_vol, put_vol = call_vol + (vol if m[2] == "C" else 0), put_vol + (vol if m[2] == "P" else 0)
        by_expiry.setdefault(m[1], {}).setdefault(int(m[3]) / 1000, {})[m[2]] = snap
    for expiry in sorted(by_expiry):
        strikes = sorted(by_expiry[expiry], key=lambda k: abs(k - spot))
        for k in strikes[:3]:
            pair = by_expiry[expiry][k]
            c, p = (_mid(pair["C"]) if "C" in pair else None), (_mid(pair["P"]) if "P" in pair else None)
            if c and p:
                exp = datetime.strptime(expiry, "%y%m%d").date()
                ivs = [float(x["impliedVolatility"]) for x in pair.values() if x.get("impliedVolatility")]
                return {"implied_move_pct": round((c + p) / spot * 100, 2), "by_date": exp.isoformat(),
                        "days": (exp - today).days,
                        "implied_volatility_pct_yearly": round(sum(ivs) / len(ivs) * 100, 1) if ivs else None,
                        "put_call_volume_ratio": round(put_vol / call_vol, 2) if call_vol else None}
    return None


async def fetch_options(rest: AlpacaRest, symbol: str, spot: float, today: date) -> dict[str, Any] | None:
    body = await rest._get(f"{DATA_HOST}/v1beta1/options/snapshots/{symbol}", feed="indicative", limit=200,
                           strike_price_gte=round(spot * 0.97, 2), strike_price_lte=round(spot * 1.03, 2),
                           expiration_date_gte=(today + timedelta(days=4)).isoformat(),
                           expiration_date_lte=(today + timedelta(days=25)).isoformat())
    return options_summary(body.get("snapshots") or {}, spot, today)


def stocktwits_summary(body: dict[str, Any]) -> dict[str, Any] | None:
    """Crowd mood from the latest StockTwits messages: how many of the tagged ones are bullish or bearish."""
    msgs = body.get("messages") or []
    if not msgs:
        return None
    tags = [((m.get("entities") or {}).get("sentiment") or {}).get("basic") for m in msgs]
    bull, bear = tags.count("Bullish"), tags.count("Bearish")
    try:
        span = (datetime.fromisoformat(msgs[0]["created_at"].replace("Z", "+00:00"))
                - datetime.fromisoformat(msgs[-1]["created_at"].replace("Z", "+00:00"))).total_seconds() / 60
    except (KeyError, ValueError):
        span = None
    return {"bullish": bull, "bearish": bear, "untagged": len(msgs) - bull - bear,
            "bullish_share_pct": round(bull / (bull + bear) * 100) if bull + bear >= 5 else None,
            "latest_messages_span_minutes": round(span, 1) if span is not None else None,
            "note": "retail chatter; a short span means the stock is being talked about a lot"}


async def fetch_stocktwits(http: httpx.AsyncClient, symbol: str) -> dict[str, Any] | None:
    r = await http.get(f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json")
    r.raise_for_status()
    return stocktwits_summary(r.json())


def finnhub_earnings(body: dict[str, Any], symbols: set[str]) -> dict[str, dict[str, Any]]:
    out = {}
    for e in body.get("earningsCalendar") or []:
        if e.get("symbol") in symbols and e.get("date"):
            when = {"bmo": "before the market opens", "amc": "after the market closes"}.get(e.get("hour"), "time not given")
            if e["symbol"] not in out or e["date"] < out[e["symbol"]]["date"]:
                out[e["symbol"]] = {"date": e["date"], "when": when, "eps_estimate": e.get("epsEstimate")}
    return out


def finnhub_rating(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    r = max(rows, key=lambda x: x.get("period", ""))
    return {"strong_buy": r.get("strongBuy"), "buy": r.get("buy"), "hold": r.get("hold"), "sell": r.get("sell"),
            "strong_sell": r.get("strongSell"), "as_of": r.get("period")}


async def fetch_finnhub(http: httpx.AsyncClient, key: str, symbols: list[str], today: date) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {s: {} for s in symbols}
    cal = await http.get("https://finnhub.io/api/v1/calendar/earnings", params={
        "from": today.isoformat(), "to": (today + timedelta(days=45)).isoformat(), "token": key})
    cal.raise_for_status()
    for sym, e in finnhub_earnings(cal.json(), set(symbols)).items():
        out[sym]["next_earnings"] = e
    for sym in symbols:
        r = await http.get("https://finnhub.io/api/v1/stock/recommendation", params={"symbol": sym, "token": key})
        if r.status_code == 200 and (rating := finnhub_rating(r.json())):
            out[sym]["analyst_ratings"] = rating
    return out
