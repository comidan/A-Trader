"""Trivial baselines the AI has to beat. They pay exactly the same costs as every other strategy."""
from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal

from ..models import OrderRequest
from .base import Context, Decision, Strategy, max_affordable


class BuyAndHold(Strategy):
    kind, name = "buy_hold", "Buy & hold S&P 500"
    description = ("Buys the S&P 500 fund (SPY) once and never sells. This is the bar to beat. "
                   "SPY is simulated as purchasable; an Italian retail account would need a European equivalent.")
    interval_s = 60.0
    symbol = "SPY"

    async def decide(self, ctx: Context) -> Decision | None:
        if ctx.open_orders:
            return None
        q = ctx.quote(self.symbol)
        if q is None:
            return None
        if ctx.account.qty(self.symbol) and ctx.available_cash < ctx.valuation["equity"] * Decimal("0.05"):
            return None                       # leftover cash too small to be worth another commission
        qty = max_affordable(ctx.available_cash, q.ask, ctx.profile.commission_per_order)
        if qty < 1:
            return None
        return Decision([OrderRequest(self.symbol, "buy", qty, meta={"rationale": "Invest all cash in the S&P 500 and hold."})],
                        summary=f"Buy {qty} {self.symbol} and hold")


class RandomTrader(Strategy):
    kind, name = "random", "Random"
    description = "Buys and sells at random. If the AI cannot beat this, it has no skill."
    interval_s = 1800.0

    def __init__(self, seed: int = 0) -> None:
        self.rng = random.Random(seed)

    async def decide(self, ctx: Context) -> Decision | None:
        if ctx.open_orders or self.rng.random() < 0.5:
            return None
        held = sorted(ctx.account.positions)
        if held and self.rng.random() < 0.5:
            sym = self.rng.choice(held)
            if ctx.quote(sym) is None:
                return None
            return Decision([OrderRequest(sym, "sell", ctx.account.qty(sym), meta={"rationale": "Random sell."})],
                            summary=f"Random: sell {sym}")
        candidates = [s for s in ctx.universe if not ctx.market.is_etf(s) and ctx.quote(s)]
        if not candidates:
            return None
        sym = self.rng.choice(candidates)
        qty = max_affordable(ctx.available_cash * Decimal("0.25"), ctx.quote(sym).ask, ctx.profile.commission_per_order)
        if qty < 1:
            return None
        return Decision([OrderRequest(sym, "buy", qty, meta={"rationale": "Random buy."})], summary=f"Random: buy {sym}")


class _DailyRanker(Strategy):
    """Once a day, 30 minutes before the close, hold the 3 stocks picked by today's move."""
    interval_s = 60.0
    hold = 3
    pick_winners = True

    def __init__(self) -> None:
        self.day = None
        self.targets: list[str] = []

    async def decide(self, ctx: Context) -> Decision | None:
        window_start = ctx.session[1] - timedelta(minutes=30)
        if ctx.now < window_start or ctx.open_orders:
            return None
        today = ctx.session[0].date()
        if self.day != today:
            if ctx.last_decision_at and ctx.last_decision_at >= window_start and not ctx.account.positions:
                return None                   # restarted after today's rebalance already started
            moves = {s: ctx.market.session_return(s, ctx.now) for s in ctx.universe
                     if not ctx.market.is_etf(s) and ctx.quote(s)}
            moves = {s: r for s, r in moves.items() if r is not None}
            if len(moves) < self.hold * 2:
                return None                   # not enough real data received today: do nothing
            ranked = sorted(moves, key=moves.get, reverse=self.pick_winners)
            self.day, self.targets = today, ranked[:self.hold]
            self.moves = {s: f"{moves[s] * 100:.2f}%" for s in self.targets}
        why = f"{'Strongest' if self.pick_winners else 'Weakest'} today: " + ", ".join(f"{s} {m}" for s, m in self.moves.items())
        sells = [OrderRequest(s, "sell", ctx.account.qty(s), meta={"rationale": f"No longer in today's picks. {why}"})
                 for s in ctx.account.positions if s not in self.targets and ctx.quote(s)]
        if sells:
            return Decision(sells, summary="Sell yesterday's picks", detail={"targets": self.targets})
        missing = [s for s in self.targets if not ctx.account.qty(s) and ctx.quote(s)]
        buys = []
        for s in missing:
            qty = max_affordable(ctx.available_cash / len(missing), ctx.quote(s).ask, ctx.profile.commission_per_order)
            if qty >= 1:
                buys.append(OrderRequest(s, "buy", qty, meta={"rationale": why}))
        if not buys:
            return None
        return Decision(buys, summary="Buy today's picks", detail={"targets": self.targets})


class Momentum(_DailyRanker):
    kind, name = "momentum", "Trend follower"
    description = "Each afternoon buys the 3 stocks that rose the most today, betting the trend continues."
    pick_winners = True


class MeanReversion(_DailyRanker):
    kind, name = "mean_reversion", "Dip buyer"
    description = "Each afternoon buys the 3 stocks that fell the most today, betting they bounce back."
    pick_winners = False
