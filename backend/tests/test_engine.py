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


# ---------------------------------------------------------------- corporate actions
def _hold(rig, qty=10, price=100):
    rig.quote("AAPL", price - 0.1, price)
    rig.scripted.script = [buy(qty=qty)]
    rig.step()
    rig.quote("AAPL", price, price)


SPLIT = {"id": "ca1", "type": "forward_split", "symbol": "AAPL", "ex_date": "2026-11-17", "new_rate": 4, "old_rate": 1}


def test_forward_split_multiplies_shares_keeps_basis_and_is_applied_once(rig):
    _hold(rig)
    basis = rig.acct().positions["AAPL"].basis
    assert rig.engine.process_corporate_actions([SPLIT], "2026-11-16") == 0       # not yet the ex-date
    assert rig.engine.process_corporate_actions([SPLIT], "2026-11-17") == 1
    assert rig.engine.process_corporate_actions([SPLIT], "2026-11-17") == 0       # idempotent
    pos = rig.acct().positions["AAPL"]
    assert pos.qty == 40 and pos.basis == basis and pos.avg_cost == basis / 40
    assert rig.engine.value("scripted")["unpriced"] == ["AAPL"]                   # old price is not reused
    restarted = rig.new_engine()
    assert restarted.state.accounts["scripted"].qty("AAPL") == 40
    assert restarted.process_corporate_actions([SPLIT], "2026-11-18") == 0


def test_reverse_split_rounds_down_and_cancels_open_orders(rig):
    _hold(rig, qty=25)
    rig.scripted.script = [Decision([OrderRequest("AAPL", "sell", 5, "limit", limit_price=D("500"), tif="gtc")])]
    rig.step()
    act = {"id": "ca2", "type": "reverse_split", "symbol": "AAPL", "ex_date": "2026-11-17", "new_rate": 1, "old_rate": 10}
    rig.engine.process_corporate_actions([act], "2026-11-17")
    assert rig.acct().qty("AAPL") == 2 and rig.engine.state.orders == {}


def test_shares_bought_on_or_after_the_ex_date_are_not_split_again(rig):
    _hold(rig)
    rig.now = OPEN + timedelta(days=1, hours=1)                                    # 17 Nov, after the split, action seen late
    rig.market.set_calendar([{"date": "2026-11-17", "open": "09:30", "close": "16:00"}], rig.now)
    rig.market.quotes.clear()
    rig.quote("AAPL", 24.9, 25)
    rig.scripted.script = [buy(qty=4)]
    rig.step()
    rig.quote("AAPL", 25, 25)
    rig.engine.process_corporate_actions([SPLIT], "2026-11-17")
    assert rig.acct().qty("AAPL") == 44                                           # 10 * 4 + 4


def test_cash_dividend_is_paid_net_of_both_taxes_on_the_payable_date(rig):
    _hold(rig)
    div = {"id": "ca3", "type": "cash_dividend", "symbol": "AAPL", "ex_date": "2026-11-17", "payable_date": "2026-11-20", "rate": 0.25}
    cash = rig.acct().cash
    assert rig.engine.process_corporate_actions([div], "2026-11-19") == 0
    assert rig.engine.process_corporate_actions([div], "2026-11-20") == 1
    # gross 2.50, US withholding 0.38, Italian tax 26% of 2.12 = 0.56 (both rounded up) -> net 1.56
    assert rig.acct().cash - cash == D("1.56") and rig.acct().dividends == D("1.56")
    assert rig.engine.process_corporate_actions([div], "2026-11-21") == 0


def test_no_dividend_for_shares_bought_on_the_ex_date(rig):
    rig.now = OPEN + timedelta(minutes=30)
    _hold(rig)
    div = {"id": "ca4", "type": "cash_dividend", "symbol": "AAPL", "ex_date": "2026-11-16", "payable_date": "2026-11-16", "rate": 1}
    assert rig.engine.process_corporate_actions([div], "2026-11-16") == 0


