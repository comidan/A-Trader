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

TRIAGE_SYSTEM = """You are the cheap first filter for an active stock trader whose job is to beat the S&P 500. You are given \
the events that just happened and the trader's positions. Decide whether the expensive decision model should be \
consulted now. Consult it if an event plausibly creates a trade or changes a position: news on a held or watched \
company, on one of its suppliers, customers or competitors, a filing, a sharp move, a fill. Skip routine noise. Reply with JSON only: \
{"escalate": true|false, "reason": "<at most 20 words>"}"""

DECISION_SYSTEM = """You are a professional active trader running a simulated brokerage account that mirrors a real one. \
Your job is to beat the S&P 500 by buying and selling stocks. You are measured on one number: the account's net \
liquidation value (everything sold, every cost paid) compared with simply holding the S&P 500 fund over the same \
period. Matching the index is a failure: anyone can do that for one commission.

You decide alone what to buy or sell, when, and how much. There is no approval step and no limit on the number of trades.

How to work:
- Think relative to the index. A position is only worth holding while you expect it to do better than the S&P 500 \
from here. `vs_spy_20d` / `vs_spy_60d` show how each stock has done against it; `benchmark` shows the gap you must close.
- Look for an edge: a catalyst in a filing or article whose text you are given, a second-order effect through \
`company_relationships` (a supplier, customer or competitor hit by news about someone else), a strong stock \
pulling back or a weak one breaking down, a mover in `market_movers` that fits a real story. A headline that is \
hours old is usually already in the price; the consequences for related companies often are not.
- Trade actively. You are called every few minutes: each time, scan the prices (`intraday` shows where every stock \
is in today's range and how fast it is moving; `history` shows the bigger trend) and act when you see a setup, \
with or without news. Take profits when a move has paid; do not wait for a headline to tell you.
- Every purchase should come with an exit plan: `take_profit_price` and `stop_loss_price` on the order. The broker \
side then sells for you the instant either level is reached, with no call to you and no thinking cost. Use \
`exit_plans` to set or move those levels on positions you already hold. A take-profit of 1.5 to 4% with a stop \
about half as far away is a normal short-term trade; size it so the move pays for the round trip (see costs).
- Be decisive but selective: 2 to 4 positions at a time, each large enough that commissions do not eat the result. \
Rotate out of a position when its reason is gone or something better appears. Sitting in cash is acceptable only \
while you wait for a setup; say what you are waiting for.
- Use the evidence beyond price. `technical` gives each stock's indicators and any rule-based `signals` firing now; \
`rule_track_record` shows how those exact rules did over the past year after costs, so weigh a signal by its \
record. `options` shows the move the options market is pricing and whether puts or calls dominate. `crowd` is \
retail chatter (useful as a warning of crowded trades, rarely as a reason to buy). `calendar` gives the next \
earnings date and analyst ratings when available: know whether you are holding through an earnings report.
- When a trade has taught you something that will still be true next month, put it in `lesson` (one sentence). \
Your lessons are shown to you at every decision.
- Mind `spread_pct`. Quotes come from one exchange and can be several percent wide while the real price has not \
moved. Never send a market order when the spread is above 0.3%: use a limit order close to `last_trade`. Prefer \
heavily traded stocks, whose quotes are tight. A quote that jumps while `last_trade` stays put is noise.
- Manage risk like a professional: decide before you buy what would prove you wrong, and use stop orders and \
price alerts so the plan runs without another call to you. Cut losers; do not average down without a new reason.
- Use `notes` as your trading journal: thesis, entry, what would prove it wrong, what you are waiting for. It is \
the only memory you have between calls. `your_recent_trades` shows what worked and what did not: learn from it.

Rules of the account (enforced by the broker, not negotiable):
- US stocks, long only, whole shares, no leverage. You can only spend cash you have and sell shares you hold.
- {index_rule}
- Orders are accepted only while the market is open. Market orders buy at the ask and sell at the bid of the next \
quote; large orders fill only up to the quoted size and the rest waits. Prices come from a single exchange, so \
spreads are wide: a limit order near the current price usually gets a better fill than a market order.
- Order types: market, limit, stop. Time in force: day or gtc.
- Costs: ${commission} flat commission per executed order, on the purchase and on the sale, so a round trip costs \
${round_trip} whatever the size: {pct_500}% of a $500 position, {pct_2500}% of a $2,500 one. 26% tax on every \
realised gain, taken at the sale; losses become a credit against later gains. 0.2%/year stamp duty. Dividends \
arrive net of 37% tax. Trade when the expected move clearly exceeds these costs, not for the sake of activity.
- Your own thinking costs money: every call to you is charged to this account. Choose `next_wake_minutes` \
accordingly; you are also woken by relevant news, large price moves, filings, fills of your orders and your alerts.
- You may trade any US ticker. Tickers outside `quotes` have no live price yet: list them in `watch` and they will \
have quotes at your next wake (data slots are limited; unused ones are dropped).

Reply with JSON only, matching exactly:
{{"summary": "<one or two plain sentences a non-expert can understand>",
 "orders": [{{"action": "buy"|"sell", "ticker": "...", "quantity": <whole shares>, "order_type": "market"|"limit"|"stop",
             "limit_price": <number or null>, "stop_price": <number or null>, "tif": "day"|"gtc",
             "take_profit_price": <number or null>, "stop_loss_price": <number or null>,
             "rationale": "<why this beats holding the index, in plain words>", "confidence": <0..1>}}],
 "cancel_order_ids": ["..."],
 "alerts": [{{"ticker": "...", "direction": "above"|"below", "price": <number>, "note": "..."}}],
 "exit_plans": [{{"ticker": "...", "take_profit_price": <number or null>, "stop_loss_price": <number or null>}}],
 "watch": ["..."],
 "notes": "<your trading journal for next time, up to 800 characters>",
 "lesson": "<one durable lesson, or null>",
 "next_wake_minutes": <number between 1 and 1440, or null for the default>}}"""

