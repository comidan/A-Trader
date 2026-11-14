"""The broker is the core: a bug here falsifies every result."""
from decimal import Decimal as D

from tradelab.accounting import SimState, apply_event
from helpers import Rig, at


# ---------------------------------------------------------------- prices
def test_buy_fills_at_ask_and_sell_at_bid_never_mid():
    r = Rig()
    r.quote(0, 99.90, 100.10)
    r.order(1, "buy", 10)
    r.quote(2, 100.00, 100.20)
    assert D(r.fills()[0]["price"]) == D("100.20")
    r.order(3, "sell", 10)
    r.quote(4, 100.05, 100.25)
    assert D(r.fills()[1]["price"]) == D("100.05")


def test_quote_the_decision_saw_is_never_used_no_look_back():
    r = Rig()
    r.quote(0, 99.90, 100.00)                 # the decision is based on this
    order, _ = r.order(1, "buy", 10)
    assert r.fills() == []                    # submission alone never fills
    r.broker.on_quote(r.market.quotes["AAPL"])  # re-delivering the old quote changes nothing
    assert r.fills() == []
    r.quote(1.5, 100.40, 100.50)              # first quote after the decision: worse price, and that is the fill
    assert D(r.fills()[0]["price"]) == D("100.50")
    assert r.fills()[0]["quote"]["recv_ts"] > order.ready_ts.isoformat()


def test_quote_received_at_the_same_instant_is_not_after():
    r = Rig()
    r.quote(0, 99.9, 100)
    r.order(1, "buy", 1)
    r.quote(1, 99.9, 100)
    assert r.fills() == []


def test_extra_latency_delays_the_first_usable_quote():
    r = Rig(extra_latency_ms=500)
    r.quote(0, 99.9, 100)
    r.order(1, "buy", 1)
    r.quote(1.4, 99.9, 100)
    assert r.fills() == []
    r.quote(1.6, 99.9, 101)
    assert D(r.fills()[0]["price"]) == D("101")


def test_slippage_makes_market_orders_worse_in_both_directions():
    r = Rig(slippage_bps=D(10))
    r.quote(0, 99.9, 100)
    r.order(1, "buy", 10)
    r.quote(2, 99.9, 100)
    assert D(r.fills()[0]["price"]) == D("100.1000")
    r.order(3, "sell", 10)
    r.quote(4, 100, 100.1)
    assert D(r.fills()[1]["price"]) == D("99.9000")


# ---------------------------------------------------------------- size
def test_fill_is_capped_at_quoted_size_and_remainder_stays_queued():
    r = Rig()
    r.quote(0, 9.9, 10, ask_size=1000)
    r.order(1, "buy", 250)
    r.quote(2, 9.9, 10, ask_size=100)
    r.quote(3, 9.9, 10.05, ask_size=100)
    r.quote(4, 9.9, 10.10, ask_size=500)
    assert [(f["qty"], D(f["price"])) for f in r.fills()] == [(100, D("10")), (100, D("10.05")), (50, D("10.10"))]
    assert r.state.orders == {}
    assert r.acct.qty("AAPL") == 250


def test_two_orders_cannot_both_take_the_same_quoted_shares():
    r = Rig()
    r.quote(0, 9.9, 10)
    r.order(1, "buy", 80)
    r.order(1.1, "buy", 80)
    r.quote(2, 9.9, 10, ask_size=100)
    assert [f["qty"] for f in r.fills()] == [80, 20]
    r.quote(3, 9.9, 10, ask_size=100)
    assert [f["qty"] for f in r.fills()] == [80, 20, 60]


def test_commission_is_charged_once_per_order_not_per_partial_fill():
    r = Rig()
    r.quote(0, 9.9, 10)
    r.order(1, "buy", 200)
    r.quote(2, 9.9, 10, ask_size=100)
    r.quote(3, 9.9, 10, ask_size=100)
    assert [f["commission"] for f in r.fills()] == ["9", "0"]
    assert r.acct.cash == D("5000") - 2000 - 9


