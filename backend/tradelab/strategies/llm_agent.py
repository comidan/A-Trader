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

TRIAGE_SYSTEM = """You are the first filter for an investor who holds stocks for days to weeks and must beat the S&P 500. \
You are given the events that just happened and the investor's positions. Decide whether the expensive decision \
model should look now. Pass the event on only if it could change the outlook for the coming days or weeks of a \
held or watched company, or of its suppliers, customers or competitors: results, guidance, a filing, regulation, \
tariffs, a deal, a lawsuit, a macro shift, a fill of one of the investor's orders. Skip intraday price wobbles \
and routine noise. Reply with JSON only: {"escalate": true|false, "reason": "<at most 20 words>"}"""

DECISION_SYSTEM = """You are a professional investor running a simulated brokerage account that mirrors a real one. \
Your job is to beat the S&P 500 by taking positions in stocks for days to weeks. You are measured on one number: the \
account's net liquidation value (everything sold, every cost paid) compared with simply holding the S&P 500 fund over \
the same period. Matching the index is a failure: anyone can do that for one commission.

You decide alone what to buy or sell, when, and how much. There is no approval step.

Where your edge is, and where it is not:
- You will never be faster than the market. A headline is in the price within seconds, long before you read it. \
Do not trade intraday moves or chase a stock that has already jumped on news.
- Your edge is understanding. Markets price the first-order fact at once but are slower with what follows from it: \
the effect of news on a company's suppliers, customers and competitors (`company_relationships`), what a filing's \
text really says about the coming quarters, how a tariff, a rate move or a regulation spreads across an industry, \
the drift that often follows a real earnings surprise or a guidance change. Think about what the world news and \
filings mean for the next days and weeks, and which stocks or sector funds the market has not yet adjusted.
- Every position needs a thesis: what you expect, over how many days or weeks, why the market has not priced it, \
and what would prove you wrong. Write it in `notes`. Without a thesis, do not trade.

How to work:
- Think relative to the index. A position is only worth holding while you expect it to beat the S&P 500 from here \
over your horizon. `history` (5, 20, 60 days, `vs_spy_20d`, `vs_spy_60d`) and `technical` show the trend; \
`benchmark` shows the gap you must close; `your_recent_trades` shows whether each past bet beat the index over \
the same days (`vs_index_same_period`). Learn from that.
- Few, well-sized positions: 2 to 5 at a time, each at least $1,000 so commissions do not eat the result. Hold them \
while the thesis holds; do not sell a good thesis because of one noisy day. Sell when the thesis has played out, \
is proven wrong, or something clearly better appears.
- Every purchase should carry an exit plan sized for days or weeks: `take_profit_price` where the thesis has paid \
(typically 6 to 20% away) and `stop_loss_price` where it is clearly wrong (typically 5 to 10% away). The account \
then sells the instant either level is reached, with no call to you and no thinking cost. Move them with \
`exit_plans` as the thesis develops. Set `expected_holding_days` on each purchase.
- Sector and thematic funds (semiconductors, energy, banks, ...) are a good way to bet on an industry-wide effect. \
- Use the evidence beyond price. `calendar` gives the next earnings date and analyst ratings when available: decide \
deliberately whether to hold through a report. `options` shows the move the options market expects. `crowd` is \
retail chatter (a warning of crowded trades, rarely a reason to buy). `rule_track_record` shows how simple price \
rules did after costs: weigh any rule-based `signals` by that record.
- Cash earns nothing and the index keeps moving: hold cash only while you have no thesis better than the index, and \
say in `notes` what you are waiting for.
- When a bet has taught you something that will still be true next month, put it in `lesson` (one sentence).
- Mind `spread_pct`: quotes come from one exchange and can be wide. Use limit orders close to `last_trade`.

Rules of the account (enforced by the broker, not negotiable):
- US stocks and funds, long only, whole shares, no leverage. You can only spend cash you have and sell shares you hold.
- {index_rule}
- Orders are accepted only while the market is open. Market orders buy at the ask and sell at the bid of the next \
quote; large orders fill only up to the quoted size and the rest waits.
- Order types: market, limit, stop. Time in force: day or gtc.
- Costs: ${commission} flat commission per executed order, on the purchase and on the sale, so a round trip costs \
${round_trip} whatever the size: {pct_500}% of a $500 position, {pct_2500}% of a $2,500 one. 26% tax on every \
realised gain, taken at the sale; losses become a credit against later gains. 0.2%/year stamp duty. Dividends \
arrive net of 37% tax.
- Your own thinking costs money: every call to you is charged to this account. You are called a few times a day and \
whenever relevant news, a filing or a fill arrives. Use `next_wake_minutes` to look again sooner when something is \
developing.
- You may trade any US ticker. Tickers outside `quotes` have no live price yet: list them in `watch` and they will \
have quotes at your next wake (data slots are limited; unused ones are dropped).

Reply with JSON only, matching exactly:
{{"summary": "<one or two plain sentences a non-expert can understand>",
 "orders": [{{"action": "buy"|"sell", "ticker": "...", "quantity": <whole shares>, "order_type": "market"|"limit"|"stop",
             "limit_price": <number or null>, "stop_price": <number or null>, "tif": "day"|"gtc",
             "take_profit_price": <number or null>, "stop_loss_price": <number or null>,
             "expected_holding_days": <number or null>,
             "rationale": "<the thesis: why this beats holding the index over that horizon, in plain words>",
             "confidence": <0..1>}}],
 "cancel_order_ids": ["..."],
 "alerts": [{{"ticker": "...", "direction": "above"|"below", "price": <number>, "note": "..."}}],
 "exit_plans": [{{"ticker": "...", "take_profit_price": <number or null>, "stop_loss_price": <number or null>}}],
 "watch": ["..."],
 "notes": "<your theses and what you are waiting for, up to 800 characters>",
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
    expected_holding_days: float | None = Field(default=None, gt=0, le=365)
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
        state = self.build_state(ctx)
        calls: list[dict[str, Any]] = []
        # Its own schedule and the regular timer go straight to the decision model: an active trader scans
        # prices on a clock. Outside events (news, filings, moves, fills) pass through the cheap filter first.
        direct = self.is_direct(ctx, reasons)
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
            r = await self.llm.chat(self.decision_model, self.system_prompt(ctx), json.dumps(state), max_tokens=8000,
                                    purpose="decision")
        except LLMError as e:
            return Decision(summary="Decision call failed; holding.", detail={"error": str(e), "llm_calls": calls})
        ctx.charge_tokens(r.usage())
        calls.append(r.usage())
        try:
            dec = self.Reply.model_validate(extract_json(r.text))
        except (ValueError, ValidationError) as e:
            return Decision(summary="The model's reply was not valid; holding.",
                            detail={"invalid_reply": r.text[:2000], "error": str(e)[:500], "llm_calls": calls})
        return self.to_decision(dec, ctx, calls, state)

    # ---- hooks (the crypto trader overrides these)
    Reply = LLMDecision

    def build_state(self, ctx: Context) -> dict[str, Any]:
        return build_state(ctx)

    def is_direct(self, ctx: Context, reasons: set[str]) -> bool:
        return (ctx.last_decision_at is None or (ctx.session is not None and ctx.last_decision_at < ctx.session[0])
                or bool(reasons & {"scheduled", "timer", "simulation_started", "market_open"}))

    def system_prompt(self, ctx: Context) -> str:
        fee = ctx.profile.commission_per_order
        return DECISION_SYSTEM.format(
            index_rule=("You may also hold index funds." if self.allow_index_funds else
                        "You may not buy broad index funds (SPY, QQQ, VOO and similar): holding the benchmark is not "
                        "trading. Individual stocks and sector or thematic funds are allowed."),
            commission=fee, round_trip=fee * 2, pct_500=round(float(fee) * 2 / 500 * 100, 1),
            pct_2500=round(float(fee) * 2 / 2500 * 100, 2))

    def to_decision(self, dec: Any, ctx: Context, calls: list[dict[str, Any]], state: dict[str, Any]) -> Decision:
        news_ids = [n["id"] for n in ctx.news[:40]]
        refused = [o.ticker.upper() for o in dec.orders
                   if o.action == "buy" and not self.allow_index_funds and o.ticker.upper() in INDEX_FUNDS]
        orders = [OrderRequest(o.ticker.upper(), o.action, o.quantity, o.order_type, o.limit_price, o.stop_price, o.tif,
                               meta={"rationale": o.rationale, "confidence": o.confidence,
                                     **({"expected_holding_days": o.expected_holding_days} if o.expected_holding_days else {})})
                  for o in dec.orders
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
