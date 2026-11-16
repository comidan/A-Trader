"""Crypto strategies. They run around the clock on Alpaca's crypto feed.

Internally crypto is traded in whole units of one millionth of a coin (so the broker's integer arithmetic and
all its guarantees apply unchanged). Everything the model reads or writes is in coins and dollars per coin;
this file converts at the boundary.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..market import NY
from ..models import CRYPTO_LOT as LOT
from ..models import OrderRequest
from .base import Context, Decision, Strategy
from .llm_agent import LLMAlert, LLMLightAgent

BENCHMARK = "BTC/USD"


def units(coins: Decimal) -> int:
    return int(coins / LOT)


def coins(u: int) -> float:
    return float(u * LOT)


def per_coin(unit_price: Any) -> float:
    return round(float(Decimal(str(unit_price)) / LOT), 6)


class CryptoBuyAndHold(Strategy):
    kind, name, market = "crypto_buy_hold", "Buy & hold Bitcoin", "crypto"
    description = "Buys Bitcoin once and never sells. This is the bar the crypto AI trader has to beat."
    interval_s = 60.0

    async def decide(self, ctx: Context) -> Decision | None:
        q = ctx.quote(BENCHMARK)
        if ctx.open_orders or q is None or ctx.account.qty(BENCHMARK):
            return None
        qty = int(ctx.available_cash / (q.ask * (1 + ctx.profile.crypto_fee_rate)) * Decimal("0.999"))
        if qty < 1:
            return None
        return Decision([OrderRequest(BENCHMARK, "buy", qty, tif="gtc", meta={"rationale": "Put all cash in Bitcoin and hold."})],
                        summary=f"Buy {coins(qty):.6f} BTC and hold")


SYSTEM = """You are a professional active crypto trader running a simulated account that mirrors a real one. \\
Your job is to beat simply holding Bitcoin, by buying and selling crypto. You are measured on one number: the \\
account's net liquidation value (everything sold, every cost paid) compared with buying Bitcoin at the start and \\
holding it. Matching Bitcoin is a failure.

The market never closes. You are called every {interval} minutes, day and night, and when something happens \\
(a sharp move, a fill, relevant news). You decide alone; there is no approval step and no limit on trades.

How to work:
- Scan the prices each time. `intraday` shows the last traded price, the move over 5, 15 and 60 minutes and the \\
position in the last day's range; `history` and `technical` show the bigger trend. Trade price action as well \\
as news. Compare every coin with Bitcoin: hold what you expect to beat it.
- Fees are a percentage, so frequent trading is possible, but not free: {fee_pct}% of every buy and every sell, \\
so a round trip costs {round_trip_pct}% before tax. A trade needs an expected move clearly larger than that. \\
A take-profit of 1.5 to 5% with a stop about half as far away is a normal short-term trade.
- Every purchase should carry `take_profit_price` and `stop_loss_price`: the account then sells the instant either \\
level is reached, with no call to you and no thinking cost. Use `exit_plans` to move them on existing holdings.
- Mind `spread_pct`: avoid coins whose quote is wide; use limit orders close to `last_trade`.
- You can hold cash (dollars) when you expect the whole market to fall: that is how you beat Bitcoin in a decline.
- Use `notes` as your trading journal; it is your only memory between calls. Put durable lessons in `lesson`.

Rules of the account (enforced, not negotiable):
- Only the pairs in `quotes`, long only, no leverage. Fractions of a coin are allowed (down to 0.000001).
- Orders buy at the ask and sell at the bid of the next quote, up to the size on offer; the rest waits.
- Order types: market, limit, stop. Orders stay until filled or cancelled.
- Tax: 33% of every realised gain, taken at the sale; losses become a credit against later crypto gains.
- Your own thinking costs money, charged to this account at every call; with a call every {interval} minutes that \\
adds up over a day. Choose `next_wake_minutes` accordingly.