def test_cancel_keeps_the_filled_part_and_drops_the_rest():
    r = Rig()
    r.quote(0, 9.9, 10)
    o, _ = r.order(1, "buy", 200)
    r.quote(2, 9.9, 10, ask_size=50)
    assert r.broker.cancel(o.id)
    r.quote(3, 9.9, 10, ask_size=500)
    assert r.acct.qty("AAPL") == 50 and r.state.orders == {}
    assert r.events[-1][2]["unfilled_qty"] == 150


def test_zero_size_or_crossed_or_empty_quotes_never_fill():
    r = Rig()
    r.quote(0, 9.9, 10)
    r.order(1, "buy", 10)
    r.quote(2, 9.9, 10, ask_size=0)
    r.quote(3, 10.5, 10)        # crossed
    r.quote(4, 0, 10)           # no bid
    assert r.fills() == []


# ---------------------------------------------------------------- market state
def test_orders_are_rejected_while_market_is_closed():
    r = Rig()
    r.quote(0, 9.9, 10)
    r.market.open = False
    o, why = r.order(1, "buy", 1)
    assert o is None and why == "market is closed"
    assert r.kinds()[-1] == "order_rejected"


def test_no_fill_on_quotes_arriving_while_closed_or_halted():
    r = Rig()
    r.quote(0, 9.9, 10)
    r.order(1, "buy", 10)
    r.market.open = False
    r.quote(2, 9.9, 10)
    r.market.open = True
    r.market.halted.add("AAPL")
    r.quote(3, 9.9, 10)
    assert r.fills() == []
    r.market.halted.clear()
    r.quote(4, 9.9, 10.3)
    assert D(r.fills()[0]["price"]) == D("10.3")


def test_day_orders_expire_at_close_gtc_survive():
    r = Rig()
    r.quote(0, 9.9, 10)
    r.order(1, "buy", 10, type="limit", limit_price=9)
    keep, _ = r.order(1, "buy", 10, type="limit", limit_price=9, tif="gtc")
    r.broker.expire_day_orders()
    assert list(r.state.orders) == [keep.id]


def test_rejected_without_a_live_quote_or_when_untradable():
    r = Rig()
    assert r.order(1, "buy", 1)[1].startswith("no live quote")
    r.quote(0, 9.9, 10)
    r.market.untradable["AAPL"] = "not tradable at the broker"
    assert r.order(1, "buy", 1)[1] == "not tradable at the broker"


def test_price_below_broker_minimum_is_rejected():
    r = Rig()
    r.quote(0, 0.40, 0.45)
    assert "broker minimum" in r.order(1, "buy", 10)[1]


# ---------------------------------------------------------------- account limits
def test_fractional_zero_and_negative_quantities_are_rejected():
    r = Rig()
    r.quote(0, 9.9, 10)
    for bad in (0, -5, 1.5, True):
        assert "whole number" in r.order(1, "buy", bad)[1]


def test_cannot_buy_more_than_cash_including_commission():
    r = Rig(cash="1000")
    r.quote(0, 9.9, 10)
    assert r.order(1, "buy", 100)[1] == "insufficient cash"      # 1000 + 9 commission > 1000
    assert r.order(1, "buy", 99)[0] is not None                  # 990 + 9 = 999


def test_open_buy_orders_reserve_cash():
    r = Rig(cash="1000")
    r.quote(0, 9.9, 10)
    r.order(1, "buy", 60)
    assert r.order(1.1, "buy", 60)[1] == "insufficient cash"


def test_cash_never_goes_negative_when_price_jumps_before_the_fill():
    r = Rig(cash="1000")
    r.quote(0, 9.9, 10)
    r.order(1, "buy", 99)
    r.quote(2, 19.9, 20)                       # price doubled before the first usable quote
    assert r.fills()[0]["qty"] == 49           # (1000 - 9) // 20
    assert r.acct.cash == D("1000") - 49 * 20 - 9 >= 0


def test_order_is_cancelled_if_not_even_one_share_is_affordable_at_fill():
    r = Rig(cash="100")
    r.quote(0, 9.9, 10)
    r.order(1, "buy", 9)
    r.quote(2, 199, 200)
    assert r.fills() == [] and r.kinds()[-1] == "order_cancelled" and r.acct.cash == D("100")


