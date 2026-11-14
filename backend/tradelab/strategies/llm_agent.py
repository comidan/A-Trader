"""Lightweight LLM agent: a cheap triage call, then at most one decision call with a strong model.

The agent pays for every token out of its own account, immediately, and is
shown what its thinking has cost so far, so it can decide how often thinking
is worth paying for.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from ..llm import LLM, LLMError, extract_json
from ..market import NY
from ..models import OrderRequest
from .base import Context, Decision, Strategy

TRIAGE_SYSTEM = """You are the cheap first filter for an autonomous stock trader. You are given the events that just \
happened and the trader's positions. Decide whether the expensive decision model should be consulted now. Consult it \
only if an event plausibly changes what the trader should hold or do. Reply with JSON only: \
{"escalate": true|false, "reason": "<one short sentence>"}"""

DECISION_SYSTEM = """You are an autonomous trader running a simulated brokerage account that mirrors a real one. \
Your only goal is the highest possible net liquidation value: what the owner would have after selling everything \
and paying every cost.

You decide alone what to buy or sell, when, and how much. There is no approval step and no limit on the number of \
trades. Doing nothing is a valid decision.

Rules of the account (enforced by the broker, not negotiable):
- US stocks and ETFs, long only, whole shares, no leverage. You can only spend cash you have and sell shares you hold.
- Orders are accepted only while the market is open. Market orders buy at the ask and sell at the bid of the next \
quote; large orders fill only up to the quoted size and the rest waits.
- Order types: market, limit, stop. Time in force: day or gtc. Limit and stop orders and price alerts work without \
another call to you.
- Costs: ${commission} flat commission per executed order (both buying and selling). 26% tax on every realised gain, \
taken at the sale; losses become a credit against later stock gains (not against fund/ETF gains). 0.2%/year stamp \
duty on holdings.
- Your own thinking costs money: every call to you is charged to this account at the real token price. The state \
shows what you have spent. Choose `next_wake_minutes` accordingly: you will also be woken by relevant news, large \
price moves, filings, fills of your orders and your alerts.
- You may trade any US ticker. Tickers outside `quotes` have no live data yet: list them in `watch` and they will \
have quotes at your next wake (data slots are limited; old ones are dropped).

Reply with JSON only, matching exactly:
{{"summary": "<one or two plain sentences a non-expert can understand>",
 "orders": [{{"action": "buy"|"sell", "ticker": "...", "quantity": <whole shares>, "order_type": "market"|"limit"|"stop",
             "limit_price": <number or null>, "stop_price": <number or null>, "tif": "day"|"gtc",
             "rationale": "<why, in plain words>", "confidence": <0..1>}}],
 "cancel_order_ids": ["..."],
 "alerts": [{{"ticker": "...", "direction": "above"|"below", "price": <number>, "note": "..."}}],
 "watch": ["..."],
 "next_wake_minutes": <number between 1 and 1440, or null for the default>}}"""


class LLMOrder(BaseModel):
    action: Literal["buy", "sell"]
    ticker: str = Field(min_length=1, max_length=10)
    quantity: int = Field(gt=0)
    order_type: Literal["market", "limit", "stop"] = "market"
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    tif: Literal["day", "gtc"] = "day"
    rationale: str = ""
    confidence: float = Field(default=0.5, ge=0, le=1)


class LLMAlert(BaseModel):
    ticker: str
    direction: Literal["above", "below"]
    price: Decimal = Field(gt=0)
    note: str = ""


class LLMDecision(BaseModel):
    summary: str = ""
    orders: list[LLMOrder] = []
    cancel_order_ids: list[str] = []
    alerts: list[LLMAlert] = []
    watch: list[str] = []
    next_wake_minutes: float | None = Field(default=None, ge=1, le=1440)


def _f(x: Any) -> float:
    return round(float(x), 4)