Reply with JSON only, matching exactly:
{{"summary": "<one or two plain sentences a non-expert can understand>",
 "orders": [{{"action": "buy"|"sell", "pair": "BTC/USD", "quantity": <coins, may be fractional>,
             "order_type": "market"|"limit"|"stop", "limit_price": <dollars per coin or null>,
             "stop_price": <dollars per coin or null>, "take_profit_price": <number or null>,
             "stop_loss_price": <number or null>, "rationale": "<why this beats holding Bitcoin>", "confidence": <0..1>}}],
 "cancel_order_ids": ["..."],
 "exit_plans": [{{"pair": "...", "take_profit_price": <number or null>, "stop_loss_price": <number or null>}}],
 "alerts": [{{"ticker": "<pair>", "direction": "above"|"below", "price": <number>, "note": "..."}}],
 "notes": "<your trading journal, up to 800 characters>",
 "lesson": "<one durable lesson, or null>",
 "next_wake_minutes": <number between 1 and 1440, or null for the default>}}"""


class CryptoOrder(BaseModel):
    action: Literal["buy", "sell"]
    pair: str
    quantity: Decimal = Field(gt=0)
    order_type: Literal["market", "limit", "stop"] = "market"
    limit_price: Decimal | None = Field(default=None, gt=0)
    stop_price: Decimal | None = Field(default=None, gt=0)
    take_profit_price: Decimal | None = Field(default=None, gt=0)
    stop_loss_price: Decimal | None = Field(default=None, gt=0)
    rationale: str = ""
    confidence: float = Field(default=0.5, ge=0, le=1)


class CryptoExit(BaseModel):
    pair: str
    take_profit_price: Decimal | None = Field(default=None, gt=0)
    stop_loss_price: Decimal | None = Field(default=None, gt=0)


class CryptoReply(BaseModel):
    summary: str = ""
    orders: list[CryptoOrder] = []
    cancel_order_ids: list[str] = []
    exit_plans: list[CryptoExit] = []
    alerts: list[LLMAlert] = []
    notes: str | None = Field(default=None, max_length=2000)
    lesson: str | None = Field(default=None, max_length=600)
    next_wake_minutes: float | None = Field(default=None, ge=1, le=1440)


class CryptoAITrader(LLMLightAgent):
    kind, name, market = "crypto_ai", "Crypto AI trader", "crypto"
    description = ("An AI model trades crypto around the clock, on prices and news, and has to beat simply holding "
                   "Bitcoin. Fees are a percentage of each trade, so frequent trading is possible.")
    Reply = CryptoReply

    def is_direct(self, ctx: Context, reasons: set[str]) -> bool:
        return ctx.last_decision_at is None or bool(reasons & {"scheduled", "timer", "simulation_started"})

    def system_prompt(self, ctx: Context) -> str:
        fee = float(ctx.profile.crypto_fee_rate) * 100
        return SYSTEM.format(interval=int(self.interval_s / 60), fee_pct=fee, round_trip_pct=round(fee * 2, 2))

    def build_state(self, ctx: Context) -> dict[str, Any]:
        a, v, fee = ctx.account, ctx.valuation, ctx.profile.crypto_fee_rate
        positions = []
        for sym, pos in a.positions.items():
            q = ctx.quote(sym)
            positions.append({"pair": sym, "coins": coins(pos.qty), "avg_cost_per_coin": per_coin(pos.avg_cost),
                              "bid": per_coin(q.bid) if q else None,
                              "unrealised_usd": round(float(pos.qty * q.bid - pos.basis), 2) if q else None,
                              "break_even_bid_after_exit_fee": per_coin(pos.basis / pos.qty / (1 - fee))})
        quotes = {}
        for sym in ctx.universe:
            q = ctx.quote(sym)
            if not q:
                continue
            row: dict[str, Any] = {"bid": per_coin(q.bid), "ask": per_coin(q.ask), "ask_size_coins": coins(q.ask_size),
                                   "spread_pct": round(float((q.ask - q.bid) / ((q.ask + q.bid) / 2)) * 100, 3)}
            intra = self._intraday(ctx, sym)
            if intra:
                row["intraday"] = intra
            hist, tech = ctx.market.history(sym), ctx.market.technical(sym)
            if hist:
                row["history"] = {k: (per_coin(x) if k == "last_close" else x) for k, x in hist.items() if not k.startswith("vs_spy")}
            if tech:
                row["technical"] = tech
            quotes[sym] = row
        news = [{"id": n["id"], "minutes_ago": int((ctx.now.timestamp() - _ts(n["recv_ts"])) / 60), "source": n["source"],
                 "about": n["symbols"][:6], "impact": n["impact"], "headline": n["headline"][:220],
                 **({"text": n["extra"]["text"][:1500]} if (n.get("extra") or {}).get("text") and i < 3 else {})}
                for i, n in enumerate(ctx.news[:30])]
        return {
            "time_utc": ctx.now.strftime("%Y-%m-%d %H:%M"), "time_new_york": ctx.now.astimezone(NY).strftime("%a %H:%M"),
            "woken_by": [{"reason": w["reason"], **({"about": w["data"]} if w.get("data") else {})} for w in ctx.wakes][:12],
            "account": {"cash_usd": round(float(a.cash), 2), "cash_available_for_new_orders": round(float(ctx.available_cash), 2),
                        "net_liquidation_value": round(float(v["net_liquidation"]), 2), "money_put_in": round(float(a.deposits), 2),
                        "net_result": round(float(v["net_result"]), 2), "positions": positions,
                        "costs_paid_so_far": {"trading_fees": round(float(a.commissions), 2), "taxes": round(float(a.taxes), 2),
                                              "your_thinking_tokens": round(float(a.token_cost), 4)},
                        "tax_loss_credit": round(float(a.loss_credit(ctx.market.tax_year(ctx.now), "crypto")), 2)},
            "benchmark": None if ctx.benchmark_net_result is None else {
                "buy_and_hold_bitcoin_net_result": round(float(ctx.benchmark_net_result), 2),
                "you_minus_benchmark": round(float(v["net_result"] - ctx.benchmark_net_result), 2)},
            "open_orders": [{"id": o.id, "pair": o.symbol, "side": o.side, "coins_left": coins(o.remaining), "type": o.type,
                             "limit": per_coin(o.limit_price) if o.limit_price else None,
                             "stop": per_coin(o.stop_price) if o.stop_price else None} for o in ctx.open_orders],
            "exit_plans_running_without_you": [{"pair": p["symbol"],
                                                "take_profit_price": per_coin(p["take_profit"]) if p.get("take_profit") else None,
                                                "stop_loss_price": per_coin(p["stop_loss"]) if p.get("stop_loss") else None}
                                               for p in ctx.exit_plans],
            "alerts": [{"pair": al["symbol"], "direction": al["direction"], "price": per_coin(al["price"])} for al in ctx.alerts],
            "quotes": quotes, "news_since_your_last_decision": news,
            "your_recent_trades": [{**t, "coins": coins(t.pop("shares")), "price": per_coin(t["price"])} for t in
                                   [dict(x) for x in ctx.recent_trades]],
            "your_notes": ctx.notes, "your_lessons": ctx.lessons,
        }

    @staticmethod
    def _intraday(ctx: Context, sym: str) -> dict[str, Any] | None:
        """Last 24 hours of 1-minute bars (crypto has no session)."""
        from datetime import timedelta
        bars = [b for b in ctx.market.bars.get(sym, ())]
        if len(bars) < 3:
            return None
        last = bars[-1]["c"]

        def chg(minutes: int) -> float | None:
            past = [b for b in bars if b["recv_ts"] <= ctx.now - timedelta(minutes=minutes)]
            return round(float(last / past[-1]["c"] - 1) * 100, 2) if past and past[-1]["c"] > 0 else None

        hi, lo = max(b["h"] for b in bars), min(b["l"] for b in bars)
        return {"last_trade": per_coin(last), "pct_5m": chg(5), "pct_15m": chg(15), "pct_60m": chg(60),
                "recent_high": per_coin(hi), "recent_low": per_coin(lo),
                "position_in_recent_range_pct": round(float((last - lo) / (hi - lo)) * 100) if hi > lo else None}

    def to_decision(self, dec: CryptoReply, ctx: Context, calls: list[dict[str, Any]], state: dict[str, Any]) -> Decision:
        allowed = set(ctx.universe)
        orders, plans, skipped = [], [], []
        for o in dec.orders:
            pair, qty = o.pair.upper(), units(o.quantity)
            if pair not in allowed or qty < 1:
                skipped.append(pair)
                continue
            orders.append(OrderRequest(pair, o.action, qty, o.order_type, o.limit_price * LOT if o.limit_price else None,
                                       o.stop_price * LOT if o.stop_price else None, "gtc",
                                       meta={"rationale": o.rationale, "confidence": o.confidence}))
            if o.action == "buy" and (o.take_profit_price or o.stop_loss_price):
                plans.append((pair, o.take_profit_price, o.stop_loss_price))
        plans += [(e.pair.upper(), e.take_profit_price, e.stop_loss_price) for e in dec.exit_plans if e.pair.upper() in allowed]
        return Decision(
            orders=orders, cancel=dec.cancel_order_ids,
            alerts=[{"symbol": al.ticker.upper(), "direction": al.direction, "price": str(al.price * LOT), "note": al.note}
                    for al in dec.alerts if al.ticker.upper() in allowed],
            exit_plans=[{"symbol": p, "take_profit": str(tp * LOT) if tp else None, "stop_loss": str(sl * LOT) if sl else None}
                        for p, tp, sl in plans if tp or sl],
            next_wake_minutes=dec.next_wake_minutes, summary=dec.summary,
            notes=dec.notes[:800] if dec.notes else None, lesson=dec.lesson[:300] if dec.lesson else None,
            detail={"llm_calls": calls, "escalated": True, "news_ids": [n["id"] for n in ctx.news[:30]], "state_seen": state,
                    **({"orders_skipped": skipped} if skipped else {})})


def _ts(iso_str: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(iso_str).timestamp()