def test_unhandled_action_on_a_holding_blocks_purchases_until_released(rig):
    _hold(rig)
    act = {"id": "ca5", "type": "spin_off", "symbol": "AAPL", "ex_date": "2026-11-16"}
    rig.engine.process_corporate_actions([act], "2026-11-16")
    rig.scripted.script = [buy(qty=1), Decision([OrderRequest("AAPL", "sell", 1)]), buy(qty=1)]
    rig.step()
    assert "purchases blocked" in rig.ledger.entries(rig.engine.sim["id"], ("order_rejected",))[0].payload["reason"]
    rig.step()
    assert rig.kinds()[-1] == "order_accepted"                                    # selling is still allowed
    rig.engine.release("AAPL")
    rig.step()
    assert rig.kinds()[-1] == "order_accepted"


def test_unexplained_overnight_price_jump_freezes_the_ticker(rig):
    _hold(rig)
    rig.now = OPEN + timedelta(days=1, hours=1)
    rig.market.set_calendar([{"date": "2026-11-17", "open": "09:30", "close": "16:00"}], rig.now)
    rig.scripted.script = [Decision([OrderRequest("AAPL", "sell", 10)])]
    rig.quote("AAPL", 24.9, 25)                                                   # -75% with no known split
    assert rig.market.is_halted("AAPL")
    rig.step()
    rig.quote("AAPL", 24.9, 25)
    assert rig.acct().qty("AAPL") == 10                                           # nothing fills on an untrusted price
    rig.engine.process_corporate_actions([SPLIT], "2026-11-17")                   # the split arrives and explains it
    assert not rig.market.is_halted("AAPL") and rig.acct().qty("AAPL") == 40


def test_llm_agent_keeps_its_notes_and_sees_history_trades_and_break_even(tmp_path):
    first = json.dumps({"summary": "buy", "orders": [{"action": "buy", "ticker": "AAPL", "quantity": 5}],
                        "notes": "Thesis: guidance raise. Wrong if it closes below 95."})
    llm = FakeLLM([first, '{"escalate": true, "reason": "fill"}', json.dumps({"summary": "hold", "orders": []})])
    r = EngineRig(tmp_path, strategies="llm_light", llm=llm)
    r.market.set_daily({"AAPL": [100 + i * 0.5 for i in range(80)]}, r.now)
    r.quote("AAPL", 99.9, 100)
    r.step("llm_light")
    r.quote("AAPL", 100, 100)
    r.step("llm_light", reason="fill")
    state = json.loads(llm.calls[-1][2])
    assert state["your_notes"].startswith("Thesis: guidance raise")
    assert state["your_recent_trades"][0] == {"at": state["your_recent_trades"][0]["at"], "side": "buy", "ticker": "AAPL", "shares": 5, "price": 100.0}
    assert state["account"]["positions"][0]["break_even_bid_after_exit_fee"] == 103.6      # (500 + 9 + 9) / 5
    h = state["quotes"]["AAPL"]["history"]
    assert h["last_close"] == 139.5 and h["pct_5d"] == round((139.5 / 137 - 1) * 100, 2) and h["pct_from_1y_high"] == 0
    restarted = r.new_engine()                       # the notes survive a restart (they live in the ledger)
    assert restarted.runners["llm_light"].notes.startswith("Thesis")


def test_llm_agent_sees_market_wide_world_news_but_not_low_impact_noise(tmp_path):
    llm = FakeLLM([json.dumps({"summary": "hold", "orders": []})])
    r = EngineRig(tmp_path, strategies="llm_light", llm=llm)
    r.quote("AAPL", 99.9, 100)
    base = {"kind": "world", "source": "GDELT (x)", "recv_ts": r.now.isoformat(), "symbols": [], "links": []}
    r.engine.on_news({**base, "id": "w1", "headline": "Fed signals rate cut", "impact": "medium"})
    r.engine.on_news({**base, "id": "w2", "headline": "Local festival opens", "impact": "low"})
    r.step("llm_light")
    seen = [n["headline"] for n in json.loads(llm.calls[0][2])["news_since_your_last_decision"]]
    assert seen == ["Fed signals rate cut"]
