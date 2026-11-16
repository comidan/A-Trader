"""Crypto: traded in integer units of one millionth of a coin, percentage fees, 33% tax in its own loss bucket."""
from decimal import Decimal as D

from tradelab.accounting import valuation
from tradelab.models import CRYPTO_LOT, BrokerProfile
from helpers import CryptoMarket, Rig

P = BrokerProfile()
UNIT = CRYPTO_LOT            # 1 unit = 0.000001 coin
BTC = "BTC/USD"


def rig():
    r = Rig()
    r.market = CryptoMarket()
    r.broker.market = r.market
    return r


def q(r, t, bid, ask, size_coins=1.0, symbol=BTC):
    """Quote given in dollars per coin and coins; the feed converts to units exactly like this."""
    return r.quote(t, D(str(bid)) * UNIT, D(str(ask)) * UNIT, int(D(str(size_coins)) / UNIT), int(D(str(size_coins)) / UNIT), symbol=symbol)


def o(r, t, side, units, symbol=BTC, **kw):
    return r.broker.submit("s", __import__("tradelab.models", fromlist=["OrderRequest"]).OrderRequest(symbol, side, units, **kw), __import__("helpers").at(t))


def test_crypto_trades_while_the_stock_market_is_closed_and_stocks_do_not():
    r = rig()
    q(r, 0, 59990, 60000)
    r.quote(0, 99.9, 100, symbol="AAPL")
    assert r.order(1, "buy", 1, symbol="AAPL")[1] == "market is closed"
    assert o(r, 1, "buy", 10_000)[0] is not None          # 0.01 BTC
    q(r, 2, 60000, 60010)
    assert r.acct.qty(BTC) == 10_000


def test_fractional_amounts_and_percentage_fee_on_every_fill():
    r = rig()
    q(r, 0, 59990, 60000)
    o(r, 1, "buy", 25_000)                                 # 0.025 BTC = $1,500
    q(r, 2, 59990, 60000, size_coins=0.01)                    # only 0.01 BTC on offer
    q(r, 3, 59990, 60000, size_coins=1)
    fills = r.fills()
    assert [f["qty"] for f in fills] == [10_000, 15_000]
    assert [D(f["commission"]) for f in fills] == [D("1.5"), D("2.25")]      # 0.25% of $600 and of $900
    assert r.acct.cash == D("5000") - 1500 - D("3.75")
    assert r.acct.positions[BTC].basis == D("1503.75")


def test_cash_never_goes_negative_with_a_percentage_fee():
    r = Rig(cash="1000")
    r.market = CryptoMarket(); r.broker.market = r.market
    q(r, 0, 59990, 60000)
    assert o(r, 1, "buy", 16_667)[1] == "insufficient cash"               # $1,000.02 + fee
    o(r, 1, "buy", 16_000)                                                # $960 + $2.40
    q(r, 2, 61990, 62000)                                                    # price jumps before the fill
    f = r.fills()[0]
    assert f["qty"] < 16_100 and r.acct.cash >= 0
    assert r.acct.cash < D("9")                                              # spent what it could, never more


def test_minimum_order_and_unknown_pair():
    r = rig()
    q(r, 0, 59990, 60000)
    assert "below the minimum" in o(r, 1, "buy", 10)[1]                   # 0.00001 BTC = $0.60
    r.market.untradable["XXX/USD"] = "unknown or untradable crypto pair"
    q(r, 0, 1, 1.01, symbol="XXX/USD")
    assert r.order(1, "buy", 1_000_000, symbol="XXX/USD")[1] == "unknown or untradable crypto pair"


def test_crypto_orders_are_not_expired_by_the_stock_market_close():
    r = rig()
    q(r, 0, 59990, 60000)
    keep, _ = o(r, 1, "buy", 10_000, type="limit", limit_price=D("50000") * UNIT)
    r.broker.expire_day_orders()
    assert list(r.state.orders) == [keep.id]


def round_trip(r, t, symbol, units, buy, sell):
    q(r, t, buy, buy, symbol=symbol)
    r.order(t + 1, "buy", units, symbol=symbol)
    q(r, t + 2, buy, buy, symbol=symbol)
    r.order(t + 3, "sell", units, symbol=symbol)
    q(r, t + 4, sell, sell, symbol=symbol)
    return r.fills()[-1]


def test_crypto_gain_is_taxed_at_33_percent_after_both_fees():
    r = rig()
    f = round_trip(r, 0, BTC, 20_000, 60000, 66000)          # 0.02 BTC: $1,200 -> $1,320
    # fees 3.00 + 3.30; gain = 1320 - 3.30 - 1203 = 113.70; tax 33% = 37.53 (rounded up)
    assert D(f["realized_gain"]) == D("113.70") and f["tax"] == "37.53" and f["tax_bucket"] == "crypto"


def test_crypto_losses_only_offset_crypto_gains_not_stock_gains():
    r = rig()
    r.market.open = True
    f = round_trip(r, 0, BTC, 20_000, 60000, 54000)          # crypto loss
    loss = D(f["loss_created"])
    assert loss > 0 and r.acct.losses == [[loss, 2030, "crypto"]]
    r.quote(10, 100, 100, symbol="AAPL"); r.order(11, "buy", 10, symbol="AAPL"); r.quote(12, 100, 100, symbol="AAPL")
    r.order(13, "sell", 10, symbol="AAPL"); r.quote(14, 120, 120, symbol="AAPL")
    stock = r.fills()[-1]
    assert stock["loss_used"] == "0" and stock["tax"] == "47.32"             # the crypto loss did not help
    f = round_trip(r, 20, "ETH/USD", 500_000, 2000, 2400)    # crypto gain: now the loss is used
    assert D(f["loss_used"]) == loss and r.acct.loss_credit(2026, "crypto") == 0


def test_valuation_of_crypto_includes_percentage_exit_fee_and_33_percent_tax():
    r = rig()
    q(r, 0, 60000, 60000)
    o(r, 1, "buy", 20_000)
    q(r, 2, 60000, 60000)                                    # basis 1203
    v = valuation(r.acct, {BTC: D("66000") * UNIT}, r.market.is_etf, 2026, P)
    # exit fee 3.30; gain 1320 - 3.30 - 1203 = 113.70; tax 37.53
    assert v["exit_costs"] == D("3.30") + D("37.53")
    assert v["equity"] == D("5000") - 1203 + 1320
