from decimal import Decimal as D

from tradelab.accounting import valuation
from tradelab.models import BrokerProfile
from helpers import Rig

P = BrokerProfile()


def round_trip(r, t, symbol, qty, buy_ask, sell_bid):
    r.quote(t, buy_ask, buy_ask, symbol=symbol)
    r.order(t + 1, "buy", qty, symbol=symbol)
    r.quote(t + 2, buy_ask, buy_ask, symbol=symbol)
    r.order(t + 3, "sell", qty, symbol=symbol)
    r.quote(t + 4, sell_bid, sell_bid, symbol=symbol)
    return r.fills()[-1]


def test_gain_is_taxed_at_26_percent_after_both_commissions():
    r = Rig()
    f = round_trip(r, 0, "AAPL", 10, 100, 120)
    # gain = 10*120 - 9 - (10*100 + 9) = 182 ; tax = 47.32
    assert f["realized_gain"] == "182" and f["tax"] == "47.32"
    assert r.acct.cash == D("5000") + 182 - D("47.32")
    assert r.acct.commissions == 18 and r.acct.taxes == D("47.32")


def test_tax_is_rounded_up_to_the_cent():
    r = Rig()
    f = round_trip(r, 0, "AAPL", 1, 100, 118.03)   # gain 0.03 -> 0.0078 -> 0.01
    assert f["realized_gain"] == "0.03" and f["tax"] == "0.01"


def test_loss_pays_no_tax_and_creates_a_credit_for_four_years():
    r = Rig()
    f = round_trip(r, 0, "AAPL", 10, 100, 90)
    assert f["tax"] == "0" and f["loss_created"] == "118" and f["loss_expiry"] == 2030
    assert r.acct.losses == [[D("118"), 2030]]


def test_earlier_loss_offsets_a_later_stock_gain():
    r = Rig()
    round_trip(r, 0, "AAPL", 10, 100, 90)              # loss 118
    f = round_trip(r, 10, "MSFT", 10, 100, 120)        # gain 182 -> taxable 64 -> 16.64
    assert f["loss_used"] == "118" and f["tax"] == "16.64"
    assert r.acct.losses == []


def test_a_gain_before_the_loss_gets_no_refund():
    r = Rig()
    round_trip(r, 0, "MSFT", 10, 100, 120)
    round_trip(r, 10, "AAPL", 10, 100, 90)
    assert r.acct.taxes == D("47.32") and r.acct.losses == [[D("118"), 2030]]


def test_etf_gain_cannot_use_loss_credit_but_etf_loss_creates_one():
    r = Rig()
    round_trip(r, 0, "AAPL", 10, 100, 90)              # stock loss 118
    f = round_trip(r, 10, "SPY", 10, 100, 120)         # ETF gain 182, taxed in full
    assert f["loss_used"] == "0" and f["tax"] == "47.32"
    assert r.acct.losses == [[D("118"), 2030]]
    f = round_trip(r, 20, "SPY", 10, 100, 95)
    assert f["loss_created"] == "68"


def test_expired_loss_credit_is_not_usable():
    r = Rig()
    r.acct.losses = [[D("500"), 2025]]                 # expired before 2026
    f = round_trip(r, 0, "AAPL", 10, 100, 120)
    assert f["loss_used"] == "0" and f["tax"] == "47.32"


def test_average_cost_basis_across_buys_and_partial_sell():
    r = Rig()
    r.quote(0, 100, 100)
    r.order(1, "buy", 10)
    r.quote(2, 100, 100)        # basis 1009
    r.order(3, "buy", 10)
    r.quote(4, 120, 120)        # basis 1009 + 1209 = 2218 for 20 shares
    r.order(5, "sell", 5)
    r.quote(6, 130, 130)
    f = r.fills()[-1]
    assert f["cost_basis"] == "554.5"                  # 2218 * 5 / 20
    assert f["realized_gain"] == str(D(650) - 9 - D("554.5"))
    assert r.acct.positions["AAPL"].basis == D("1663.5") and r.acct.qty("AAPL") == 15


def test_position_is_removed_when_fully_sold_and_cash_reconciles():
    r = Rig()
    round_trip(r, 0, "AAPL", 10, 100, 100)
    assert r.acct.positions == {}
    assert r.acct.cash == D("5000") - 18               # flat price: you lose exactly two commissions


def test_token_cost_and_stamp_duty_reduce_cash_and_are_tracked():
    r = Rig()
    r.emit("token_cost", {"usd": "0.25"}, "s")
    r.emit("stamp_duty", {"usd": "0.03"}, "s")
    assert r.acct.cash == D("4999.72") and r.acct.costs == D("0.28")


def test_valuation_marks_at_bid_and_net_liquidation_includes_exit_costs():
    r = Rig()
    r.quote(0, 100, 100)
    r.order(1, "buy", 10)
    r.quote(2, 100, 100)                                # cash 3991, basis 1009
    v = valuation(r.acct, {"AAPL": D(120)}, r.market.is_etf, 2026, P)
    assert v["equity"] == D("3991") + 1200
    # exit: commission 9 + tax 26% of (1200 - 9 - 1009 = 182) = 47.32
    assert v["exit_costs"] == D("56.32")
    assert v["net_liquidation"] == D("5191") - D("56.32")
    assert v["net_result"] == D("134.68")
    assert v["gross_result"] == D("200")                # (120-100)*10 before any cost


def test_valuation_does_not_net_unrealised_losses_against_unrealised_gains():
    r = Rig()
    for sym in ("AAPL", "MSFT"):
        r.quote(0, 100, 100, symbol=sym)
        r.order(1, "buy", 10, symbol=sym)
        r.quote(2, 100, 100, symbol=sym)
    v = valuation(r.acct, {"AAPL": D(120), "MSFT": D(80)}, r.market.is_etf, 2026, P)
    assert v["exit_costs"] == D("18") + D("47.32")      # full tax on the winner, nothing back for the loser


def test_unpriced_position_is_valued_at_zero_and_reported():
    r = Rig()
    r.quote(0, 100, 100)
    r.order(1, "buy", 10)
    r.quote(2, 100, 100)
    v = valuation(r.acct, {}, r.market.is_etf, 2026, P)
    assert v["market_value"] == 0 and v["unpriced"] == ["AAPL"]


def test_net_result_after_full_exit_equals_cash_minus_deposits():
    r = Rig()
    round_trip(r, 0, "AAPL", 10, 100, 120)
    v = valuation(r.acct, {}, r.market.is_etf, 2026, P)
    assert v["net_result"] == r.acct.cash - D("5000") == D("134.68")
