"""Multi-agent LLM strategy: TradingAgents (TauricResearch, Apache-2.0), used as an optional dependency.

Each run analyses one ticker with a team of LLM agents and returns a rating.
We track the cost and duration of every run and charge the tokens to this
strategy's account. A run whose token usage cannot be measured places no trade.

Fidelity caveat (shown in the dashboard): TradingAgents fetches its own market
and news data (Yahoo Finance, SEC EDGAR, ...). That data is not stored with
TradeLab receive timestamps. Its orders still go through the same simulated
broker and fill on our live quotes like everyone else's.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import timedelta
from decimal import Decimal
from typing import Any

from ..config import Settings
from ..llm import cost_usd
from ..market import NY
from ..models import OrderRequest
from .base import Context, Decision, Strategy, max_affordable

log = logging.getLogger(__name__)


def available() -> bool:
    try:
        import tradingagents  # noqa: F401
        return True
    except Exception:
        return False


def rating_to_action(rating: str) -> str:
    r = (rating or "").upper()
    if "SELL" in r or "UNDERWEIGHT" in r:
        return "sell"
    if "BUY" in r or "OVERWEIGHT" in r:
        return "buy"
    return "hold"


class TradingAgentsStrategy(Strategy):
    kind, name = "tradingagents", "AI team"
    description = ("A team of AI analysts, researchers who debate, a trader and risk managers (the open source "
                   "TradingAgents project). Far more thinking per decision than the single AI trader, so far more "
                   "expensive. It reads its own external data, which TradeLab cannot timestamp.")
    is_llm = True
    interval_s = 300.0
    position_fraction = Decimal("0.20")

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.tickers = [t.strip().upper() for t in settings.tradingagents_tickers.split(",") if t.strip()]
        self.done: dict[str, set[str]] = {}

    def _run(self, ticker: str, day: str, portfolio: dict[str, Any]) -> tuple[str, dict[str, Any], float]:
        from langchain_core.callbacks import get_usage_metadata_callback
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.portfolio import PortfolioContext

        s = self.settings
        os.environ["OPENAI_COMPATIBLE_API_KEY"] = s.ica_api_key.get_secret_value()
        if s.sec_user_agent_email:
            os.environ["SEC_EDGAR_USER_AGENT"] = f"TradeLab {s.sec_user_agent_email}"
        base = os.path.join(os.path.dirname(os.path.abspath(s.db_path)), "tradingagents")
        config = {**DEFAULT_CONFIG, "llm_provider": "openai_compatible", "backend_url": s.llm_openai_base_url,
                  "deep_think_llm": s.llm_decision_model, "quick_think_llm": s.llm_triage_model,
                  "max_debate_rounds": 1, "max_risk_discuss_rounds": 1, "results_dir": os.path.join(base, "logs"),
                  "data_cache_dir": os.path.join(base, "cache"),
                  "memory_log_path": os.path.join(base, "memory", "trading_memory.md")}
        t0 = time.monotonic()
        with get_usage_metadata_callback() as cb:
            graph = TradingAgentsGraph(debug=False, config=config)
            _, rating = graph.propagate(ticker, day, portfolio=PortfolioContext.model_validate(portfolio))
        return str(rating), dict(cb.usage_metadata), time.monotonic() - t0

    async def decide(self, ctx: Context) -> Decision | None:
        day = ctx.now.astimezone(NY).date().isoformat()
        done = self.done.setdefault(day, set())
        if ctx.now < ctx.session[0] + timedelta(minutes=60) or ctx.session[1] - ctx.now < timedelta(minutes=30):
            return None
        if ctx.last_decision_at and ctx.last_decision_at >= ctx.session[0] and not done:
            done.update(self.tickers)                 # restarted after today's runs: do not pay twice
        todo = [t for t in self.tickers if t not in done and ctx.quote(t)]
        budget = ctx.token_budget_left()
        if not todo or ctx.open_orders or (budget is not None and budget <= 0):
            return None
        ticker = todo[0]
        done.add(ticker)
        portfolio = {"cash": float(ctx.available_cash), "currency": "USD",
                     "positions": [{"ticker": s, "quantity": p.qty, "average_price": float(p.avg_cost)}
                                   for s, p in ctx.account.positions.items()]}
        try:
            rating, usage, seconds = await asyncio.to_thread(self._run, ticker, day, portfolio)
        except Exception as e:
            log.exception("tradingagents_run_failed")
            return Decision(summary=f"The AI team's analysis of {ticker} failed; no trade.",
                            detail={"error": f"{type(e).__name__}: {e}"[:500]})
        calls, total = [], Decimal(0)
        for model, u in usage.items():
            usd = cost_usd(self.settings.llm_prices, model.split("/")[-1], int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0)))
            call = {"model": model, "prompt_tokens": int(u.get("input_tokens", 0)),
                    "completion_tokens": int(u.get("output_tokens", 0)), "usd": str(usd),
                    "latency_ms": int(seconds * 1000), "purpose": f"tradingagents:{ticker}"}
            ctx.charge_tokens(call)
            calls.append(call)
            total += usd
        detail = {"ticker": ticker, "rating": rating, "run_seconds": round(seconds, 1), "run_cost_usd": str(total),
                  "llm_calls": calls, "external_data": "fetched by TradingAgents itself; not receive-timestamped by TradeLab"}
        if not calls:
            return Decision(summary=f"The AI team rated {ticker} '{rating}', but the cost of the run could not be "
                                    "measured, so no trade was placed.", detail=detail)
        action, q = rating_to_action(rating), ctx.quote(ticker)
        why = f"AI team rating for {ticker}: {rating}. The analysis took {seconds / 60:.1f} min and cost ${total:.2f}."
        orders = []
        if action == "sell" and ctx.account.qty(ticker):
            orders.append(OrderRequest(ticker, "sell", ctx.account.qty(ticker), meta={"rationale": why}))
        elif action == "buy" and q:
            target = ctx.valuation["equity"] * self.position_fraction - ctx.account.qty(ticker) * q.bid
            qty = max_affordable(min(target, ctx.available_cash), q.ask, ctx.profile.commission_per_order)
            if qty >= 1:
                orders.append(OrderRequest(ticker, "buy", qty, meta={"rationale": why}))
        return Decision(orders, summary=why, detail=detail)
