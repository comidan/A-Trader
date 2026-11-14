"""Historical replay, for DEBUGGING ONLY.

It replays the quotes and bars this installation has recorded through the same
engine and broker, with the non-LLM baselines only. It is not valid for LLM
agents: a model may already know what happened after the replayed dates.
Recorded quotes are conflated to one per symbol per second, so a replay is
coarser than the live run. Results go to a throw-away in-memory ledger.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from .bus import EventBus
from .config import Settings
from .engine import Engine
from .ingest.alpaca import next_seq
from .market import MarketState
from .models import Quote
from .simulations import Simulations
from .storage.db import Database, Ledger
from .storage.market_store import MarketStore

LABEL = "REPLAY: debugging only. Not valid for LLM agents (the model may know the past)."


async def run_replay(settings: Settings, store: MarketStore, live_market: MarketState, factory, start: str, end: str,
                     capital: Decimal) -> dict[str, Any]:
    db = Database(":memory:")
    ledger = Ledger(db)
    sims = Simulations(db, ledger)
    sim = sims.create(f"replay {start}..{end}", capital, mode="forward", config={"replay": True})
    sims.transition(sim["id"], "start")
    market = MarketState(settings.quote_stale_seconds)
    market.assets, market.calendar = live_market.assets, live_market.calendar
    clock = {"now": datetime.fromisoformat(start)}
    market.calendar_fetched_at = clock["now"]
    replay_settings = settings.model_copy(update={"strategies": "buy_hold,random,momentum,mean_reversion"})
    engine = Engine(replay_settings, ledger, sims, EventBus(), market, MarketStore(":memory:"), factory,
                    clock=lambda: clock["now"], autostart=False)
    engine.load()
    rows = store.query("SELECT 'q' k, symbol, recv_ts, bid a, bid_size b, ask c, ask_size d, NULL e FROM quotes "
                       "WHERE recv_ts >= ? AND recv_ts < ? UNION ALL "
                       "SELECT 'b', symbol, recv_ts, o, h, l, c, v FROM bars WHERE recv_ts >= ? AND recv_ts < ? "
                       "ORDER BY recv_ts", (start, end, start, end))
    last_step = None
    for r in rows:
        now = datetime.fromisoformat(r["recv_ts"])
        clock["now"] = now
        market.calendar_fetched_at = now
        if r["k"] == "q":
            engine.on_quote(Quote(r["symbol"], Decimal(r["a"]), r["b"], Decimal(r["c"]), r["d"], now, next_seq(now), "replay"))
        else:
            engine.on_bar(r["symbol"], {"t": r["recv_ts"], "recv_ts": now, "o": Decimal(r["a"]), "h": Decimal(r["b"]),
                                        "l": Decimal(r["c"]), "c": Decimal(r["d"]), "v": r["e"]}, "replay")
        if last_step is None or now - last_step >= timedelta(seconds=60):
            last_step = now
            engine.tick(now)
            if market.is_open(now):
                for runner in engine.runners.values():
                    await engine.step(runner, [{"reason": "timer", "data": None}])
    out = []
    for sid, a in engine.state.accounts.items():
        v = engine.value(sid)
        out.append({"id": sid, "name": engine.state.strategies[sid]["name"], "net_result": str(v["net_result"]),
                    "gross_result": str(v["gross_result"]), "fills": a.fills, "costs": str(a.costs)})
    db.close()
    return {"label": LABEL, "start": start, "end": end, "events_replayed": len(rows), "strategies": out}
