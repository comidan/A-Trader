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


# ---------------------------------------------------------------- through the engine
import asyncio
import json
from datetime import timedelta

from test_engine import ASSETS, OPEN, EngineRig, FakeLLM
from tradelab.ingest.alpaca import parse_crypto_bar, parse_crypto_quote
from tradelab.strategies.crypto import CryptoAITrader, CryptoBuyAndHold


class CryptoRig(EngineRig):
    def __init__(self, tmp_path, strategies, llm=None, weekend=True):
        self._strategies = strategies
        super().__init__(tmp_path, strategies=strategies, llm=llm)
        if weekend:
            self.now = OPEN + timedelta(days=5, hours=3)          # Saturday: the stock market is closed
        self.market.crypto_assets = {"BTC/USD": {"name": "Bitcoin"}, "ETH/USD": {"name": "Ethereum"}}
        self.engine.crypto = ["BTC/USD", "ETH/USD"]

    def factory(self, kind, sim):
        if kind == "crypto_buy_hold":
            return CryptoBuyAndHold()
        if kind == "crypto_ai":
            return CryptoAITrader(self.llm, "claude-haiku-5-5", "claude-opus-5-5", 30)
        return super().factory(kind, sim)

    def cq(self, pair, bid, ask, size="2.5"):
        self.now += timedelta(seconds=1)
        self.engine.on_quote(parse_crypto_quote({"S": pair, "bp": bid, "bs": size, "ap": ask, "as": size}, self.now, "test"))


def test_feed_conversion_to_units_is_exact_and_sizes_round_down():
    from datetime import datetime, timezone
    t = datetime(2026, 11, 16, tzinfo=timezone.utc)
    q = parse_crypto_quote({"S": "ETH/USD", "bp": "1818.58", "bs": "0.1002349", "ap": "1819.1", "as": "0.066113"}, t, "x")
    assert q.bid == D("0.00181858") and q.ask == D("0.0018191") and q.bid_size == 100_234 and q.ask_size == 66_113
    assert q.bid * 1_000_000 == D("1818.58")                      # one coin costs exactly what the feed said
    b = parse_crypto_bar({"S": "ETH/USD", "o": 1800, "h": 1820, "l": 1790, "c": 1810.5, "t": "x"}, t)
    assert b["c"] == D("0.0018105") and parse_crypto_quote({"S": "ETH/USD"}, t, "x") is None


def test_bitcoin_buy_and_hold_runs_on_a_weekend_and_spends_all_cash(tmp_path):
    r = CryptoRig(tmp_path, "crypto_buy_hold")
    assert not r.market.is_open(r.now)
    r.cq("BTC/USD", "59990", "60000")
    d = r.step("crypto_buy_hold")
    assert d.orders[0].symbol == "BTC/USD"
    r.cq("BTC/USD", "59995", "60005")
    a = r.acct("crypto_buy_hold")
    assert a.qty("BTC/USD") > 82_000 and D("0") <= a.cash < D("10")       # about 0.083 BTC for $5,000
    assert a.commissions == a.traded_value * D("0.0025") and a.cash == D("5000") - a.traded_value - a.commissions
    assert r.step("crypto_buy_hold") is None


def test_crypto_ai_trader_talks_in_coins_and_trades_in_units_with_exits(tmp_path):
    reply = json.dumps({"summary": "ETH is breaking out.", "orders": [
        {"action": "buy", "pair": "eth/usd", "quantity": 0.5, "take_profit_price": 2100, "stop_loss_price": 1950, "rationale": "r"},
        {"action": "buy", "pair": "DOGE/USD", "quantity": 100}], "notes": "thesis", "next_wake_minutes": 20})
    llm = FakeLLM([reply])
    r = CryptoRig(tmp_path, "crypto_buy_hold,crypto_ai", llm=llm)
    r.cq("ETH/USD", "1999", "2000")
    r.cq("BTC/USD", "59990", "60000")
    d = r.step("crypto_ai")
    state = json.loads(llm.calls[0][2])
    assert state["quotes"]["ETH/USD"]["ask"] == 2000.0 and state["quotes"]["ETH/USD"]["ask_size_coins"] == 2.5
    assert state["benchmark"]["buy_and_hold_bitcoin_net_result"] == 0.0
    assert d.detail["orders_skipped"] == ["DOGE/USD"]                    # not a followed pair
    r.cq("ETH/USD", "2000", "2001")
    a = r.acct("crypto_ai")
    assert a.qty("ETH/USD") == 500_000                                   # 0.5 ETH
    assert a.commissions == D("1000.5") * D("0.0025")
    plan = r.engine.state.exit_plans["crypto_ai:ETH/USD"]
    assert D(plan["take_profit"]) == D("2100") * D("0.000001")
    r.cq("ETH/USD", "2101", "2102")                                      # take-profit reached...
    r.cq("ETH/USD", "2101", "2102")                                      # ...sold on the next quote, no model call
    assert a.positions == {} and len(llm.calls) == 1
    sell = r.ledger.entries(r.engine.sim["id"], ("fill",), "crypto_ai")[-1].payload
    assert sell["tax_bucket"] == "crypto" and D(sell["tax"]) > 0


def test_an_order_filled_in_pieces_wakes_the_trader_once_and_shows_as_one_row(tmp_path):
    from tradelab.services import Services
    reply = json.dumps({"summary": "buy", "orders": [{"action": "buy", "pair": "ETH/USD", "quantity": 1.0}]})
    r = CryptoRig(tmp_path, "crypto_ai", llm=FakeLLM([reply]))
    r.cq("ETH/USD", "1999", "2000")
    r.step("crypto_ai")
    runner = r.engine.runners["crypto_ai"]
    while not runner.queue.empty():
        runner.queue.get_nowait()
    for _ in range(3):
        r.cq("ETH/USD", "1999", "2000", size="0.3")                # three pieces of 0.3 ETH...
    assert runner.queue.empty()                                    # ...no wake yet: the order is not complete
    r.cq("ETH/USD", "1999", "2002", size="0.3")                    # the last 0.1 ETH
    assert [runner.queue.get_nowait()["reason"]] == ["fill"] and runner.queue.empty()
    rows = [{"kind": "fill", "order_id": "o1", "qty": 0.1, "price": 2002.0, "commission": 0.5, "tax": 0.0, "seq": 4},
            {"kind": "decision", "seq": 3},
            {"kind": "fill", "order_id": "o1", "qty": 0.9, "price": 2000.0, "commission": 4.5, "tax": 0.0, "seq": 2}]
    merged = Services._merge_fills(rows)
    assert [m["kind"] for m in merged] == ["fill", "decision"]
    assert merged[0]["pieces"] == 2 and abs(merged[0]["qty"] - 1.0) < 1e-12 and abs(merged[0]["price"] - 2000.2) < 1e-9
    assert merged[0]["commission"] == 5.0
