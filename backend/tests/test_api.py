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
        assert len(c.get("/api/ledger").json()) == 4


def test_websocket_pushes_status_and_events(settings):
    with TestClient(create_app(settings)) as c:
        with c.websocket_connect("/ws") as ws:
            first = ws.receive_json()
            assert first["type"] == "status" and first["payload"]["active_simulation"] is None
            c.post("/api/simulations", json={"name": "run 1"})
            for _ in range(20):
                msg = ws.receive_json()
                if msg["type"] == "event":
                    break
            assert msg["topic"] == "simulation.changed" and msg["payload"]["name"] == "run 1"


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