def test_cannot_sell_shares_not_held_and_open_sells_reserve_shares():
    r = Rig()
    r.quote(0, 9.9, 10)
    assert "not held" in r.order(1, "sell", 1)[1]
    r.order(1, "buy", 10)
    r.quote(2, 9.9, 10)
    assert r.order(3, "sell", 6)[0] is not None
    assert "not held" in r.order(3.1, "sell", 6)[1]


def test_kill_switch_rejects_everything():
    r = Rig()
    r.quote(0, 9.9, 10)
    r.emit("kill_switch", {"on": True}, None)
    assert r.order(1, "buy", 1)[1] == "kill switch is on"


# ---------------------------------------------------------------- limit and stop
def test_limit_buy_waits_until_the_ask_reaches_the_limit_and_fills_at_the_ask():
    r = Rig()
    r.quote(0, 9.9, 10)
    r.order(1, "buy", 10, type="limit", limit_price=9.50)
    r.quote(2, 9.50, 9.60)                      # bid touches the limit: not enough, we need the ask
    assert r.fills() == []
    r.quote(3, 9.30, 9.40)
    assert D(r.fills()[0]["price"]) == D("9.40")


def test_limit_sell_waits_until_the_bid_reaches_the_limit():
    r = Rig()
    r.quote(0, 9.9, 10)
    r.order(1, "buy", 10)
    r.quote(2, 9.9, 10)
    r.order(3, "sell", 10, type="limit", limit_price=11)
    r.quote(4, 10.9, 11.0)
    assert len(r.fills()) == 1
    r.quote(5, 11.2, 11.3)
    assert D(r.fills()[1]["price"]) == D("11.2")


def test_limit_orders_are_not_subject_to_slippage_beyond_the_limit():
    r = Rig(slippage_bps=D(50))
    r.quote(0, 9.9, 10)
    r.order(1, "buy", 10, type="limit", limit_price=10)
    r.quote(2, 9.9, 10)
    assert D(r.fills()[0]["price"]) == D("10")


def test_stop_sell_triggers_on_the_bid_and_fills_on_the_next_quote():
    r = Rig()
    r.quote(0, 9.9, 10)
    r.order(1, "buy", 10)
    r.quote(2, 9.9, 10)
    r.order(3, "sell", 10, type="stop", stop_price=9.50)
    r.quote(4, 9.60, 9.70)
    assert len(r.fills()) == 1
    r.quote(5, 9.45, 9.55)                      # triggers here...
    assert len(r.fills()) == 1 and r.kinds()[-1] == "order_triggered"
    r.quote(6, 9.10, 9.20)                      # ...and fills here, at the worse bid (gap risk is real)
    assert D(r.fills()[1]["price"]) == D("9.10")


def test_stop_buy_triggers_on_the_ask():
    r = Rig()
    r.quote(0, 9.9, 10)
    r.order(1, "buy", 10, type="stop", stop_price=10.5)
    r.quote(2, 10.4, 10.5)
    r.quote(3, 10.6, 10.7)
    assert D(r.fills()[0]["price"]) == D("10.7")


# ---------------------------------------------------------------- rebuild
def test_state_rebuilt_from_the_event_log_is_identical():
    r = Rig()
    r.quote(0, 9.9, 10)
    r.order(1, "buy", 300)
    r.quote(2, 9.9, 10, ask_size=100)
    r.quote(3, 9.9, 10.2, ask_size=500)
    r.order(4, "sell", 120)
    r.quote(5, 11, 11.1)
    r.order(6, "sell", 50, type="stop", stop_price=9, tif="gtc")
    r.order(7, "buy", 5000)                    # rejected
    r.emit("token_cost", {"usd": "0.0421"}, "s")
    rebuilt = SimState()
    for kind, sid, payload in r.events:
        apply_event(rebuilt, kind, sid, payload)
    assert rebuilt.accounts == r.state.accounts
    assert {k: o.to_dict() for k, o in rebuilt.orders.items()} == {k: o.to_dict() for k, o in r.state.orders.items()}
