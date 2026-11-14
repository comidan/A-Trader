import asyncio

from fastapi.testclient import TestClient

from tradelab.api.app import create_app
from tradelab.bus import EventBus


def test_status_is_honest_and_leaks_no_secrets(settings):
    with TestClient(create_app(settings)) as c:
        r = c.get("/api/status")
        s = r.json()
        assert s["live_trading"] is False
        assert s["price_feed"]["feed"] == "iex" and s["price_feed"]["full_market"] is False
        assert s["price_feed"]["warning"]
        assert all(f["state"] == "not_started" and f["last_received_at_utc"] is None for f in s["feeds"])
        assert s["credentials"] == {"alpaca": True, "llm": False, "sec_user_agent": False}
        assert "secret-value" not in r.text


def test_simulation_endpoints(settings):
    with TestClient(create_app(settings)) as c:
        a = c.post("/api/simulations", json={"name": "run 1"}).json()
        assert a["starting_capital_usd"] == "5000" and a["state"] == "created"
        assert c.post(f"/api/simulations/{a['id']}/pause").status_code == 409
        assert c.post(f"/api/simulations/{a['id']}/start").json()["state"] == "running"
        b = c.post("/api/simulations", json={"name": "run 2", "starting_capital_usd": "7500.50"}).json()
        assert b["predecessor_id"] == a["id"]
        assert [s["state"] for s in c.get("/api/simulations").json()] == ["created", "ended"]
        assert c.post("/api/simulations", json={"name": "x", "starting_capital_usd": "-1"}).status_code == 422
        assert c.get("/api/ledger/verify").json()["ok"] is True
        kinds = [e["kind"] for e in c.get("/api/ledger?limit=200").json()]
        assert kinds.count("simulation_created") == 2 and "simulation_ended" in kinds


def test_websocket_pushes_status_and_events(settings):
    with TestClient(create_app(settings)) as c:
        with c.websocket_connect("/ws") as ws:
            first = ws.receive_json()
            assert first["type"] == "tick" and first["status"]["active_simulation"] is None
            c.post("/api/simulations", json={"name": "run 1"})
            for _ in range(20):
                msg = ws.receive_json()
                if msg["type"] == "event":
                    break
            assert msg["topic"].startswith(("simulation.", "ledger."))


def test_bus_prefix_filter_and_drop_on_full():
    async def run():
        bus = EventBus()
        news = bus.subscribe("news.", maxsize=1)
        bus.publish("price.AAPL", {})
        bus.publish("news.AAPL", {"n": 1})
        bus.publish("news.AAPL", {"n": 2})
        assert news.queue.qsize() == 1 and news.dropped == 1
        assert (await news.queue.get()).payload == {"n": 1}
    asyncio.run(run())


def test_overview_shows_every_strategy_with_the_same_capital_and_no_invented_numbers(settings):
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/overview").json()["strategies"] == []
        sim = c.post("/api/simulations", json={"name": "run"}).json()
        c.post(f"/api/simulations/{sim['id']}/start")
        o = c.get("/api/overview").json()
        ids = {s["id"] for s in o["strategies"]}
        assert {"buy_hold", "random", "momentum", "mean_reversion"} <= ids
        assert all(s["net_result"] == 0 and s["cash"] == 5000 and s["fills"] == 0 for s in o["strategies"])
        assert o["market"]["state"] == "unknown"                      # no calendar: treated as closed
        assert o["not_running"]["llm_light"].startswith("not running")  # no ICA key in tests
        assert c.get("/api/equity").json()["buy_hold"] == []
        assert c.get("/api/report").json()["headline"] == "No AI agent is running."


def test_operator_controls(settings):
    with TestClient(create_app(settings)) as c:
        assert c.post("/api/kill-switch/on").status_code == 409        # nothing to control yet
        sim = c.post("/api/simulations", json={"name": "run"}).json()
        assert c.post("/api/kill-switch/on").json() == {"kill_switch": True}
        assert c.post("/api/kill-switch/off").json() == {"kill_switch": False}
        s = c.put("/api/settings", json={"max_position_pct": 25, "stop_loss_pct": 8}).json()
        assert s["max_position_pct"] == 25 and s["max_daily_loss_pct"] is None
        assert c.put("/api/settings", json={"max_position_pct": 500}).status_code == 422
        o = c.post("/api/deposit", json={"amount": "1000", "strategy_id": "buy_hold"}).json()
        bh = next(x for x in o["strategies"] if x["id"] == "buy_hold")
        assert bh["cash"] == 6000 and bh["net_result"] == 0
        acts = c.get("/api/activity").json()
        assert {a["kind"] for a in acts} >= {"cash_deposit", "kill_switch", "settings_changed"}
        assert c.get("/api/strategies/buy_hold").json()["positions"] == []
        assert c.get("/api/strategies/nope").status_code == 404


def test_optional_password_protects_api_and_websocket(settings):
    import pytest
    from starlette.websockets import WebSocketDisconnect
    from pydantic import SecretStr
    protected = settings.model_copy(update={"dashboard_password": SecretStr("s3cret")})
    with TestClient(create_app(protected)) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/overview").status_code == 401
        assert c.get("/api/overview", auth=("tradelab", "wrong")).status_code == 401
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws"):
                pass
        assert c.get("/api/overview", auth=("tradelab", "s3cret")).status_code == 200
        assert c.get("/api/overview").status_code == 200           # the session cookie now covers API and websocket
        with c.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "tick"


def test_map_and_watch_endpoints(settings):
    with TestClient(create_app(settings)) as c:
        m = c.get("/api/map").json()
        assert m["nodes"] == [] and m["links"] == []
        assert c.post("/api/watch/ZZZZ").status_code == 409        # asset list not loaded: refused, not guessed
        assert c.post("/api/release/AAPL").json() == {"released": "AAPL"}


def test_strategy_loops_are_actually_running_after_creating_a_simulation_from_the_api(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        sim = c.post("/api/simulations", json={"name": "run"}).json()
        c.post(f"/api/simulations/{sim['id']}/start")
        runners = app.state.svc.engine.runners
        assert set(runners) >= {"buy_hold", "random", "momentum", "mean_reversion"}
        assert all(r.task is not None and not r.task.done() for r in runners.values())
        c.post("/api/simulations", json={"name": "second"})
        assert all(r.task is not None and not r.task.done() for r in app.state.svc.engine.runners.values())
