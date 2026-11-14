"""End-to-end through the engine with hand-made quotes (tests are the only place where data is not real)."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from tradelab.bus import EventBus
from tradelab.config import Settings
from tradelab.engine import Engine
from tradelab.llm import LLMResult
from tradelab.market import MarketState
from tradelab.models import OrderRequest, Quote
from tradelab.simulations import Simulations
from tradelab.storage.db import Database, Ledger
from tradelab.storage.market_store import MarketStore
from tradelab.strategies.base import Decision, Strategy
from tradelab.strategies.baselines import BuyAndHold
from tradelab.strategies.llm_agent import LLMLightAgent

OPEN = datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc)
ASSETS = [{"symbol": s, "name": n, "exchange": "NASDAQ", "tradable": True, "status": "active"}
          for s, n in [("SPY", "SPDR S&P 500 ETF Trust"), ("AAPL", "Apple Inc."), ("MSFT", "Microsoft Corp")]]


class Scripted(Strategy):
    kind, name = "scripted", "Scripted"

    def __init__(self, rig):
        self.rig, self.script, self.think_s = rig, [], 0

    async def decide(self, ctx):
        self.rig.now += timedelta(seconds=self.think_s)
        if callable(self.script[0]):
            return self.script.pop(0)(ctx)
        return self.script.pop(0)


class FakeLLM:
    configured = True

    def __init__(self, replies):
        self.replies, self.calls = replies, []

    async def chat(self, model, system, user, max_tokens=0, purpose=""):
        self.calls.append((model, purpose, user))
        return LLMResult(self.replies.pop(0), model, 1000, 200, D("0.008"), 900, purpose)


class EngineRig:
    def __init__(self, tmp_path, strategies="scripted", llm=None):
        self.now = OPEN + timedelta(minutes=30)
        self.settings = Settings(_env_file=None, db_path=str(tmp_path / "l.sqlite"), strategies=strategies,
                                 core_symbols="SPY,AAPL,MSFT")
        self.db = Database(self.settings.db_path)
        self.ledger, self.llm = Ledger(self.db), llm
        self.sims = Simulations(self.db, self.ledger)
        self.store = MarketStore(":memory:")
        self.market = MarketState()
        self.market.set_calendar([{"date": "2026-11-16", "open": "09:30", "close": "16:00"}], OPEN)
        self.market.set_assets(ASSETS)
        self.scripted = Scripted(self)
        sim = self.sims.create("t", D("5000"))
        self.sims.transition(sim["id"], "start")
        self.engine = self.new_engine()

    def factory(self, kind, sim):
        return {"scripted": lambda: self.scripted, "buy_hold": BuyAndHold,
                "llm_light": lambda: LLMLightAgent(self.llm, "claude-haiku-5-5", "claude-opus-5-5", 30)}[kind]()

    def new_engine(self):
        e = Engine(self.settings, self.ledger, self.sims, EventBus(), self.market, self.store, self.factory,
                   clock=lambda: self.now)
        e.load()
        return e

    def quote(self, symbol, bid, ask, size=1000, dt=1):
        self.now += timedelta(seconds=dt)
        self.engine.on_quote(Quote(symbol, D(str(bid)), size, D(str(ask)), size, self.now, int(self.now.timestamp() * 1e6), "test"))

    def step(self, sid="scripted", reason="timer"):
        return asyncio.run(self.engine.step(self.engine.runners[sid], [{"reason": reason, "data": None}]))

    def kinds(self, sid=None):
        return [e.kind for e in self.ledger.entries(self.engine.sim["id"], strategy_id=sid)]

    def acct(self, sid="scripted"):
        return self.engine.state.accounts[sid]


@pytest.fixture
def rig(tmp_path):
    return EngineRig(tmp_path)


def buy(symbol="AAPL", qty=10, **kw):
    return Decision([OrderRequest(symbol, "buy", qty, **kw)], summary="test buy")


def test_strategies_start_with_the_same_capital_recorded_in_the_ledger(tmp_path):
    r = EngineRig(tmp_path, strategies="scripted,buy_hold")
    assert r.acct("scripted").cash == r.acct("buy_hold").cash == D("5000")
    assert r.kinds("buy_hold") == ["strategy_registered", "cash_deposit"]


def test_decision_order_fill_are_all_in_the_ledger_with_the_data_seen(rig):
    rig.quote("AAPL", 99.9, 100)
    rig.scripted.script = [buy()]
    rig.step()
    rig.quote("AAPL", 100.1, 100.2)
    assert rig.kinds("scripted")[-3:] == ["decision", "order_accepted", "fill"]
    entries = rig.ledger.entries(rig.engine.sim["id"], strategy_id="scripted")
    decision, accepted, fill = entries[-3:]
    assert decision.payload["quotes_seen"]["AAPL"]["ask"] == "100"
    assert accepted.payload["quote_seen"]["ask"] == "100"
    assert fill.payload["price"] == "100.2" and fill.payload["quote"]["ask"] == "100.2"
    assert fill.payload["meta"]["decision_id"] == decision.payload["id"]


def test_quotes_arriving_while_the_strategy_is_thinking_cannot_be_used(rig):
    rig.quote("AAPL", 99.9, 100)
    seen = {}

    def slow(ctx):
        # a quote arrives 2s into a 5s think: the order did not exist yet, so it must not fill on it
        rig.engine.on_quote(Quote("AAPL", D("98"), 1000, D("98.5"), 1000, rig.now - timedelta(seconds=3), 1, "test"))
        seen["ok"] = True
        return buy()

    rig.scripted.think_s, rig.scripted.script = 5, [slow]
    rig.step()
    assert rig.acct().positions == {}
    rig.quote("AAPL", 101, 101.5)
    fill = rig.ledger.entries(rig.engine.sim["id"], ("fill",))[0]
    assert fill.payload["price"] == "101.5"
    assert rig.ledger.entries(rig.engine.sim["id"], ("decision",))[0].payload["thinking_ms"] == 5000


def test_restart_rebuilds_identical_state_from_the_ledger(rig):
    rig.quote("AAPL", 99.9, 100)
    rig.scripted.script = [buy(qty=30), Decision([OrderRequest("AAPL", "sell", 10, "stop", stop_price=D("90"), tif="gtc")])]
    rig.step()
    rig.quote("AAPL", 100, 100.1, size=20)          # partial fill, 10 left open
    rig.step()
    before = (rig.acct(), {k: o.to_dict() for k, o in rig.engine.state.orders.items()})
    restarted = rig.new_engine()
    assert restarted.state.accounts["scripted"] == before[0]
    assert {k: o.to_dict() for k, o in restarted.state.orders.items()} == before[1]
    rig.engine = restarted
    rig.quote("AAPL", 100, 100.3)                   # the open order keeps filling after the restart
    assert rig.acct().qty("AAPL") == 30
    assert rig.ledger.verify()["ok"]


def test_paused_simulation_neither_fills_nor_decides(rig):
    rig.quote("AAPL", 99.9, 100)
    rig.scripted.script = [buy()]
    rig.step()
    rig.sims.transition(rig.engine.sim["id"], "pause")
    rig.engine.refresh_sim()
    rig.quote("AAPL", 100, 100.1)
    assert rig.acct().positions == {}
    rig.sims.transition(rig.engine.sim["id"], "resume")
    rig.engine.refresh_sim()
    rig.quote("AAPL", 100, 100.4)
    assert rig.acct().qty("AAPL") == 10


def test_new_simulation_starts_clean_and_keeps_the_old_ledger(rig):
    rig.quote("AAPL", 99.9, 100)
    rig.scripted.script = [buy()]
    rig.step()
    rig.quote("AAPL", 100, 100.1)
    old = rig.engine.sim["id"]
    new = rig.sims.create("second", D("5000"))
    rig.engine.refresh_sim()
    assert rig.engine.sim["id"] == new["id"] and rig.acct().positions == {} and rig.acct().cash == D("5000")
    assert "fill" in [e.kind for e in rig.ledger.entries(old)]


def test_kill_switch_cancels_open_orders_and_blocks_new_ones(rig):
    rig.quote("AAPL", 99.9, 100)
    rig.scripted.script = [buy(type="limit", limit_price=D("90")), buy()]
    rig.step()
    rig.engine.set_kill_switch(True)
    assert rig.engine.state.orders == {}
    rig.step()
    assert rig.ledger.entries(rig.engine.sim["id"], ("order_rejected",))[0].payload["reason"] == "kill switch is on"


def test_only_the_operator_adds_cash_and_it_is_a_ledger_event(rig):
    rig.engine.deposit("scripted", D("1000"))
    assert rig.acct().cash == D("6000") and rig.acct().deposits == D("6000")
    e = rig.ledger.entries(rig.engine.sim["id"], ("cash_deposit",))[-1]
    assert e.payload == {"amount": "1000", "by": "operator"}
    assert rig.engine.value("scripted")["net_result"] == 0      # a deposit is not a profit


def test_close_expires_day_orders_charges_stamp_duty_and_snapshots(rig):
    rig.quote("AAPL", 99.9, 100)
    rig.scripted.script = [buy(qty=10), buy(qty=1, type="limit", limit_price=D("50"))]
    rig.step()
    rig.quote("AAPL", 100, 100)
    rig.step()
    rig.engine.tick(rig.now)
    rig.now = OPEN + timedelta(hours=6, minutes=31)
    rig.engine.tick(rig.now)
    assert rig.engine.state.orders == {}
    duty = rig.ledger.entries(rig.engine.sim["id"], ("stamp_duty",))[0].payload
    assert duty["usd"] == "0.0055"                 # 1000 * 0.2% / 365
    rig.store.flush()
    assert len(rig.store.query("SELECT * FROM equity_points")) >= 1


def test_optional_risk_limits_are_off_by_default_and_work_when_set(rig):
    rig.quote("AAPL", 99.9, 100)
    assert rig.engine.state.settings["max_position_pct"] is None
    rig.engine.set_settings({"max_position_pct": 10, "stop_loss_pct": 5})
    rig.scripted.script = [buy(qty=10), buy(qty=4)]
    rig.step()
    assert "risk limit" in rig.ledger.entries(rig.engine.sim["id"], ("order_rejected",))[0].payload["reason"]
    rig.step()
    rig.quote("AAPL", 100, 100)                    # bought 4 at 100
    rig.quote("AAPL", 94, 94.1)                    # more than 5% below cost: risk manager sells
    rig.quote("AAPL", 93.9, 94)
    assert rig.acct().positions == {}
    assert rig.ledger.entries(rig.engine.sim["id"], ("fill",))[-1].payload["meta"]["source"] == "risk_manager"


def test_price_alert_fires_once_and_wakes_the_strategy(rig):
    rig.quote("AAPL", 99.9, 100)
    rig.scripted.script = [Decision(alerts=[{"symbol": "AAPL", "direction": "above", "price": "105", "note": "breakout"}])]
    rig.step()
    rig.quote("AAPL", 104, 104.1)
    assert "alert_triggered" not in rig.kinds()
    rig.quote("AAPL", 105, 105.1)
    rig.quote("AAPL", 106, 106.1)
    assert rig.kinds().count("alert_triggered") == 1
    assert rig.engine.runners["scripted"].queue.get_nowait()["reason"] == "price_alert"


def test_buy_and_hold_invests_everything_once(tmp_path):
    r = EngineRig(tmp_path, strategies="buy_hold")
    r.quote("SPY", 499.9, 500)
    r.step("buy_hold")
    r.quote("SPY", 500, 500.1)
    assert r.acct("buy_hold").qty("SPY") == 9       # (5000 - 9) // 500
    assert r.step("buy_hold") is None


# ---------------------------------------------------------------- LLM agent
GOOD = json.dumps({"summary": "Apple looks strong after earnings.", "orders": [
    {"action": "buy", "ticker": "aapl", "quantity": 5, "order_type": "market", "rationale": "Earnings beat.", "confidence": 0.7}],
    "alerts": [{"ticker": "AAPL", "direction": "below", "price": 90, "note": "cut"}], "watch": ["MSFT"],
    "next_wake_minutes": 45})


def test_llm_agent_pays_for_its_tokens_immediately_and_trades_validated_json(tmp_path):
    llm = FakeLLM(["```json\n" + GOOD + "\n```"])
    r = EngineRig(tmp_path, strategies="llm_light", llm=llm)
    r.quote("AAPL", 99.9, 100)
    r.step("llm_light")
    a = r.acct("llm_light")
    assert a.token_cost == D("0.008") and a.cash == D("5000") - D("0.008")      # charged before any fill
    assert [c[1] for c in llm.calls] == ["decision"]                           # first wake of the day: no triage
    r.quote("AAPL", 100, 100.1)
    assert a.qty("AAPL") == 5
    fill = r.ledger.entries(r.engine.sim["id"], ("fill",))[0].payload
    assert fill["meta"]["rationale"] == "Earnings beat." and fill["meta"]["confidence"] == 0.7
    assert r.engine.runners["llm_light"].next_wake_s == 45 * 60
    assert len(r.engine.state.alerts) == 1
    state = json.loads(llm.calls[0][2])
    assert state["account"]["costs_paid_so_far"]["your_thinking_tokens"] == 0
    assert state["quotes"]["AAPL"] == {"bid": 99.9, "ask": 100.0, "ask_size": 1000, "today_pct": None}


def test_llm_agent_triage_can_stop_before_the_expensive_call(tmp_path):
    llm = FakeLLM([json.dumps({"summary": "wait", "orders": []}), '{"escalate": false, "reason": "old news"}'])
    r = EngineRig(tmp_path, strategies="llm_light", llm=llm)
    r.quote("AAPL", 99.9, 100)
    r.step("llm_light")
    r.step("llm_light", reason="news")
    assert [c[1] for c in llm.calls] == ["decision", "triage"]
    assert r.acct("llm_light").token_cost == D("0.016")
    assert r.step("llm_light", reason="timer") is None and len(llm.calls) == 2   # quiet timer wake costs nothing


def test_llm_agent_invalid_reply_costs_money_but_places_no_order(tmp_path):
    for bad in ["I think we should buy!", json.dumps({"orders": [{"action": "short", "ticker": "AAPL", "quantity": 5}]}),
                json.dumps({"orders": [{"action": "buy", "ticker": "AAPL", "quantity": 2.5}]})]:
        llm = FakeLLM([bad])
        r = EngineRig(tmp_path / str(len(bad)), strategies="llm_light", llm=llm)
        r.quote("AAPL", 99.9, 100)
        r.step("llm_light")
        assert r.acct("llm_light").token_cost == D("0.008")
        assert "order_accepted" not in r.kinds("llm_light")


def test_llm_agent_orders_face_the_same_broker_rules(tmp_path):
    reply = json.dumps({"summary": "x", "orders": [
        {"action": "buy", "ticker": "ZZZZ", "quantity": 1}, {"action": "sell", "ticker": "AAPL", "quantity": 1},
        {"action": "buy", "ticker": "AAPL", "quantity": 100000}]})
    r = EngineRig(tmp_path, strategies="llm_light", llm=FakeLLM([reply]))
    r.quote("AAPL", 99.9, 100)
    r.step("llm_light")
    reasons = [e.payload["reason"] for e in r.ledger.entries(r.engine.sim["id"], ("order_rejected",))]
    assert reasons == ["unknown symbol", "shares not held (short selling is not allowed)", "insufficient cash"]


def test_daily_token_ceiling_is_off_by_default_and_stops_thinking_when_set(tmp_path):
    llm = FakeLLM([json.dumps({"summary": "hold", "orders": []})])
    r = EngineRig(tmp_path, strategies="llm_light", llm=llm)
    r.quote("AAPL", 99.9, 100)
    r.engine.set_settings({"daily_token_budget_usd": 0.005})
    r.step("llm_light")                                # spends 0.008 > ceiling
    d = r.step("llm_light", reason="news")
    assert d.detail == {"skipped": "token_budget"} and len(llm.calls) == 1
