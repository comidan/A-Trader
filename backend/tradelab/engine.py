"""The engine: wires live data, strategies, the simulated broker, accounting and the ledger.

All simulation state changes go through `emit`, which appends to the immutable
ledger first and only then applies the change. On restart the state is rebuilt
by replaying the ledger, so nothing is ever recomputed after the fact.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from decimal import ROUND_UP, Decimal
from typing import Any, Callable

from .accounting import SimState, apply_event, valuation
from .broker import SimBroker
from .bus import EventBus
from .clock import iso, now_utc
from .config import Settings
from .market import NY, MarketState, Universe
from .models import BrokerProfile, D, OrderRequest, Quote
from .simulations import Simulations
from .storage.db import Ledger
from .storage.market_store import MarketStore
from .strategies.base import Context, Decision, Strategy

log = logging.getLogger(__name__)

DEFAULT_SETTINGS = {"max_position_pct": None, "max_daily_loss_pct": None, "stop_loss_pct": None,
                    "daily_token_budget_usd": None, "extra_latency_ms": 0, "slippage_bps": 0}
MOVE_WINDOW = timedelta(minutes=5)
MOVE_THRESHOLD = Decimal("0.015")


class Runner:
    """One continuous loop per strategy: woken by events, by a timer and by its own schedule."""

    def __init__(self, engine: "Engine", sid: str, strategy: Strategy) -> None:
        self.engine, self.sid, self.strategy = engine, sid, strategy
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.next_wake_s: float | None = None
        self.last_decision_at: datetime | None = None
        self.notes = ""
        self.task: asyncio.Task | None = None
        self.busy = False

    def wake(self, reason: str, data: Any = None) -> None:
        self.queue.put_nowait({"reason": reason, "data": data, "at": iso(self.engine.clock())})

    async def run(self) -> None:
        while True:
            timeout = self.next_wake_s if self.next_wake_s is not None else self.strategy.interval_s
            scheduled = self.next_wake_s is not None
            try:
                wakes = [await asyncio.wait_for(self.queue.get(), timeout)]
                await asyncio.sleep(self.engine.coalesce_s)        # let a burst of events arrive together
                while not self.queue.empty():
                    wakes.append(self.queue.get_nowait())
            except asyncio.TimeoutError:
                wakes = [{"reason": "scheduled" if scheduled else "timer", "data": None, "at": iso(self.engine.clock())}]
                self.next_wake_s = None
            if not self.engine.running or not self.engine.market.is_open(self.engine.clock()):
                continue
            self.busy = True
            try:
                await self.engine.step(self, wakes)
            except Exception:
                log.exception("strategy_step_failed", extra={"strategy": self.sid})
                self.engine.emit("strategy_error", {"error": "step failed, see logs"}, self.sid)
            finally:
                self.busy = False


class Engine:
    def __init__(self, settings: Settings, ledger: Ledger, sims: Simulations, bus: EventBus, market: MarketState,
                 store: MarketStore, strategy_factory: Callable[[str, dict[str, Any]], Strategy | None],
                 clock: Callable[[], datetime] = now_utc, autostart: bool = True) -> None:
        self.settings, self.ledger, self.sims, self.bus = settings, ledger, sims, bus
        self.market, self.store, self.clock, self.factory = market, store, clock, strategy_factory
        self.profile = BrokerProfile()
        self.autostart = autostart
        self.universe = Universe(settings.core_list, settings.capacity)
        self.sim: dict[str, Any] | None = None
        self.state = SimState()
        self.broker: SimBroker | None = None
        self.runners: dict[str, Runner] = {}
        self.coalesce_s = 3.0
        self.on_universe_change: Callable[[list[str], list[str]], None] | None = None
        self.on_order: Callable[[str, dict[str, Any]], None] | None = None       # paper cross-check hook
        self._move_ref: dict[str, tuple[Decimal, datetime]] = {}
        self._day_start: dict[str, Decimal] = {}
        self._tokens_today: dict[str, tuple[Any, Decimal]] = {}
        self._last_stamp: dict[str, Any] = {}
        self._was_open = False
        self._last_snapshot: datetime | None = None
        self._suspended: set[str] = set()
        self._explained: dict[str, str] = {}          # symbol -> day a split explained its price jump
        self.on_market_open: Callable[[], None] | None = None
        self.relations: Callable[[str], dict[str, Any] | None] | None = None

    # ------------------------------------------------------------------ lifecycle
    @property
    def running(self) -> bool:
        return bool(self.sim and self.sim["state"] == "running")

    def load(self) -> None:
        """(Re)build everything from the ledger for the active simulation."""
        for r in self.runners.values():
            if r.task:
                r.task.cancel()
        self.runners, self.state, self.broker = {}, SimState(), None
        self.sim = self.sims.active()
        if not self.sim or self.sim["mode"] != "forward":
            return
        for e in self.ledger.entries(self.sim["id"]):
            apply_event(self.state, e.kind, e.strategy_id, e.payload)
            if e.kind == "stamp_duty":
                self._last_stamp[e.strategy_id] = e.payload["date"]
        self.state.settings = {**DEFAULT_SETTINGS, **self.state.settings}
        self._make_broker()
        capital = D(self.sim["starting_capital_usd"])
        for kind in [k.strip() for k in self.settings.strategies.split(",") if k.strip()]:
            strategy = self.factory(kind, self.sim)
            if strategy is None:
                continue
            if kind not in self.state.strategies:
                self.emit("strategy_registered", {"kind": kind, "name": strategy.name, "description": strategy.description,
                                                  "is_llm": strategy.is_llm}, kind)
                self.emit("cash_deposit", {"amount": str(capital), "by": "starting capital"}, kind)
            runner = Runner(self, kind, strategy)
            last = self.ledger.entries(self.sim["id"], ("decision",), kind, limit=1, newest_first=True)
            runner.last_decision_at = datetime.fromisoformat(last[0].recorded_at_utc) if last else None
            noted = [e for e in self.ledger.entries(self.sim["id"], ("decision",), kind, limit=30, newest_first=True)
                     if e.payload.get("notes")]
            runner.notes = noted[0].payload["notes"] if noted else ""
            self.runners[kind] = runner
        try:
            loop = asyncio.get_running_loop() if self.autostart else None
            for r in self.runners.values() if loop else ():
                r.task = loop.create_task(r.run())
        except RuntimeError:
            pass                                    # no loop (tests drive step() directly)
        self.sync_universe()
        log.info("engine_loaded", extra={"sim_id": self.sim["id"], "strategies": list(self.runners)})

    def _make_broker(self) -> None:
        s = self.state.settings
        self.broker = SimBroker(self.state, self.emit, self.market, self.profile,
                                extra_latency_ms=int(s.get("extra_latency_ms") or 0),
                                slippage_bps=D(str(s.get("slippage_bps") or 0)), risk_check=self.risk_check)

    def refresh_sim(self) -> None:
        active = self.sims.active()
        if (active or {}).get("id") != (self.sim or {}).get("id"):
            self.load()
        else:
            self.sim = active

    def emit(self, kind: str, payload: dict[str, Any], strategy_id: str | None = None) -> None:
        entry = self.ledger.append(self.sim["id"], kind, payload, strategy_id)
        apply_event(self.state, kind, strategy_id, payload)
        if kind == "fill":
            if strategy_id in self.runners:
                self.runners[strategy_id].wake("fill", {"ticker": payload["symbol"], "side": payload["side"],
                                                        "shares": payload["qty"], "price": payload["price"]})
        if kind in ("fill", "order_accepted", "order_rejected", "order_cancelled", "order_expired") and self.on_order:
            self.on_order(kind, {**payload, "strategy_id": strategy_id})
        if kind != "decision" or payload.get("orders") or payload.get("summary"):
            self.bus.publish("ledger." + kind, {"seq": entry.seq, "kind": kind, "strategy_id": strategy_id})

    # ------------------------------------------------------------------ operator actions
    def set_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        clean = {k: v for k, v in changes.items() if k in DEFAULT_SETTINGS}
        self.emit("settings_changed", {"settings": clean, "by": "operator"})
        self._make_broker()
        return self.state.settings

    def set_kill_switch(self, on: bool) -> None:
        self.emit("kill_switch", {"on": on, "by": "operator"})
        if on:
            for o in list(self.state.orders.values()):
                self.broker.cancel(o.id, "kill switch")

    def deposit(self, strategy_id: str | None, amount: Decimal) -> None:
        for sid in ([strategy_id] if strategy_id else list(self.state.accounts)):
            self.emit("cash_deposit", {"amount": str(amount), "by": "operator"}, sid)

    # ------------------------------------------------------------------ risk
    def risk_check(self, sid: str, req: OrderRequest, q: Quote, now: datetime) -> str | None:
        if req.symbol in self.state.blocked:
            return f"purchases blocked: {self.state.blocked[req.symbol]}"
        s = self.state.settings
        v = self.value(sid)
        if s.get("max_position_pct"):
            after = (self.state.accounts[sid].qty(req.symbol) + req.qty) * q.ask
            if v["equity"] > 0 and after / v["equity"] * 100 > D(str(s["max_position_pct"])):
                return f"risk limit: position would exceed {s['max_position_pct']}% of the account"
        if s.get("max_daily_loss_pct") and sid in self._day_start and self._day_start[sid] > 0:
            if (v["equity"] / self._day_start[sid] - 1) * 100 <= -D(str(s["max_daily_loss_pct"])):
                return f"risk limit: daily loss of {s['max_daily_loss_pct']}% reached, no new buys today"
        return None

    def _check_stop_loss(self, q: Quote) -> None:
        pct = self.state.settings.get("stop_loss_pct")
        if not pct:
            return
        for sid, a in self.state.accounts.items():
            pos = a.positions.get(q.symbol)
            if pos and q.bid <= pos.avg_cost * (1 - D(str(pct)) / 100) and self.broker.sellable(sid, q.symbol) > 0:
                self.broker.submit(sid, OrderRequest(q.symbol, "sell", self.broker.sellable(sid, q.symbol), meta={
                    "rationale": f"Automatic stop loss: price fell {pct}% below the average cost.", "source": "risk_manager"}),
                    q.recv_ts)

    # ------------------------------------------------------------------ valuation
    def value(self, sid: str) -> dict[str, Any]:
        a = self.state.accounts[sid]
        bids = {s: b for s in a.positions if (b := self.market.last_bid(s)) is not None}
        return valuation(a, bids, self.market.is_etf, self.market.tax_year(self.clock()), self.profile)

    def snapshot_equity(self, now: datetime) -> None:
        if not self.sim:
            return
        for sid in self.state.accounts:
            v = self.value(sid)
            if v["unpriced"]:
                continue                      # never record a fake dip while a holding has no price
            self.store.queue("INSERT INTO equity_points VALUES (?,?,?,?,?,?,?,?,?)",
                             (self.sim["id"], sid, iso(now), str(v["net_liquidation"]), str(v["equity"]), str(v["cash"]),
                              str(v["net_result"]), str(v["gross_result"]), str(v["costs"])))
        self._last_snapshot = now

    # ------------------------------------------------------------------ market data in
    def on_quote(self, q: Quote) -> None:
        self._check_overnight_jump(q)
        self.market.on_quote(q)
        self.store.add_quote(q)
        if not self.running or self.broker is None:
            return
        self.broker.on_quote(q)
        self._check_alerts(q)
        self._check_stop_loss(q)
        self._check_move(q)

    def _check_alerts(self, q: Quote) -> None:
        for al in [a for a in self.state.alerts.values() if a["symbol"] == q.symbol]:
            price = D(al["price"])
            if (al["direction"] == "above" and q.bid >= price) or (al["direction"] == "below" and q.ask <= price):
                self.emit("alert_triggered", {"id": al["id"], "quote": q.to_dict()}, al["strategy_id"])
                if al["strategy_id"] in self.runners:
                    self.runners[al["strategy_id"]].wake("price_alert", {"ticker": q.symbol, "direction": al["direction"],
                                                                         "price": al["price"], "note": al.get("note", "")})

    def _check_move(self, q: Quote) -> None:
        mid = (q.bid + q.ask) / 2
        ref = self._move_ref.get(q.symbol)
        if ref is None or q.recv_ts - ref[1] > MOVE_WINDOW:
            self._move_ref[q.symbol] = (mid, q.recv_ts)
            return
        change = mid / ref[0] - 1
        if abs(change) >= MOVE_THRESHOLD:
            self._move_ref[q.symbol] = (mid, q.recv_ts)
            for r in self.runners.values():
                if r.strategy.wants_events:
                    r.wake("price_move", {"ticker": q.symbol, "move_pct": round(float(change) * 100, 2), "within_minutes": 5})

    def on_bar(self, symbol: str, bar: dict[str, Any], source: str) -> None:
        self.market.on_bar(symbol, bar)
        self.store.add_bar(symbol, bar, source)

    def on_status(self, symbol: str, code: str, message: str, reason: str, recv_ts: datetime) -> None:
        change = self.market.on_status(symbol, code, message, reason, recv_ts)
        if change:
            self.store.feed_event(iso(recv_ts), "alpaca_quotes", f"ticker_{change}", f"{symbol}: {message} {reason}".strip())
            log.info("ticker_" + change, extra={"symbol": symbol, "message_": message, "reason": reason})

    def on_news(self, item: dict[str, Any]) -> bool:
        if not self.store.add_news(item):
            return False
        self.bus.publish("news", {"id": item["id"]})
        if item.get("impact") in ("medium", "high"):
            related = set(item.get("symbols", [])) | {l["symbol"] for l in item.get("links", [])}
            for sid, r in self.runners.items():
                interesting = related & (set(self.universe.symbols()) | set(self.state.accounts[sid].positions))
                if r.strategy.wants_events and interesting:
                    r.wake("filing" if item["kind"] == "filing" else "news",
                           {"id": item["id"], "tickers": sorted(interesting)[:6], "headline": item["headline"][:160]})
        return True

    # ------------------------------------------------------------------ corporate actions
    def _check_overnight_jump(self, q: Quote) -> None:
        """A price that moved more than 40% since the previous session with no known corporate action is not
        trusted: the symbol is frozen (no fills) until an action explains it or the operator releases it."""
        prev = self.market.quotes.get(q.symbol)
        session = self.market.session(q.recv_ts)
        if not prev or not session or prev.recv_ts >= session[0] or not q.valid() or not prev.valid():
            return
        ratio = (q.bid + q.ask) / (prev.bid + prev.ask)
        today = q.recv_ts.astimezone(NY).date().isoformat()
        if (ratio > Decimal("1.4") or ratio < Decimal("0.6")) and self._explained.get(q.symbol) != today:
            reason = f"price moved {float(ratio - 1) * 100:+.0f}% since the last session with no known corporate action"
            self.market.halted[q.symbol] = {"since": iso(q.recv_ts), "message": "Frozen by TradeLab", "reason": reason}
            self.store.feed_event(iso(q.recv_ts), "corporate_actions", "ticker_frozen", f"{q.symbol}: {reason}")
            log.error("ticker_frozen", extra={"symbol": q.symbol, "reason": reason})

    def release(self, symbol: str) -> None:
        """Operator decision: trust the price again and allow purchases."""
        self.market.halted.pop(symbol, None)
        if self.sim and symbol in self.state.blocked:
            self.emit("symbol_unblocked", {"symbol": symbol, "by": "operator"})

    def shares_before(self, sid: str, symbol: str, day: str) -> int:
        """Shares of `symbol` held at the start of New York day `day`, rebuilt from the ledger."""
        qty = 0
        for e in self.ledger.entries(self.sim["id"], ("fill", "split"), sid):
            p = e.payload
            if p["symbol"] != symbol:
                continue
            when = datetime.fromisoformat(p["quote"]["recv_ts"] if e.kind == "fill" else e.recorded_at_utc)
            if when.astimezone(NY).date().isoformat() >= day:
                break
            qty = int(p["qty_after"]) if e.kind == "split" else qty + (int(p["qty"]) if p["side"] == "buy" else -int(p["qty"]))
        return qty

    def process_corporate_actions(self, actions: list[dict[str, Any]], today: str) -> int:
        """Apply splits and cash dividends; anything else on a held stock blocks new purchases and alerts you."""
        if not self.sim or not self.broker:
            return 0
        applied = 0
        for act in sorted(actions, key=lambda a: a.get("ex_date") or ""):
            sym, kind, ex = act["symbol"], act["type"], act.get("ex_date")
            if not ex:
                continue
            if kind in ("forward_split", "reverse_split") and ex <= today:
                self._explained[sym] = today
                if self.market.halted.get(sym, {}).get("message") == "Frozen by TradeLab":
                    del self.market.halted[sym]
            for sid, a in self.state.accounts.items():
                key = f"{sid}:{act['id']}"
                if key in self.state.applied_actions:
                    continue
                base = {"action_id": act["id"], "symbol": sym, "ex_date": ex, "source": "Alpaca corporate actions"}
                if kind in ("forward_split", "reverse_split"):
                    before = self.shares_before(sid, sym, ex)
                    if ex > today or before <= 0 or a.qty(sym) <= 0:
                        continue
                    if act.get("new_symbol") and act["new_symbol"] != sym:
                        self.emit("corporate_action_unhandled", {**base, "type": kind, "reason": f"reverse split with a ticker change to {act['new_symbol']}"}, sid)
                        continue
                    for o in self.state.open_orders(sid, sym):
                        self.broker.cancel(o.id, "stock split")
                    new, old = D(str(act["new_rate"])), D(str(act["old_rate"]))
                    pre = min(before, a.qty(sym))
                    after = a.qty(sym) - pre + int(pre * new / old)
                    self.emit("split", {**base, "type": kind, "new_rate": str(new), "old_rate": str(old),
                                        "qty_before": a.qty(sym), "qty_after": after}, sid)
                    self.market.quotes.pop(sym, None)          # the last known price is pre-split: do not value with it
                    applied += 1
                elif kind == "cash_dividend":
                    pay = act.get("payable_date") or ex
                    if pay > today:
                        continue
                    qty = self.shares_before(sid, sym, ex)
                    if qty <= 0:
                        continue
                    gross = qty * D(str(act["rate"]))
                    us = (gross * self.profile.dividend_us_withholding).quantize(D("0.01"), ROUND_UP)
                    it = ((gross - us) * self.profile.dividend_italian_tax).quantize(D("0.01"), ROUND_UP)
                    self.emit("dividend", {**base, "payable_date": pay, "qty": qty, "rate": str(act["rate"]), "gross": str(gross),
                                           "us_withholding": str(us), "italian_tax": str(it), "net": str(gross - us - it)}, sid)
                    applied += 1
                elif ex <= today and a.qty(sym) > 0:
                    self.emit("corporate_action_unhandled", {**base, "type": kind, "reason": f"{kind.replace('_', ' ')} on {ex}: not simulated, check this holding"}, sid)
                    applied += 1
        return applied

    # ------------------------------------------------------------------ universe
    def sync_universe(self) -> None:
        before = set(self.universe.symbols())
        pinned = {o.symbol for o in self.state.orders.values()} | {a["symbol"] for a in self.state.alerts.values()}
        for a in self.state.accounts.values():
            pinned |= set(a.positions)
        self.universe.pin(pinned)
        self._notify_universe(before)

    def watch(self, symbol: str) -> tuple[bool, str]:
        why = self.market.tradable(symbol)
        if why:
            return False, why
        before = set(self.universe.symbols())
        ok, _ = self.universe.request(symbol)
        self._notify_universe(before)
        return ok, "" if ok else "no free live-data slot (every slot is pinned by a position, order or alert)"

    def _notify_universe(self, before: set[str]) -> None:
        after = set(self.universe.symbols())
        if after != before and self.on_universe_change:
            self.on_universe_change(sorted(after - before), sorted(before - after))

    # ------------------------------------------------------------------ strategy step
    def context(self, runner: Runner, wakes: list[dict[str, Any]], now: datetime) -> Context:
        sid = runner.sid
        since = iso(runner.last_decision_at) if runner.last_decision_at else iso(now - timedelta(hours=12))
        symbols = set(self.universe.symbols()) | set(self.state.accounts[sid].positions)
        news = self.store.news(limit=60, since=since, symbols=symbols) if runner.strategy.is_llm else []

        def charge(usage: dict[str, Any]) -> None:
            self.emit("token_cost", usage, sid)
            day = self.clock().date()
            prev = self._tokens_today.get(sid, (day, D(0)))
            self._tokens_today[sid] = (day, (prev[1] if prev[0] == day else D(0)) + D(usage["usd"]))

        def budget_left() -> Decimal | None:
            cap = self.state.settings.get("daily_token_budget_usd")
            if not cap:
                return None
            day, spent = self._tokens_today.get(sid, (None, D(0)))
            return D(str(cap)) - (spent if day == self.clock().date() else D(0))

        trades, relations = [], {}
        if runner.strategy.is_llm:
            for en in self.ledger.entries(self.sim["id"], ("fill",), sid, limit=10, newest_first=True):
                p = en.payload
                trades.append({"at": en.recorded_at_utc[:16], "side": p["side"], "ticker": p["symbol"], "shares": p["qty"],
                               "price": float(p["price"]),
                               **({"result_after_tax": round(float(D(p["realized_gain"]) - D(p["tax"])), 2)} if p["side"] == "sell" else {})})
            if self.relations:
                relations = {s: g for s in symbols if (g := self.relations(s))}
        return Context(now, sid, wakes, self.state.accounts[sid], self.value(sid), self.broker.available_cash(sid),
                       self.market, self.universe.symbols(), self.state.open_orders(sid),
                       [a for a in self.state.alerts.values() if a["strategy_id"] == sid], news, self.profile,
                       self.market.session(now), runner.last_decision_at, D(self.sim["starting_capital_usd"]),
                       charge, budget_left, runner.notes, trades, relations)

    async def step(self, runner: Runner, wakes: list[dict[str, Any]]) -> Decision | None:
        started = self.clock()
        ctx = self.context(runner, wakes, started)
        seen = {s: q.to_dict() for s in ctx.universe if (q := ctx.quote(s))}
        decision = await runner.strategy.decide(ctx)
        if decision is None:
            return None
        ready = self.clock()                       # the decision exists only now: LLM latency is included
        if not self.running:
            return None
        sid = runner.sid
        decision_id = uuid.uuid4().hex
        relevant = {o.symbol for o in decision.orders} | set(ctx.account.positions)
        self.emit("decision", {
            "id": decision_id, "wakes": wakes, "started_at": iso(started), "ready_at": iso(ready),
            "thinking_ms": int((ready - started).total_seconds() * 1000), "summary": decision.summary,
            "orders": [{"symbol": o.symbol, "side": o.side, "qty": o.qty, "type": o.type,
                        "limit_price": str(o.limit_price) if o.limit_price else None,
                        "stop_price": str(o.stop_price) if o.stop_price else None, "tif": o.tif, **o.meta}
                       for o in decision.orders],
            "cancel": decision.cancel, "alerts": decision.alerts, "watch": decision.watch,
            "next_wake_minutes": decision.next_wake_minutes, "notes": decision.notes,
            "quotes_seen": {s: q for s, q in seen.items() if s in relevant}, "detail": decision.detail,
        }, sid)
        runner.last_decision_at = ready
        if decision.notes is not None:
            runner.notes = decision.notes
        for oid in decision.cancel:
            o = self.state.orders.get(oid)
            if o and o.strategy_id == sid:
                self.broker.cancel(oid)
        for sym in decision.watch:
            ok, why = self.watch(sym)
            if not ok:
                self.emit("watch_rejected", {"symbol": sym, "reason": why}, sid)
        news_ids = decision.detail.get("news_ids", [])
        for req in decision.orders:
            req.meta = {**req.meta, "decision_id": decision_id, "summary": decision.summary, "news_ids": news_ids,
                        "wakes": [w["reason"] for w in wakes]}
            self.broker.submit(sid, req, ready)
        for al in decision.alerts:
            if self.market.tradable(al["symbol"]) is None:
                self.emit("alert_set", {"id": uuid.uuid4().hex, **al}, sid)
        if decision.next_wake_minutes:
            runner.next_wake_s = float(decision.next_wake_minutes) * 60
        self.sync_universe()
        return decision

    # ------------------------------------------------------------------ clock-driven housekeeping
    def tick(self, now: datetime, watched_feed_ok: bool = True) -> None:
        is_open = self.market.is_open(now)
        if self.sim and self.broker:
            if is_open and not self._was_open:
                self._day_start = {sid: self.value(sid)["equity"] for sid in self.state.accounts}
                for r in self.runners.values():
                    r.wake("market_open")
                if self.on_market_open:
                    self.on_market_open()
            if self._was_open and not is_open:
                self.broker.expire_day_orders()
                self._accrue_stamp_duty(now)
                self.snapshot_equity(now)
            if self.running and is_open and (self._last_snapshot is None or now - self._last_snapshot >= timedelta(seconds=60)):
                self.snapshot_equity(now)
        self._was_open = is_open
        if is_open and watched_feed_ok:
            suspended = {s for s in self.universe.symbols() if self.market.fresh_quote(s, now) is None}
            for s in suspended - self._suspended:
                self.store.feed_event(iso(now), "alpaca_quotes", "ticker_suspended", f"{s}: no fresh quote")
            for s in self._suspended - suspended:
                self.store.feed_event(iso(now), "alpaca_quotes", "ticker_resumed", s)
            self._suspended = suspended
        elif not is_open:
            self._suspended = set()

    def _accrue_stamp_duty(self, now: datetime) -> None:
        """0.2% a year on the value of holdings, accrued at each close for the calendar days elapsed."""
        today = now.date()
        for sid, a in self.state.accounts.items():
            v = self.value(sid)
            last = self._last_stamp.get(sid)
            days = max(1, (today - datetime.fromisoformat(last).date()).days) if last else 1
            if last == today.isoformat() or v["market_value"] <= 0:
                continue
            usd = (v["market_value"] * self.profile.stamp_duty_annual_rate * days / 365).quantize(D("0.0001"))
            self.emit("stamp_duty", {"usd": str(usd), "basis_value": str(v["market_value"]), "days": days,
                                     "date": today.isoformat()}, sid)
            self._last_stamp[sid] = today.isoformat()
