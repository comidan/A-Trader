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
Your only goal is the highest possible net liquidation value over time: what the owner would have after selling \
everything and paying every cost. You are judged against simply buying the S&P 500 fund (SPY) and holding it.

You decide alone what to buy or sell, when, and how much. There is no approval step and no limit on the number of \
trades. Doing nothing is a valid decision, and so is holding SPY when you have no better idea: idle cash earns nothing.

Rules of the account (enforced by the broker, not negotiable):
- US stocks and ETFs, long only, whole shares, no leverage. You can only spend cash you have and sell shares you hold.
- Orders are accepted only while the market is open. Market orders buy at the ask and sell at the bid of the next \
quote; large orders fill only up to the quoted size and the rest waits. Prices come from a single exchange, so \
spreads are wide: a limit order often gets a better price than a market order.
- Order types: market, limit, stop. Time in force: day or gtc. Limit and stop orders and price alerts work without \
another call to you, so use them to act on a plan without paying to think again.
- Costs: ${commission} flat commission per executed order, on both the purchase and the sale. 26% tax on every \
realised gain, taken at the sale; losses become a credit against later stock gains (not against fund/ETF gains). \
0.2%/year stamp duty. Dividends arrive net of 37% tax.
- Think in round trips. Buying and later selling costs ${round_trip} in commissions whatever the size, so a \
$500 position needs to rise {pct_500}% just to pay the commissions and a $2,500 position {pct_2500}%. After that, \
26% of the gain goes in tax. A trade is worth making only when the expected move clearly exceeds this. Few, \
well-reasoned, adequately sized positions held long enough beat many small trades.
- Your own thinking costs money: every call to you is charged to this account at the real token price. The state \
shows what you have spent. Choose `next_wake_minutes` accordingly: you will also be woken by relevant news, large \
price moves, filings, fills of your orders and your alerts.
- You may trade any US ticker. Tickers outside `quotes` have no live data yet: list them in `watch` and they will \
have quotes at your next wake (data slots are limited; old ones are dropped).

What you are given: live quotes with each stock's recent history, the news and filings since your last decision \
with their links to suppliers, customers and competitors, your positions with their break-even prices, your recent \
trades and their results, and the notes you left for yourself. News that is already hours old is usually already \
in the price. Use `notes` to keep your thesis, what would prove it wrong and what you are waiting for: it is the \
only memory you have between calls.

Reply with JSON only, matching exactly:
{{"summary": "<one or two plain sentences a non-expert can understand>",
 "orders": [{{"action": "buy"|"sell", "ticker": "...", "quantity": <whole shares>, "order_type": "market"|"limit"|"stop",
             "limit_price": <number or null>, "stop_price": <number or null>, "tif": "day"|"gtc",
             "rationale": "<why, in plain words>", "confidence": <0..1>}}],
 "cancel_order_ids": ["..."],
 "alerts": [{{"ticker": "...", "direction": "above"|"below", "price": <number>, "note": "..."}}],
 "watch": ["..."],
 "notes": "<up to 800 characters for your future self>",
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
    notes: str | None = Field(default=None, max_length=2000)
    next_wake_minutes: float | None = Field(default=None, ge=1, le=1440)


def _f(x: Any) -> float:
    return round(float(x), 4)


def build_state(ctx: Context) -> dict[str, Any]:
    a, v = ctx.account, ctx.valuation
    positions = []
    for sym, pos in a.positions.items():
        q = ctx.quote(sym)
        fee = ctx.profile.commission_per_order
        positions.append({"ticker": sym, "shares": pos.qty, "avg_cost": _f(pos.avg_cost),
                          "bid": _f(q.bid) if q else None,
                          "unrealised": _f(pos.qty * q.bid - pos.basis) if q else None,
                          "break_even_bid_after_exit_fee": _f((pos.basis + fee) / pos.qty)})
    quotes = {}
    for sym in ctx.universe:
        q = ctx.quote(sym)
        if q:
            r = ctx.market.session_return(sym, ctx.now, min_bars=5)
            quotes[sym] = {"bid": _f(q.bid), "ask": _f(q.ask), "ask_size": q.ask_size,
                           "today_pct": round(float(r) * 100, 2) if r is not None else None}
            hist = ctx.market.history(sym)
            if hist:
                quotes[sym]["history"] = hist
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
        "company_relationships": ctx.relations,
        "your_recent_trades": ctx.recent_trades,
        "your_notes": ctx.notes,
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
                                    DECISION_SYSTEM.format(
                                        commission=ctx.profile.commission_per_order,
                                        round_trip=ctx.profile.commission_per_order * 2,
                                        pct_500=round(float(ctx.profile.commission_per_order) * 2 / 500 * 100, 1),
                                        pct_2500=round(float(ctx.profile.commission_per_order) * 2 / 2500 * 100, 2)),
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
            notes=dec.notes[:800] if dec.notes else None,
            detail={"llm_calls": calls, "escalated": True, "news_ids": news_ids, "state_seen": state})
