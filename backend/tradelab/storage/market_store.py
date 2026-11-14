"""Market data and derived series (separate SQLite file; the ledger file stays small and fsync'd).

Everything is stored with OUR receive timestamp. Quotes are conflated to at
most one row per symbol per second; every quote actually used for a fill or a
decision is stored in full inside the ledger entry itself.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (symbol TEXT, recv_ts TEXT, bid TEXT, bid_size INT, ask TEXT, ask_size INT,
    source TEXT, exch_ts TEXT);
CREATE INDEX IF NOT EXISTS quotes_i ON quotes(symbol, recv_ts);
CREATE TABLE IF NOT EXISTS bars (symbol TEXT, bar_ts TEXT, recv_ts TEXT, o TEXT, h TEXT, l TEXT, c TEXT, v INT,
    source TEXT, PRIMARY KEY (symbol, bar_ts));
CREATE TABLE IF NOT EXISTS news (id TEXT PRIMARY KEY, kind TEXT, source TEXT, recv_ts TEXT, published_ts TEXT,
    headline TEXT, summary TEXT, url TEXT, symbols TEXT, links TEXT, impact TEXT, extra TEXT);
CREATE INDEX IF NOT EXISTS news_i ON news(recv_ts);
CREATE TABLE IF NOT EXISTS feed_events (ts TEXT, feed TEXT, event TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS equity_points (sim_id TEXT, strategy_id TEXT, ts TEXT, net_liquidation TEXT, equity TEXT,
    cash TEXT, net_result TEXT, gross_result TEXT, costs TEXT);
CREATE INDEX IF NOT EXISTS equity_i ON equity_points(sim_id, strategy_id, ts);
CREATE TABLE IF NOT EXISTS paper_orders (order_id TEXT PRIMARY KEY, sim_id TEXT, strategy_id TEXT, symbol TEXT,
    side TEXT, qty INT, submitted_ts TEXT, status TEXT, paper_status TEXT, paper_filled_qty INT, paper_avg_price TEXT,
    sim_filled_qty INT, sim_avg_price TEXT, deviation_bps TEXT, detail TEXT, updated_ts TEXT);
CREATE TABLE IF NOT EXISTS company_graph (symbol TEXT PRIMARY KEY, built_ts TEXT, source TEXT, model TEXT,
    cost_usd TEXT, graph TEXT);
CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, sim_id TEXT, created_ts TEXT, report TEXT);
CREATE TABLE IF NOT EXISTS platform_costs (ts TEXT, purpose TEXT, model TEXT, usd TEXT, detail TEXT);
"""


class MarketStore:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._lock = threading.RLock()
        self._pending: list[tuple[str, tuple]] = []
        self._last_quote_second: dict[str, str] = {}

    def exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self.conn.execute(sql, params)

    def queue(self, sql: str, params: tuple) -> None:
        with self._lock:
            self._pending.append((sql, params))

    def flush(self) -> int:
        with self._lock:
            batch, self._pending = self._pending, []
            if not batch:
                return 0
            self.conn.execute("BEGIN")
            for sql, params in batch:
                self.conn.execute(sql, params)
            self.conn.execute("COMMIT")
            return len(batch)

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # ---- writers
    def add_quote(self, q) -> None:
        second = q.recv_ts.isoformat(timespec="seconds")
        if self._last_quote_second.get(q.symbol) == second:
            return
        self._last_quote_second[q.symbol] = second
        self.queue("INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?)",
                   (q.symbol, q.recv_ts.isoformat(), str(q.bid), q.bid_size, str(q.ask), q.ask_size, q.source, q.exch_ts))

    def add_bar(self, symbol: str, b: dict[str, Any], source: str) -> None:
        self.queue("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?)",
                   (symbol, b["t"], b["recv_ts"].isoformat(), str(b["o"]), str(b["h"]), str(b["l"]), str(b["c"]),
                    int(b["v"]), source))

    def add_news(self, n: dict[str, Any]) -> bool:
        """Returns False if this id was already stored (duplicates are ignored)."""
        with self._lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO news VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (n["id"], n["kind"], n["source"], n["recv_ts"], n.get("published_ts"), n["headline"],
                 n.get("summary", ""), n.get("url", ""), json.dumps(n.get("symbols", [])),
                 json.dumps(n.get("links", [])), n.get("impact", "low"), json.dumps(n.get("extra", {}))))
            return cur.rowcount == 1

    def news(self, limit: int = 100, since: str | None = None, symbols: set[str] | None = None) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM news WHERE recv_ts > ? ORDER BY recv_ts DESC LIMIT ?",
                          (since or "", limit if not symbols else limit * 20))
        out = []
        for r in rows:
            r["symbols"], r["links"], r["extra"] = json.loads(r["symbols"]), json.loads(r["links"]), json.loads(r["extra"])
            if symbols and not (set(r["symbols"]) | {l["symbol"] for l in r["links"]}) & symbols:
                continue
            out.append(r)
        return out[:limit]

    def feed_event(self, ts: str, feed: str, event: str, detail: str = "") -> None:
        self.queue("INSERT INTO feed_events VALUES (?,?,?,?)", (ts, feed, event, detail))

    def last_quotes(self) -> list[dict[str, Any]]:
        return self.query("SELECT q.* FROM quotes q JOIN (SELECT symbol, MAX(recv_ts) m FROM quotes GROUP BY symbol) x"
                          " ON q.symbol = x.symbol AND q.recv_ts = x.m")

    def close(self) -> None:
        self.flush()
        self.conn.close()
