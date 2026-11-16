"""A purely rule-based trader: no AI, only a classic tested signal. It is both a competitor for the AI and a
check on it: if arithmetic on prices does as well, the AI's judgement is not adding anything."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from ..models import OrderRequest
from ..signals import entry_signal, exit_signal, rsi
from .base import Context, Decision, Strategy, max_affordable

RULE = "pullback_in_uptrend"


class TechnicalRules(Strategy):
    kind, name = "technical_rules", "Rule-based trader"
    description = ("No AI. Buys strong stocks (above their 200-day average) after a sharp two-day drop and sells when "
                   "they bounce back above their 5-day average. Decides once a day, 20 minutes before the close.")
    interval_s = 60.0
    max_positions = 3

    async def decide(self, ctx: Context) -> Decision | None:
        if ctx.now < ctx.session[1] - timedelta(minutes=20) or ctx.open_orders:
            return None
        if ctx.last_decision_at and ctx.last_decision_at >= ctx.session[1] - timedelta(minutes=20):
            return None                                      # already acted today

        def closes(sym: str) -> list[float] | None:
            """Completed daily closes plus today's last real trade as the provisional close."""
            hist, last = ctx.market.daily.get(sym), ctx.market.last_trade(sym)
            return hist + [float(last)] if hist and len(hist) >= 200 and last is not None else None

        orders = []
        for sym in list(ctx.account.positions):
            c = closes(sym)
            if c and exit_signal(RULE, c) and ctx.quote(sym):
                orders.append(OrderRequest(sym, "sell", ctx.account.qty(sym), meta={
                    "rationale": "Rule: the stock closed back above its 5-day average, so the bounce has happened."}))
        if orders:
            return Decision(orders, summary="Rule-based exit")
        room = self.max_positions - len(ctx.account.positions)
        if room <= 0:
            return None
        picks = []
        for sym in ctx.universe:
            c = closes(sym)
            if c and not ctx.market.is_etf(sym) and not ctx.account.qty(sym) and ctx.quote(sym) and entry_signal(RULE, c):
                picks.append((rsi(c, 2), sym))
        for score, sym in sorted(picks)[:room]:
            qty = max_affordable(ctx.available_cash / Decimal(room), ctx.quote(sym).ask, ctx.profile.commission_per_order)
            if qty >= 1:
                orders.append(OrderRequest(sym, "buy", qty, meta={
                    "rationale": f"Rule: in a long-term uptrend and sharply oversold (2-day RSI {score})."}))
        return Decision(orders, summary="Rule-based entry") if orders else None