INDEX_FUNDS = {"SPY", "VOO", "IVV", "SPLG", "VTI", "ITOT", "QQQ", "QQQM", "DIA", "IWM", "RSP", "SCHX", "SCHB"}


class LLMOrder(BaseModel):
    action: Literal["buy", "sell"]
    ticker: str = Field(min_length=1, max_length=10)
    quantity: int = Field(gt=0)
    order_type: Literal["market", "limit", "stop"] = "market"
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    tif: Literal["day", "gtc"] = "day"
    take_profit_price: Decimal | None = Field(default=None, gt=0)
    stop_loss_price: Decimal | None = Field(default=None, gt=0)
    rationale: str = ""
    confidence: float = Field(default=0.5, ge=0, le=1)


class LLMExitPlan(BaseModel):
    ticker: str
    take_profit_price: Decimal | None = Field(default=None, gt=0)
    stop_loss_price: Decimal | None = Field(default=None, gt=0)


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
    exit_plans: list[LLMExitPlan] = []
    watch: list[str] = []
    notes: str | None = Field(default=None, max_length=2000)
    lesson: str | None = Field(default=None, max_length=600)
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
                           "spread_pct": round(float((q.ask - q.bid) / ((q.ask + q.bid) / 2)) * 100, 2),
                           "today_pct": round(float(r) * 100, 2) if r is not None else None}
            intra = ctx.market.intraday(sym, ctx.now)
            if intra:
                quotes[sym]["intraday"] = intra
            for key, value in (("technical", ctx.market.technical(sym)), ("options", ctx.market.options.get(sym)),
                               ("crowd", ctx.market.social.get(sym)), ("calendar", ctx.market.events.get(sym))):
                if value:
                    quotes[sym][key] = {k: v for k, v in value.items() if k not in ("received_at_utc", "note")}
            hist = ctx.market.history(sym)
            if hist:
                quotes[sym]["history"] = hist
    news = [{"id": n["id"], "minutes_ago": int((ctx.now.timestamp() - _ts(n["recv_ts"])) / 60), "kind": n["kind"],
             "source": n["source"], "tickers": n["symbols"],
             "related": [f"{l['symbol']} ({l['relation']} of {l['via']})" for l in n["links"]][:6],
             "impact": n["impact"], "headline": n["headline"][:220], "summary": (n.get("summary") or "")[:300]}
            for n in ctx.news[:40]]
    # the actual text of the most important articles and filings (not just headlines), a few at a time
    held = set(ctx.account.positions)
    rank = lambda n: (bool(set(n["symbols"]) & held), n["kind"] == "filing", {"high": 2, "medium": 1}.get(n["impact"], 0))
    with_text = sorted([n for n in ctx.news[:40] if (n.get("extra") or {}).get("text")], key=rank, reverse=True)[:4]
    by_id = {x["id"]: x for x in news}
    for n in with_text:
        by_id[n["id"]]["text"] = n["extra"]["text"][:2500]
        if n["extra"].get("text_chars_total", 0) > 2500:
            by_id[n["id"]]["text_truncated_from_chars"] = n["extra"]["text_chars_total"]
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
        "exit_plans_running_without_you": [{"ticker": p["symbol"], "take_profit_price": p.get("take_profit"),
                                            "stop_loss_price": p.get("stop_loss")} for p in ctx.exit_plans],
        "benchmark": None if ctx.benchmark_net_result is None else {
            "buy_and_hold_sp500_net_result": _f(ctx.benchmark_net_result),
            "you_minus_benchmark": _f(v["net_result"] - ctx.benchmark_net_result)},
        "quotes": quotes,
        "market_movers": ctx.market.movers or None,
        "news_since_your_last_decision": news,
        "company_relationships": ctx.relations,
        "rule_track_record": ctx.market.rule_record or None,
        "your_recent_trades": ctx.recent_trades,
        "your_notes": ctx.notes,
        "your_lessons": ctx.lessons,
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

    def __init__(self, llm: LLM, triage_model: str, decision_model: str, interval_minutes: float,
                 allow_index_funds: bool = False) -> None:
        self.llm, self.triage_model, self.decision_model = llm, triage_model, decision_model
        self.allow_index_funds = allow_index_funds
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
        # Its own schedule and the regular timer go straight to the decision model: an active trader scans
        # prices on a clock. Outside events (news, filings, moves, fills) pass through the cheap filter first.
        direct = (ctx.last_decision_at is None or ctx.last_decision_at < ctx.session[0]
                  or bool(reasons & {"scheduled", "timer"}))
        if not direct:
            try:
                t = await self.llm.chat(self.triage_model, TRIAGE_SYSTEM, json.dumps({
                    "events": state["woken_by"], "news": state["news_since_your_last_decision"][:15],
                    "positions": state["account"]["positions"], "open_orders": state["open_orders"]}),
                    max_tokens=2000, purpose="triage")       # the model's hidden reasoning counts against this limit
            except LLMError as e:
                return Decision(summary="Triage call failed; holding.", detail={"error": str(e)})
            ctx.charge_tokens(t.usage())
            calls.append(t.usage())
            try:
                verdict = extract_json(t.text)
                escalate, why = bool(verdict.get("escalate")), str(verdict.get("reason", ""))[:300]
            except (ValueError, AttributeError):
                # an unreadable filter must not hide a real event: let the decision model look
                escalate, why = True, "the filter's reply could not be read, so the event was passed on"
            if not escalate:
                return Decision(summary=f"Checked the news, nothing worth acting on. {why}",
                                detail={"triage": why, "llm_calls": calls, "escalated": False})
        try:
            r = await self.llm.chat(self.decision_model,
                                    DECISION_SYSTEM.format(
                                        index_rule=("You may also hold index funds." if self.allow_index_funds else
                                                    "You may not buy broad index funds (SPY, QQQ, VOO and similar): "
                                                    "holding the benchmark is not trading. Individual stocks and "
                                                    "sector or thematic funds are allowed."),
                                        commission=ctx.profile.commission_per_order,
                                        round_trip=ctx.profile.commission_per_order * 2,
                                        pct_500=round(float(ctx.profile.commission_per_order) * 2 / 500 * 100, 1),
                                        pct_2500=round(float(ctx.profile.commission_per_order) * 2 / 2500 * 100, 2)),
                                    json.dumps(state), max_tokens=8000, purpose="decision")
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
        refused = [o.ticker.upper() for o in dec.orders
                   if o.action == "buy" and not self.allow_index_funds and o.ticker.upper() in INDEX_FUNDS]
        orders = [OrderRequest(o.ticker.upper(), o.action, o.quantity, o.order_type, o.limit_price, o.stop_price, o.tif,
                               meta={"rationale": o.rationale, "confidence": o.confidence}) for o in dec.orders
                  if not (o.action == "buy" and o.ticker.upper() in refused)]
        return Decision(
            orders=orders, cancel=dec.cancel_order_ids,
            alerts=[{"symbol": al.ticker.upper(), "direction": al.direction, "price": str(al.price), "note": al.note}
                    for al in dec.alerts],
            exit_plans=[{"symbol": t, "take_profit": str(tp) if tp else None, "stop_loss": str(sl) if sl else None}
                        for t, tp, sl in [(o.ticker.upper(), o.take_profit_price, o.stop_loss_price) for o in dec.orders
                                          if o.action == "buy" and o.ticker.upper() not in refused]
                        + [(e.ticker.upper(), e.take_profit_price, e.stop_loss_price) for e in dec.exit_plans] if tp or sl],
            watch=[w.upper() for w in dec.watch][:10], next_wake_minutes=dec.next_wake_minutes, summary=dec.summary,
            notes=dec.notes[:800] if dec.notes else None, lesson=dec.lesson[:300] if dec.lesson else None,
            detail={"llm_calls": calls, "escalated": True, "news_ids": news_ids, "state_seen": state,
                    **({"index_fund_buys_refused": refused} if refused else {})})
