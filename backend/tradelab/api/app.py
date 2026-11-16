"""HTTP + websocket API. Nothing here can place a real order."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
from contextlib import asynccontextmanager, suppress
from decimal import Decimal
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import __version__
from ..config import Settings
from ..crosscheck import summary as crosscheck_summary
from ..logging_setup import setup_logging
from ..replay import run_replay
from ..services import Services
from ..simulations import SimulationError

log = logging.getLogger(__name__)

FEED_NOTES = {
    "iex": "Prices come from IEX only (about 4% of US trading). Spreads look wider and available sizes smaller than "
           "in the real market, so results are pessimistic, and more so for strategies that trade often.",
    "sip": "",
}


class NewSimulation(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    starting_capital_usd: Decimal | None = Field(default=None, gt=0)


class RiskSettings(BaseModel):
    max_position_pct: float | None = Field(default=None, gt=0, le=100)
    max_daily_loss_pct: float | None = Field(default=None, gt=0, le=100)
    stop_loss_pct: float | None = Field(default=None, gt=0, lt=100)
    daily_token_budget_usd: float | None = Field(default=None, gt=0)
    extra_latency_ms: int = Field(default=0, ge=0, le=60000)
    slippage_bps: float = Field(default=0, ge=0, le=500)


class Deposit(BaseModel):
    amount: Decimal = Field(gt=0)
    strategy_id: str | None = None


class Replay(BaseModel):
    start: str
    end: str


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    setup_logging(settings.log_level)
    svc = Services(settings)

    def status() -> dict[str, Any]:
        st = svc.status()
        st["version"] = __version__
        st["price_feed"]["warning"] = FEED_NOTES[settings.alpaca_feed]
        st["active_simulation"] = svc.engine.sim if svc.engine.sim else svc.sims.active()
        return st

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("startup", extra={"version": __version__, "feed": settings.alpaca_feed,
                                   "credentials": settings.credentials_present()})
        await svc.start()
        yield
        await svc.stop()

    # Every endpoint that touches the engine is `async`: it runs on the event loop, the same thread that
    # processes quotes and strategy decisions, so simulation state is never touched from two threads.
    app = FastAPI(title="TradeLab", version=__version__, lifespan=lifespan)
    app.state.svc = svc

    # Optional login: one shared password (user name "tradelab"). A session cookie covers the websocket.
    password = settings.dashboard_password.get_secret_value()
    token = hmac.new(password.encode(), b"tradelab-session", hashlib.sha256).hexdigest() if password else ""

    def authorised(headers, cookies) -> bool:
        if not password or hmac.compare_digest(cookies.get("tl_session", ""), token):
            return True
        auth = headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                user, _, given = base64.b64decode(auth[6:]).decode().partition(":")
                return user == "tradelab" and hmac.compare_digest(given, password)
            except Exception:
                return False
        return False

    @app.middleware("http")
    async def login(request: Request, call_next):
        if request.url.path == "/api/health":            # container health check: no login, and it grants nothing
            return await call_next(request)
        if authorised(request.headers, request.cookies):
            response = await call_next(request)
            if password and request.cookies.get("tl_session") != token:
                response.set_cookie("tl_session", token, httponly=True, samesite="strict", max_age=30 * 86400)
            return response
        return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="TradeLab"'})

    def need_sim() -> None:
        if not svc.engine.sim:
            raise HTTPException(409, "no simulation: create one first")

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/status")
    async def get_status() -> dict[str, Any]:
        return status()

    @app.get("/api/overview")
    async def overview() -> dict[str, Any]:
        return svc.overview()

    @app.get("/api/equity")
    async def equity() -> dict[str, Any]:
        return svc.equity_curves()

    @app.get("/api/strategies/{sid}")
    async def strategy(sid: str) -> dict[str, Any]:
        if sid not in svc.engine.state.accounts:
            raise HTTPException(404, "unknown strategy")
        return svc.strategy_detail(sid)

    @app.get("/api/activity")
    async def activity(limit: int = 60, strategy_id: str | None = None) -> list[dict[str, Any]]:
        return svc.activity(min(max(limit, 1), 300), strategy_id)

    @app.get("/api/news")
    async def news(limit: int = 60) -> list[dict[str, Any]]:
        return svc.store.news(min(max(limit, 1), 300))

    @app.get("/api/quotes")
    async def quotes() -> list[dict[str, Any]]:
        return svc.quotes()

    @app.get("/api/map")
    async def relationship_map() -> dict[str, Any]:
        return svc.relationship_map()

    @app.post("/api/watch/{symbol}")
    async def watch(symbol: str) -> dict[str, Any]:
        ok, why = svc.engine.watch(symbol.upper())
        if not ok:
            raise HTTPException(409, why)
        return {"watching": symbol.upper()}

    @app.post("/api/release/{symbol}")
    async def release(symbol: str) -> dict[str, Any]:
        svc.engine.release(symbol.upper())
        return {"released": symbol.upper()}

    @app.get("/api/rules")
    async def rules() -> dict[str, Any]:
        return {"record": svc.market.rule_record,
                "lessons": {sid: r.lessons for sid, r in svc.engine.runners.items() if r.lessons},
                "research": {"options": len(svc.market.options), "crowd": len(svc.market.social), "calendar": len(svc.market.events),
                             "finnhub_key_set": bool(settings.finnhub_api_key.get_secret_value())}}

    @app.get("/api/report")
    async def report() -> dict[str, Any]:
        return svc.report()

    @app.get("/api/reports")
    async def reports() -> list[dict[str, Any]]:
        return svc.store.query("SELECT * FROM reports ORDER BY created_ts DESC LIMIT 52")

    @app.get("/api/crosscheck")
    async def crosscheck() -> dict[str, Any]:
        return crosscheck_summary(svc.store, (svc.engine.sim or {}).get("id"))

    # ---- operator controls
    @app.get("/api/simulations")
    async def list_simulations() -> list[dict[str, Any]]:
        return svc.sims.list()

    @app.post("/api/simulations", status_code=201)
    async def new_simulation(body: NewSimulation) -> dict[str, Any]:
        try:
            sim = svc.sims.create(body.name, body.starting_capital_usd or settings.starting_capital_usd, "forward",
                                  {"price_feed": settings.alpaca_feed, "broker": svc.engine.profile.name,
                                   "strategies": settings.strategies})
        except SimulationError as e:
            raise HTTPException(400, str(e))
        svc.engine.refresh_sim()
        svc.bus.publish("simulation.changed", {"id": sim["id"]})
        return sim

    @app.post("/api/simulations/{sim_id}/{action}")
    async def simulation_action(sim_id: str, action: Literal["start", "pause", "resume", "end"]) -> dict[str, Any]:
        try:
            sim = svc.sims.transition(sim_id, action)
        except SimulationError as e:
            raise HTTPException(409, str(e))
        svc.engine.refresh_sim()
        svc.bus.publish("simulation.changed", {"id": sim_id})
        return sim

    @app.put("/api/settings")
    async def put_settings(body: RiskSettings) -> dict[str, Any]:
        need_sim()
        return svc.engine.set_settings(body.model_dump())

    @app.post("/api/kill-switch/{state}")
    async def kill_switch(state: Literal["on", "off"]) -> dict[str, bool]:
        need_sim()
        svc.engine.set_kill_switch(state == "on")
        return {"kill_switch": svc.engine.state.kill_switch}

    @app.post("/api/deposit")
    async def deposit(body: Deposit) -> dict[str, Any]:
        need_sim()
        if body.strategy_id and body.strategy_id not in svc.engine.state.accounts:
            raise HTTPException(404, "unknown strategy")
        svc.engine.deposit(body.strategy_id, body.amount)
        return svc.overview()

    @app.post("/api/replay")
    async def replay(body: Replay) -> dict[str, Any]:
        return await run_replay(settings, svc.store, svc.market, svc.make_strategy, body.start, body.end,
                                settings.starting_capital_usd)

    @app.get("/api/ledger")
    async def get_ledger(limit: int = 50, sim_id: str | None = None) -> list[dict[str, Any]]:
        return [e.as_dict() for e in svc.ledger.tail(min(max(limit, 1), 500), sim_id)]

    @app.get("/api/ledger/verify")
    def verify_ledger() -> dict[str, Any]:
        return svc.ledger.verify()

    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        if not authorised(sock.headers, sock.cookies):
            await sock.close(code=4401)
            return
        await sock.accept()
        sub = svc.bus.subscribe()

        def snapshot() -> dict[str, Any]:
            return {"type": "tick", "status": status(), "overview": svc.overview()}

        async def push_events() -> None:
            while True:
                ev = await sub.queue.get()
                await sock.send_json({"type": "event", "topic": ev.topic, "payload": ev.payload})

        task = asyncio.create_task(push_events())
        try:
            while True:
                await sock.send_json(snapshot())
                await asyncio.sleep(settings.status_push_seconds)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
            svc.bus.unsubscribe(sub)

    return app
