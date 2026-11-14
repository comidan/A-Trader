"""HTTP + websocket API. Nothing here can place a real order."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from decimal import Decimal
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from .. import __version__
from ..bus import EventBus
from ..clock import iso, now_utc
from ..config import Settings
from ..feeds import FeedRegistry, FeedStatus
from ..logging_setup import setup_logging
from ..simulations import SimulationError, Simulations
from ..storage.db import Database, Ledger

log = logging.getLogger(__name__)

FEED_NOTES = {
    "iex": "IEX feed: prices and sizes are pessimistic versus the real market and the bias falls on active "
           "strategies, not on buy & hold. Volume in bars is about 4% of the real figure.",
    "sip": "",
}


class NewSimulation(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    starting_capital_usd: Decimal | None = Field(default=None, gt=0)
    mode: Literal["forward", "replay"] = "forward"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    setup_logging(settings.log_level)

    db = Database(settings.db_path)
    ledger = Ledger(db)
    sims = Simulations(db, ledger)
    bus = EventBus()
    feeds = FeedRegistry()
    pending = "ingestion not built yet"
    feeds.register(FeedStatus("alpaca_quotes", f"Alpaca {settings.alpaca_feed.upper()} websocket", detail=f"{pending} (M1)"))
    feeds.register(FeedStatus("alpaca_news", "Alpaca news websocket (Benzinga)", detail=f"{pending} (M4)"))
    feeds.register(FeedStatus("sec_edgar", "SEC EDGAR latest filings", detail=f"{pending} (M4)"))
    feeds.register(FeedStatus("gdelt", "GDELT DOC 2.0", detail=f"{pending} (M4)"))

    def status() -> dict[str, Any]:
        return {
            "server_time_utc": iso(now_utc()),
            "version": __version__,
            "price_feed": {
                "provider": "alpaca", "feed": settings.alpaca_feed,
                "full_market": settings.alpaca_feed == "sip", "warning": FEED_NOTES[settings.alpaca_feed],
            },
            "market": {"state": "unknown", "detail": "market calendar arrives in M1"},
            "feeds": feeds.snapshot(),
            "credentials": settings.credentials_present(),
            "active_simulation": sims.active(),
            "ledger_entries": ledger.count(),
            "live_trading": False,
        }

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("startup", extra={"version": __version__, "feed": settings.alpaca_feed,
                                   "credentials": settings.credentials_present()})
        yield
        db.close()

    app = FastAPI(title="TradeLab", version=__version__, lifespan=lifespan)
    app.state.settings, app.state.db, app.state.ledger = settings, db, ledger
    app.state.sims, app.state.bus, app.state.feeds = sims, bus, feeds

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/status")
    def get_status() -> dict[str, Any]:
        return status()

    @app.get("/api/simulations")
    def list_simulations() -> list[dict[str, Any]]:
        return sims.list()

    @app.post("/api/simulations", status_code=201)
    def new_simulation(body: NewSimulation) -> dict[str, Any]:
        try:
            sim = sims.create(body.name, body.starting_capital_usd or settings.starting_capital_usd, body.mode,
                              {"price_feed": settings.alpaca_feed})
        except SimulationError as e:
            raise HTTPException(400, str(e))
        log.info("simulation_created", extra={"sim_id": sim["id"], "predecessor_id": sim["predecessor_id"]})
        bus.publish("simulation.changed", sim)
        return sim

    @app.post("/api/simulations/{sim_id}/{action}")
    def simulation_action(sim_id: str, action: Literal["start", "pause", "resume", "end"]) -> dict[str, Any]:
        try:
            sim = sims.transition(sim_id, action)
        except SimulationError as e:
            raise HTTPException(409, str(e))
        log.info("simulation_" + action, extra={"sim_id": sim_id})
        bus.publish("simulation.changed", sim)
        return sim

    @app.get("/api/ledger")
    def get_ledger(limit: int = 50, sim_id: str | None = None) -> list[dict[str, Any]]:
        return [e.as_dict() for e in ledger.tail(min(max(limit, 1), 500), sim_id)]

    @app.get("/api/ledger/verify")
    def verify_ledger() -> dict[str, Any]:
        return ledger.verify()

    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        sub = bus.subscribe()

        async def push_events() -> None:
            while True:
                ev = await sub.queue.get()
                await sock.send_json({"type": "event", "topic": ev.topic, "payload": ev.payload})
                await sock.send_json({"type": "status", "payload": await asyncio.to_thread(status)})

        task = asyncio.create_task(push_events())
        try:
            while True:
                await sock.send_json({"type": "status", "payload": await asyncio.to_thread(status)})
                await asyncio.sleep(settings.status_push_seconds)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
            bus.unsubscribe(sub)

    return app
