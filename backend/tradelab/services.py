"""Builds and runs every long-lived component: feeds, engine, cross-check, background jobs."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from .bus import EventBus
from .clock import iso, now_utc
from .config import Settings
from .crosscheck import PaperMirror
from .engine import Engine
from .feeds import FeedRegistry, FeedStatus
from .graph import CompanyGraph
from .ingest import news as news_mod
from .ingest.alpaca import STREAM_HOST, AlpacaRest, AlpacaStream, parse_bar, parse_quote
from .llm import LLM
from .market import NY, MarketState
from .metrics import daily_index, daily_returns, strategy_metrics, weekly_report
from .models import Quote
from .simulations import Simulations
from .storage.db import Database, Ledger
from .storage.market_store import MarketStore
from .strategies import tradingagents_adapter
from .strategies.baselines import BuyAndHold, MeanReversion, Momentum, RandomTrader
from .strategies.llm_agent import LLMLightAgent

log = logging.getLogger(__name__)
STALL_AFTER = timedelta(seconds=30)


class Services:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings.db_path)
        self.ledger = Ledger(self.db)
        self.sims = Simulations(self.db, self.ledger)
        self.bus = EventBus()
        self.store = MarketStore(settings.market_db if settings.db_path != ":memory:" else ":memory:")
        self.market = MarketState(settings.quote_stale_seconds)
        self.llm = LLM(settings)
        self.graph = CompanyGraph(self.store)
        self.feeds = FeedRegistry()
        feed = settings.alpaca_feed.upper()
        self.f_quotes = self.feeds.register(FeedStatus("alpaca_quotes", f"Alpaca {feed} websocket (quotes, 1-min bars, halts)"))
        self.f_news = self.feeds.register(FeedStatus("alpaca_news", "Alpaca news websocket (Benzinga and others)"))
        self.f_edgar = self.feeds.register(FeedStatus("sec_edgar", "SEC EDGAR latest filings (8-K, 10-Q, 10-K)"))
        self.f_rss = self.feeds.register(FeedStatus("world_news", "World news feeds (BBC, MarketWatch, Federal Reserve)"))
        self.f_gdelt = self.feeds.register(FeedStatus("gdelt", "GDELT world news (15-minute knowledge-graph files)"))
        self.f_ref = self.feeds.register(FeedStatus("alpaca_reference", "Alpaca trading calendar and asset list"))
        self.engine = Engine(settings, self.ledger, self.sims, self.bus, self.market, self.store, self.make_strategy)
        self.rest: AlpacaRest | None = None
        self.quote_stream: AlpacaStream | None = None
        self.mirror: PaperMirror | None = None
        self.tasks: list[asyncio.Task] = []
        self.malformed = 0
        self._news_backfilled = False
        self.strategy_notes: dict[str, str] = {}
        self._kick = asyncio.Event()                  # set at the open: check corporate actions right now
        self.engine.relations = lambda sym: self.graph.graphs.get(sym)
        self.f_ca = self.feeds.register(FeedStatus("corporate_actions", "Alpaca corporate actions (splits, dividends) and daily history"))

    # ------------------------------------------------------------------ strategies
    def make_strategy(self, kind: str, sim: dict[str, Any]):
        s = self.settings
        if kind == "buy_hold":
            return BuyAndHold()
        if kind == "random":
            return RandomTrader(seed=int(sim["id"][:8], 16))
        if kind == "momentum":
            return Momentum()
        if kind == "mean_reversion":
            return MeanReversion()
        if kind == "llm_light":
            if not self.llm.configured:
                self.strategy_notes[kind] = "not running: ICA_API_KEY is missing"
                return None
            for m in (s.llm_triage_model, s.llm_decision_model):
                if m not in s.llm_prices:
                    self.strategy_notes[kind] = f"not running: no token price configured for {m}"
                    return None
            return LLMLightAgent(self.llm, s.llm_triage_model, s.llm_decision_model, s.llm_agent_interval_minutes)
        if kind == "tradingagents":
            if not tradingagents_adapter.available():
                self.strategy_notes[kind] = "not installed (build with INSTALL_TRADINGAGENTS=1)"
                return None
            if not self.llm.configured:
                self.strategy_notes[kind] = "not running: ICA_API_KEY is missing"
                return None
            return tradingagents_adapter.TradingAgentsStrategy(s)
        self.strategy_notes[kind] = "unknown strategy"
        return None

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        for r in self.store.last_quotes():           # last known prices, for valuation after a restart (they are stale)
            self.market.on_quote(Quote(r["symbol"], Decimal(r["bid"]), r["bid_size"], Decimal(r["ask"]), r["ask_size"],
                                       datetime.fromisoformat(r["recv_ts"]), 0, r["source"], r["exch_ts"]))
        self.engine.load()
        self._spawn(self._tick_loop())
        if not self.settings.ingest_enabled:
            return
        s = self.settings
        key, secret = s.alpaca_api_key.get_secret_value(), s.alpaca_api_secret.get_secret_value()
        if key and secret:
            self.rest = AlpacaRest(key, secret, s.alpaca_feed)
            self._spawn(self._reference_loop())
            self._spawn(self._corporate_actions_loop())
            self._spawn(self._history_loop())
            self.engine.on_market_open = lambda: self._kick.set()
            self.quote_stream = AlpacaStream(f"{STREAM_HOST}/v2/{s.alpaca_feed}", key, secret, self.f_quotes,
                                             self._on_stock_message, self._channels, self._on_feed_state("alpaca_quotes"))
            self.engine.on_universe_change = self._on_universe_change
            self._spawn(self.quote_stream.run())
            news_stream = AlpacaStream(f"{STREAM_HOST}/v1beta1/news", key, secret, self.f_news, self._on_news_message,
                                       lambda: {"news": ["*"]}, self._on_feed_state("alpaca_news"))
            self._spawn(news_stream.run())
            if s.paper_crosscheck:
                self.mirror = PaperMirror(self.rest, self.store, lambda: (self.engine.sim or {}).get("id"))
                self.engine.on_order = self.mirror.on_order
                self._spawn(self.mirror.run())
        else:
            for f in (self.f_quotes, self.f_news, self.f_ref):
                f.state, f.detail = "error", "ALPACA_API_KEY / ALPACA_API_SECRET missing in .env"
        if s.sec_user_agent_email:
            sec = news_mod.SecClient(s.sec_user_agent_email)
            self._spawn(news_mod.edgar_loop(sec, self.f_edgar, self._on_item, s.edgar_poll_seconds))
            if self.llm.configured:
                self._spawn(self._graph_loop(sec))
        else:
            self.f_edgar.state, self.f_edgar.detail = "error", "SEC_USER_AGENT_EMAIL missing in .env (the SEC requires a contact)"
        if s.rss_feeds:
            self._spawn(news_mod.rss_loop(self.f_rss, self._on_item, s.rss_feeds, s.world_news_poll_seconds))
        self._spawn(news_mod.gdelt_loop(self.f_gdelt, self._on_gdelt, s.gdelt_poll_seconds))
        self._spawn(self._report_loop())

    def _spawn(self, coro) -> None:
        self.tasks.append(asyncio.get_running_loop().create_task(coro))

    async def stop(self) -> None:
        for t in self.tasks + [r.task for r in self.engine.runners.values() if r.task]:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.store.close()
        self.db.close()

    # ------------------------------------------------------------------ feed callbacks
    def _on_feed_state(self, feed: str):
        return lambda state, detail: self.store.feed_event(iso(now_utc()), feed, state, detail)

    def _channels(self) -> dict[str, list[str]]:
        syms = self.engine.universe.symbols()
        return {"quotes": syms, "bars": syms, "statuses": syms}

    def _on_universe_change(self, added: list[str], removed: list[str]) -> None:
        if not self.quote_stream:
            return
        for action, syms in (("unsubscribe", removed), ("subscribe", added)):
            if syms:
                asyncio.run_coroutine_threadsafe(
                    self.quote_stream.send({"action": action, "quotes": syms, "bars": syms, "statuses": syms}), self.loop)
        if added:
            self._kick.set()                      # new ticker: fetch its corporate actions now

    def _on_stock_message(self, m: dict[str, Any], recv_ts: datetime) -> None:
        if self.f_quotes.state == "stalled":
            self.f_quotes.state, self.f_quotes.detail = "live", "recovered"
            self.store.feed_event(iso(recv_ts), "alpaca_quotes", "live", "data flowing again")
        t = m.get("T")
        source = f"Alpaca {self.settings.alpaca_feed.upper()}"
        if t == "q":
            q = parse_quote(m, recv_ts, source)
            if q:
                self.engine.on_quote(q)
            else:
                self._malformed(m)
        elif t == "b":
            bar = parse_bar(m, recv_ts)
            if bar and m.get("S"):
                self.engine.on_bar(m["S"], bar, source)
            else:
                self._malformed(m)
        elif t == "s" and m.get("S"):
            self.engine.on_status(m["S"], str(m.get("sc", "")), str(m.get("sm", "")), str(m.get("rm", "")), recv_ts)

    def _malformed(self, m: dict[str, Any]) -> None:
        self.malformed += 1
        if self.malformed <= 20:
            log.warning("malformed_market_message", extra={"msg": json.dumps(m, default=str)[:300]})

    def _on_news_message(self, m: dict[str, Any], recv_ts: datetime) -> None:
        item = news_mod.from_alpaca(m, recv_ts)
        if item:
            self._on_item(item)

    def _on_item(self, item: dict[str, Any]) -> bool:
        return self.engine.on_news(news_mod.finish(item, self.graph))

    def _on_gdelt(self, item: dict[str, Any], only_if_linked: bool) -> bool:
        """Keep a GDELT story if it touches a followed company; macro stories are stored on the second call."""
        done = news_mod.finish(item, self.graph)
        if only_if_linked and not (done["symbols"] or done["links"]):
            return False
        # GDELT is broad and noisy: only a story with a market-moving word in its headline may wake an agent.
        strong = bool(news_mod.HIGH_WORDS.search(done["headline"]) or news_mod.MACRO_WORDS.search(done["headline"]))
        done["impact"] = "medium" if strong and "late_minutes" not in done["extra"] else "low"
        self.engine.on_news(done)
        return True

    # ------------------------------------------------------------------ background loops
    async def _reference_loop(self) -> None:
        while True:
            try:
                now = now_utc()
                start = (now - timedelta(days=2)).date().isoformat()
                self.market.set_calendar(await self.rest.calendar(start, (now + timedelta(days=14)).date().isoformat()), now)
                self.market.set_assets(await self.rest.assets())
                self.f_ref.state, self.f_ref.last_received_at_utc = "live", iso(now_utc())
                self.f_ref.detail = f"{len(self.market.assets)} assets, calendar to {max(self.market.calendar)}"
                if not self._news_backfilled:
                    # Real articles published while we were not connected. They are stored with their true
                    # publication time and OUR receive time, so they count as late and never wake an agent.
                    start = (now - timedelta(hours=36)).isoformat(timespec="seconds").replace("+00:00", "Z")
                    for m in reversed(await self.rest.recent_news(self._tracked(), start)):
                        self._on_news_message({**m, "T": "n"}, now_utc())
                    self._news_backfilled = True
                await asyncio.sleep(6 * 3600)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.f_ref.state, self.f_ref.detail = "error", f"{type(e).__name__}: {e}"[:200]
                log.warning("reference_refresh_failed", extra={"error": str(e)[:200]})
                await asyncio.sleep(60)

    def _tracked(self) -> list[str]:
        syms = set(self.engine.universe.symbols())
        for a in self.engine.state.accounts.values():
            syms |= set(a.positions)
        return sorted(syms)

    async def _corporate_actions_loop(self) -> None:
        """Splits and dividends for every watched or held ticker: at start, every 2 hours and at each market open."""
        while True:
            try:
                now = now_utc().astimezone(NY)
                acts = await self.rest.corporate_actions(self._tracked(), (now - timedelta(days=45)).date().isoformat(),
                                                         (now + timedelta(days=3)).date().isoformat())
                n = self.engine.process_corporate_actions(acts, now.date().isoformat())
                self.f_ca.state, self.f_ca.last_received_at_utc = "live", iso(now_utc())
                self.f_ca.detail = f"{len(acts)} actions known for {len(self._tracked())} tickers; checked every 2 h and at the open"
                if n:
                    self.bus.publish("corporate_actions", {"applied": n})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.f_ca.state, self.f_ca.detail = "error", f"{type(e).__name__}: {e}"[:200]
                log.warning("corporate_actions_failed", extra={"error": str(e)[:200]})
            self._kick.clear()
            try:
                await asyncio.wait_for(self._kick.wait(), 7200 if self.f_ca.state == "live" else 120)
            except asyncio.TimeoutError:
                pass

    async def _history_loop(self) -> None:
        """Adjusted daily closes (completed days only) so the agents can see each stock's recent past."""
        while True:
            wait = 6 * 3600
            try:
                now = now_utc()
                bars = await self.rest.daily_bars(self._tracked(), (now - timedelta(days=400)).date().isoformat(),
                                                  (now - timedelta(minutes=20)).isoformat(timespec="seconds").replace("+00:00", "Z"))
                today = now.astimezone(NY).date().isoformat()
                closes = {s: [float(b["c"]) for b in rows if b["t"][:10] < today] for s, rows in bars.items()}
                self.market.set_daily(closes, now_utc())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                wait = 300
                log.warning("history_failed", extra={"error": f"{type(e).__name__}: {e}"[:200]})
            await asyncio.sleep(wait)

    async def _tick_loop(self) -> None:
        n = 0
        while True:
            await asyncio.sleep(1)
            n += 1
            try:
                now = now_utc()
                qs = self.quote_stream
                feed_ok = self.f_quotes.state == "live"
                if qs and feed_ok and self.market.is_open(now) and qs.last_message_at and now - qs.last_message_at > STALL_AFTER:
                    self.f_quotes.state = "stalled"
                    self.f_quotes.detail = f"no data for {int((now - qs.last_message_at).total_seconds())}s while the market is open"
                    self.store.feed_event(iso(now), "alpaca_quotes", "stalled", self.f_quotes.detail)
                    log.error("feed_stalled", extra={"feed": "alpaca_quotes"})
                    feed_ok = False
                self.engine.tick(now, feed_ok)
                self.store.flush()
                if n % 30 == 0 and self.engine.sim:
                    self.engine.sync_universe()
            except Exception:
                log.exception("tick_failed")

    async def _graph_loop(self, sec: news_mod.SecClient) -> None:
        """Build the relationship graph once per core company (skips funds); cached forever in the market DB."""
        await asyncio.sleep(5)
        try:
            by_ticker = {t: c for c, t in (await sec.tickers()).items()}
        except Exception as e:
            log.warning("graph_tickers_failed", extra={"error": str(e)[:200]})
            return
        for sym in self.settings.core_list:
            if sym in self.graph.graphs or sym in self.market.etf_overrides or sym not in by_ticker:
                continue
            try:
                await self.graph.build(sym, by_ticker[sym], sec, self.llm, self.settings.llm_triage_model)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("graph_build_failed", extra={"symbol": sym, "error": f"{type(e).__name__}: {e}"[:200]})

    async def _report_loop(self) -> None:
        while True:
            await asyncio.sleep(1800)
            try:
                now = now_utc().astimezone(NY)
                week = now.strftime("%G-W%V")
                if now.weekday() == 5 and self.engine.sim:
                    rid = f"{self.engine.sim['id']}:{week}"
                    if not self.store.query("SELECT 1 FROM reports WHERE id=?", (rid,)):
                        self.store.exec("INSERT INTO reports VALUES (?,?,?,?)",
                                        (rid, self.engine.sim["id"], iso(now_utc()), json.dumps({"week": week, **self.report()})))
                        log.info("weekly_report_stored", extra={"week": week})
            except Exception:
                log.exception("report_failed")

    # ------------------------------------------------------------------ read models for the API
    def points(self, sid: str, limit: int | None = None) -> list[dict[str, Any]]:
        if not self.engine.sim:
            return []
        return self.store.query("SELECT ts, net_result, net_liquidation FROM equity_points WHERE sim_id=? AND strategy_id=? ORDER BY ts",
                                (self.engine.sim["id"], sid))

    def overview(self) -> dict[str, Any]:
        e, now = self.engine, now_utc()
        sim = e.sim
        rows = []
        if sim and e.broker:
            starting = Decimal(sim["starting_capital_usd"])
            for sid, a in e.state.accounts.items():
                v = e.value(sid)
                pts = self.points(sid)
                meta = e.state.strategies[sid]
                stale = [s for s in a.positions if e.market.fresh_quote(s, now) is None]
                rows.append({
                    "id": sid, "name": meta["name"], "description": meta.get("description", ""), "is_llm": meta.get("is_llm", False),
                    "active": sid in e.runners, "thinking": e.runners[sid].busy if sid in e.runners else False,
                    "net_result": float(v["net_result"]), "gross_result": float(v["gross_result"]),
                    "net_liquidation": float(v["net_liquidation"]), "equity": float(v["equity"]), "cash": float(v["cash"]),
                    "deposits": float(a.deposits), "dividends": float(a.dividends), "positions": len(a.positions), "open_orders": len(e.state.open_orders(sid)),
                    "costs": {"commissions": float(a.commissions), "taxes": float(a.taxes), "tokens": float(a.token_cost),
                              "stamp_duty": float(a.stamp_duty), "exit_costs_if_sold_now": float(v["exit_costs"])},
                    "prices_stale_for": stale if not e.market.is_open(now) else [], "prices_missing_for": v["unpriced"],
                    "suspended_positions": stale if e.market.is_open(now) else [],
                    **strategy_metrics(a, v, pts, starting)})
            bh = next((r for r in rows if r["id"] == "buy_hold"), None)
            for r in rows:
                r["vs_buy_hold_pct"] = r["return_pct"] - bh["return_pct"] if bh else None
            rows.sort(key=lambda r: r["net_result"], reverse=True)
        return {"simulation": sim, "market": e.market.describe(now), "strategies": rows, "blocked": e.state.blocked,
                "frozen": {s: h["reason"] for s, h in e.market.halted.items() if h.get("message") == "Frozen by TradeLab"},
                "kill_switch": e.state.kill_switch, "settings": e.state.settings if sim else {},
                "not_running": self.strategy_notes, "server_time_utc": iso(now),
                "broker": {"name": e.profile.name, "commission_per_order": float(e.profile.commission_per_order),
                           "tax_rate_pct": float(e.profile.capital_gains_tax_rate * 100), "source": e.profile.source}}

    def equity_curves(self, max_points: int = 400) -> dict[str, list[list[Any]]]:
        if not self.engine.sim:
            return {}
        starting = float(self.engine.sim["starting_capital_usd"])
        out = {}
        for sid in self.engine.state.accounts:
            pts = self.points(sid)
            step = max(1, len(pts) // max_points)
            series = [[p["ts"], starting + float(p["net_result"])] for p in pts[::step]]
            if pts and pts[-1]["ts"] != (series[-1][0] if series else None):
                series.append([pts[-1]["ts"], starting + float(pts[-1]["net_result"])])
            out[sid] = series
        return out

    def report(self) -> dict[str, Any]:
        e = self.engine
        rows = []
        if e.sim and e.broker:
            starting = Decimal(e.sim["starting_capital_usd"])
            for sid, a in e.state.accounts.items():
                v = e.value(sid)
                rows.append({"id": sid, "name": e.state.strategies[sid]["name"], "is_llm": e.state.strategies[sid].get("is_llm", False),
                             "net_result": float(v["net_result"]), "return_pct": float(v["net_result"]) / float(a.deposits) * 100,
                             "daily": dict(daily_returns(daily_index(self.points(sid), starting))),
                             "closed_trades": a.closed_trades, "winning_trades": a.winning_trades})
        return {"created_at_utc": iso(now_utc()), **weekly_report(rows)}

    def strategy_detail(self, sid: str) -> dict[str, Any]:
        e, now = self.engine, now_utc()
        a = e.state.accounts[sid]
        positions = []
        for sym, pos in a.positions.items():
            q = e.market.quotes.get(sym)
            positions.append({
                "symbol": sym, "name": e.market.name(sym), "qty": pos.qty, "avg_cost": float(pos.avg_cost),
                "bid": float(q.bid) if q else None, "value": float(pos.qty * q.bid) if q else None,
                "unrealized": float(pos.qty * q.bid - pos.basis) if q else None,
                "price_source": q.source if q else None, "price_received_at_utc": iso(q.recv_ts) if q else None,
                "price_is_fresh": bool(q and e.market.fresh_quote(sym, now)), "halted": e.market.is_halted(sym)})
        return {"id": sid, "positions": positions,
                "open_orders": [o.to_dict() for o in e.state.open_orders(sid)],
                "alerts": [al for al in e.state.alerts.values() if al["strategy_id"] == sid],
                "tax_loss_credit": float(a.loss_credit(e.market.tax_year(now)))}

    def activity(self, limit: int = 60, sid: str | None = None) -> list[dict[str, Any]]:
        e = self.engine
        if not e.sim:
            return []
        kinds = ("fill", "decision", "order_rejected", "order_cancelled", "order_expired", "cash_deposit", "kill_switch",
                 "settings_changed", "alert_triggered", "strategy_error", "split", "dividend", "corporate_action_unhandled",
                 "symbol_unblocked")
        entries = self.ledger.entries(e.sim["id"], kinds, sid, limit=limit * 3, newest_first=True)
        news_ids: set[str] = set()
        out = []
        for en in entries:
            p = en.payload
            row: dict[str, Any] = {"seq": en.seq, "at": en.recorded_at_utc, "kind": en.kind, "strategy_id": en.strategy_id,
                                   "strategy": e.state.strategies.get(en.strategy_id or "", {}).get("name")}
            if en.kind == "fill":
                meta = p.get("meta", {})
                row.update(symbol=p["symbol"], side=p["side"], qty=p["qty"], price=float(p["price"]),
                           commission=float(p["commission"]), tax=float(p.get("tax", 0)),
                           realized_gain=float(p["realized_gain"]) if "realized_gain" in p else None,
                           rationale=meta.get("rationale"), summary=meta.get("summary"), confidence=meta.get("confidence"),
                           wakes=meta.get("wakes", []), news_ids=meta.get("news_ids", [])[:5],
                           quote={"bid": p["quote"]["bid"], "ask": p["quote"]["ask"], "source": p["quote"]["source"],
                                  "recv_ts": p["quote"]["recv_ts"]}, by_risk_manager=meta.get("source") == "risk_manager")
                news_ids |= set(row["news_ids"])
            elif en.kind == "decision":
                if not p.get("summary") and not p.get("orders"):
                    continue
                d = p.get("detail", {})
                calls = d.get("llm_calls", [])
                row.update(summary=p.get("summary"), orders=len(p.get("orders", [])), thinking_ms=p.get("thinking_ms"),
                           wakes=[w["reason"] for w in p.get("wakes", [])],
                           token_cost=sum(float(c["usd"]) for c in calls), models=[c["model"] for c in calls],
                           error=d.get("error"))
            elif en.kind == "order_rejected":
                row.update(symbol=p["symbol"], side=p["side"], qty=p["qty"], reason=p["reason"],
                           rationale=(p.get("meta") or {}).get("rationale"))
            elif en.kind in ("order_cancelled", "order_expired"):
                row.update(reason=p.get("reason"), unfilled_qty=p.get("unfilled_qty"))
            elif en.kind == "cash_deposit":
                if p.get("by") != "operator":
                    continue
                row.update(amount=float(p["amount"]))
            elif en.kind == "dividend":
                row.update(symbol=p["symbol"], amount=float(p["net"]), qty=p["qty"],
                           tax=float(p["us_withholding"]) + float(p["italian_tax"]))
            elif en.kind == "split":
                row.update(symbol=p["symbol"], qty=p["qty_after"], reason=f"{p['new_rate']}-for-{p['old_rate']} split: {p['qty_before']} shares became {p['qty_after']}")
            elif en.kind in ("corporate_action_unhandled", "symbol_unblocked"):
                row.update(symbol=p["symbol"], reason=p.get("reason", "released by you"))
            else:
                row.update(detail=p)
            out.append(row)
            if len(out) >= limit:
                break
        if news_ids:
            marks = ",".join("?" * len(news_ids))
            titles = {r["id"]: r for r in self.store.query(f"SELECT id, headline, source, url, recv_ts FROM news WHERE id IN ({marks})", tuple(news_ids))}
            for row in out:
                if row.get("news_ids"):
                    row["news"] = [titles[i] for i in row["news_ids"] if i in titles]
        return out

    def relationship_map(self, hours: int = 24, max_news: int = 30) -> dict[str, Any]:
        """Nodes and links between recent news, the tickers they touch and the companies related to them."""
        e, now = self.engine, now_utc()
        recent = self.store.news(200, since=iso(now - timedelta(hours=hours)))
        items = [n for n in recent if n["symbols"] or n["links"]][:max_news]
        if not items:
            items = [n for n in self.store.news(300) if n["symbols"] or n["links"]][:max_news]
        held: dict[str, list[str]] = {}
        for sid, a in e.state.accounts.items():
            for sym in a.positions:
                held.setdefault(sym, []).append(e.state.strategies[sid]["name"])
        nodes: dict[str, dict[str, Any]] = {}
        links: list[dict[str, str]] = []

        def ticker(sym: str) -> str:
            nodes.setdefault("T:" + sym, {"id": "T:" + sym, "type": "ticker", "label": sym, "name": e.market.name(sym),
                                          "held_by": held.get(sym, []), "watched": sym in e.universe.symbols()})
            return "T:" + sym

        for sym in held:
            ticker(sym)
        for n in items:
            nid = "N:" + n["id"]
            nodes[nid] = {"id": nid, "type": n["kind"], "label": n["headline"][:140], "impact": n["impact"],
                          "source": n["source"], "url": n["url"], "received_at_utc": n["recv_ts"]}
            for sym in n["symbols"][:6]:
                links.append({"a": nid, "b": ticker(sym), "kind": "about"})
            for l in n["links"][:6]:
                links.append({"a": nid, "b": ticker(l["symbol"]), "kind": l["relation"], "via": l["via"]})
        present = {k[2:] for k in nodes if k.startswith("T:")}
        for sym in list(present):
            g = self.graph.graphs.get(sym) or {}
            for rel in ("suppliers", "customers", "competitors"):
                for c in g.get(rel, []):
                    other = str(c.get("ticker") or "").upper()
                    if other and other != sym and (other in present or sym in held):
                        links.append({"a": ticker(sym), "b": ticker(other), "kind": rel[:-1], "via": c.get("name", other)})
        return {"nodes": list(nodes.values()), "links": links, "generated_at_utc": iso(now),
                "note": "Company relationships come from each company's latest annual report (10-K), read once by the AI."}

    def quotes(self) -> list[dict[str, Any]]:
        e, now = self.engine, now_utc()
        out = []
        for sym in e.universe.symbols():
            q = e.market.quotes.get(sym)
            out.append({"symbol": sym, "name": e.market.name(sym), "core": sym in e.universe.core,
                        "bid": float(q.bid) if q else None, "ask": float(q.ask) if q else None,
                        "bid_size": q.bid_size if q else None, "ask_size": q.ask_size if q else None,
                        "source": q.source if q else None, "received_at_utc": iso(q.recv_ts) if q else None,
                        "age_seconds": (now - q.recv_ts).total_seconds() if q else None,
                        "fresh": bool(q and e.market.fresh_quote(sym, now)), "halted": e.market.is_halted(sym)})
        return out

    def status(self) -> dict[str, Any]:
        now = now_utc()
        s = self.settings
        platform = self.store.query("SELECT COALESCE(SUM(CAST(usd AS REAL)), 0) usd, COUNT(*) n FROM platform_costs")[0]
        events = self.store.query("SELECT * FROM feed_events ORDER BY ts DESC LIMIT 25")
        return {
            "server_time_utc": iso(now), "live_trading": False,
            "price_feed": {"provider": "alpaca", "feed": s.alpaca_feed, "full_market": s.alpaca_feed == "sip",
                           "streamed_symbols": len(self.engine.universe.symbols()), "capacity": s.capacity},
            "market": self.market.describe(now), "feeds": self.feeds.snapshot(), "credentials": s.credentials_present(),
            "halted": self.market.halted, "ledger_entries": self.ledger.count(), "malformed_messages": self.malformed,
            "feed_events": events, "company_graphs": len(self.graph.graphs),
            "platform_llm_cost": {"usd": platform["usd"], "calls": platform["n"],
                                  "note": "one-off cost of building the company graph; not charged to any strategy"},
            "llm": {"triage_model": s.llm_triage_model, "decision_model": s.llm_decision_model, "prices_usd_per_million": s.llm_prices},
        }