def build_state(ctx: Context) -> dict[str, Any]:
    a, v = ctx.account, ctx.valuation
    positions = []
    for sym, pos in a.positions.items():
        q = ctx.quote(sym)
        positions.append({"ticker": sym, "shares": pos.qty, "avg_cost": _f(pos.avg_cost),
                          "bid": _f(q.bid) if q else None,
                          "unrealised": _f(pos.qty * q.bid - pos.basis) if q else None})
    quotes = {}
    for sym in ctx.universe:
        q = ctx.quote(sym)
        if q:
            r = ctx.market.session_return(sym, ctx.now, min_bars=5)
            quotes[sym] = {"bid": _f(q.bid), "ask": _f(q.ask), "ask_size": q.ask_size,
                           "today_pct": round(float(r) * 100, 2) if r is not None else None}
    news = [{"id": n["id"], "minutes_ago": int((ctx.now.timestamp() - _ts(n["recv_ts"])) / 60), "kind": n["kind"],
             "source": n["source"], "tickers": n["symbols"],
             "related": [f"{l['symbol']} ({l['relation']} of {l['via']})" for l in n["links"]][:6],
             "impact": n["impact"], "headline": n["headline"][:220], "summary": (n.get("summary") or "")[:300]}
            for n in ctx.news[:40]]
    return {
        "time_new_york": ctx.now.astimezone(NY).strftime("%Y-%m-%d %H:%M"),
        "minutes_to_close": int((ctx.session[1] - ctx.now).total_seconds() / 60),
        "woken_by": [{"reason": w["reason"], **({"about": w["data"]} if w.get("data") else {})} for w in ctx.wakes][:12],
        "account": {
            "cash": _f(a.cash), "cash_available_for_new_orders": _f(ctx.available_cash),
            "net_liquidation_value": _f(v["net_liquidation"]), "money_put_in": _f(a.deposits),
            "net_result": _f(v["net_result"]), "positions": positions,
            "costs_paid_so_far": {"commissions": _f(a.commissions), "taxes": _f(a.taxes),
                                  "your_thinking_tokens": _f(a.token_cost), "stamp_duty": _f(a.stamp_duty)},
            "tax_loss_credit": _f(a.loss_credit(ctx.market.tax_year(ctx.now))),
        },
        "open_orders": [{"id": o.id, "ticker": o.symbol, "side": o.side, "shares_left": o.remaining, "type": o.type,
                         "limit": _f(o.limit_price) if o.limit_price else None,
                         "stop": _f(o.stop_price) if o.stop_price else None, "tif": o.tif} for o in ctx.open_orders],
        "alerts": [{"ticker": al["symbol"], "direction": al["direction"], "price": al["price"]} for al in ctx.alerts],
        "quotes": quotes,
        "news_since_your_last_decision": news,
    }


def _ts(iso_str: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(iso_str).timestamp()


class LLMLightAgent(Strategy):
    kind, name = "llm_light", "AI trader"
    description = ("A single AI model reads the news, filings and prices and trades on its own. "
                   "A cheap model filters events; a strong model decides. It pays for its own thinking.")
    is_llm = True
    wants_events = True

    def __init__(self, llm: LLM, triage_model: str, decision_model: str, interval_minutes: float) -> None:
        self.llm, self.triage_model, self.decision_model = llm, triage_model, decision_model
        self.interval_s = interval_minutes * 60

    async def decide(self, ctx: Context) -> Decision | None:
        if not self.llm.configured:
            return None
        budget = ctx.token_budget_left()
        if budget is not None and budget <= 0:
            return Decision(summary="Daily token ceiling reached: no thinking until tomorrow.",
                            detail={"skipped": "token_budget"})
        reasons = {w["reason"] for w in ctx.wakes}
        state = build_state(ctx)
        calls: list[dict[str, Any]] = []
        direct = ctx.last_decision_at is None or ctx.last_decision_at < ctx.session[0] or "scheduled" in reasons
        if not direct:
            if reasons == {"timer"} and not ctx.news:
                return None                                   # nothing new: thinking would be wasted money
            try:
                t = await self.llm.chat(self.triage_model, TRIAGE_SYSTEM, json.dumps({
                    "events": state["woken_by"], "news": state["news_since_your_last_decision"][:15],
                    "positions": state["account"]["positions"], "open_orders": state["open_orders"]}),
                    max_tokens=300, purpose="triage")
            except LLMError as e:
                return Decision(summary="Triage call failed; holding.", detail={"error": str(e)})
            ctx.charge_tokens(t.usage())
            calls.append(t.usage())
            try:
                verdict = extract_json(t.text)
                escalate, why = bool(verdict.get("escalate")), str(verdict.get("reason", ""))[:300]
            except (ValueError, AttributeError):
                escalate, why = False, "triage reply was not valid JSON"
            if not escalate:
                return Decision(summary=f"Checked the news, nothing worth acting on. {why}",
                                detail={"triage": why, "llm_calls": calls, "escalated": False})
        try:
            r = await self.llm.chat(self.decision_model,
                                    DECISION_SYSTEM.format(commission=ctx.profile.commission_per_order),
                                    json.dumps(state), max_tokens=6000, purpose="decision")
        except LLMError as e:
            return Decision(summary="Decision call failed; holding.", detail={"error": str(e), "llm_calls": calls})
        ctx.charge_tokens(r.usage())
        calls.append(r.usage())
        try:
            dec = LLMDecision.model_validate(extract_json(r.text))
        except (ValueError, ValidationError) as e:
            return Decision(summary="The model's reply was not valid; holding.",
                            detail={"invalid_reply": r.text[:2000], "error": str(e)[:500], "llm_calls": calls})
        news_ids = [n["id"] for n in ctx.news[:40]]
        orders = [OrderRequest(o.ticker.upper(), o.action, o.quantity, o.order_type, o.limit_price, o.stop_price, o.tif,
                               meta={"rationale": o.rationale, "confidence": o.confidence}) for o in dec.orders]
        return Decision(
            orders=orders, cancel=dec.cancel_order_ids,
            alerts=[{"symbol": al.ticker.upper(), "direction": al.direction, "price": str(al.price), "note": al.note}
                    for al in dec.alerts],
            watch=[w.upper() for w in dec.watch][:10], next_wake_minutes=dec.next_wake_minutes, summary=dec.summary,
            detail={"llm_calls": calls, "escalated": True, "news_ids": news_ids, "state_seen": state})
