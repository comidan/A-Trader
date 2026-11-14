"""SQLite storage with an append-only, hash-chained ledger.

Fidelity rules enforced here:
- `ledger` and `simulations` rows can never be updated or deleted (triggers).
- Every ledger entry is chained to the previous one by SHA-256, so any
  out-of-band edit of the file is detectable with `Ledger.verify()`.
- Money is stored as decimal strings, never floats.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..clock import iso, now_utc

GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS simulations (
    id                   TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    mode                 TEXT NOT NULL CHECK (mode IN ('forward', 'replay')),
    starting_capital_usd TEXT NOT NULL,
    config_json          TEXT NOT NULL,
    predecessor_id       TEXT REFERENCES simulations(id),
    created_at_utc       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    sim_id          TEXT NOT NULL REFERENCES simulations(id),
    strategy_id     TEXT,
    recorded_at_utc TEXT NOT NULL,
    kind            TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    prev_hash       TEXT NOT NULL,
    hash            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_sim ON ledger(sim_id, seq);
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS simulations_no_update BEFORE UPDATE ON simulations
BEGIN SELECT RAISE(ABORT, 'simulations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS simulations_no_delete BEFORE DELETE ON simulations
BEGIN SELECT RAISE(ABORT, 'simulations are immutable'); END;
"""


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def entry_hash(prev_hash: str, sim_id: str, strategy_id: str | None, recorded_at: str, kind: str, payload_json: str) -> str:
    material = "\x1f".join([prev_hash, sim_id, strategy_id or "", recorded_at, kind, payload_json])
    return hashlib.sha256(material.encode()).hexdigest()


class Database:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._lock = threading.RLock()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def close(self) -> None:
        self.conn.close()


@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    sim_id: str
    strategy_id: str | None
    recorded_at_utc: str
    kind: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _row_to_entry(r: sqlite3.Row) -> LedgerEntry:
    return LedgerEntry(r["seq"], r["sim_id"], r["strategy_id"], r["recorded_at_utc"], r["kind"],
                       json.loads(r["payload_json"]), r["prev_hash"], r["hash"])


class Ledger:
    def __init__(self, db: Database) -> None:
        self.db = db

    def append(self, sim_id: str, kind: str, payload: dict[str, Any], strategy_id: str | None = None,
               conn: sqlite3.Connection | None = None) -> LedgerEntry:
        """Append one entry. Pass `conn` to join an already-open transaction."""
        if conn is None:
            with self.db.tx() as c:
                return self.append(sim_id, kind, payload, strategy_id, c)
        payload_json = canonical_json(payload)
        recorded_at = iso(now_utc())
        last = conn.execute("SELECT hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
        prev = last["hash"] if last else GENESIS
        h = entry_hash(prev, sim_id, strategy_id, recorded_at, kind, payload_json)
        cur = conn.execute(
            "INSERT INTO ledger (sim_id, strategy_id, recorded_at_utc, kind, payload_json, prev_hash, hash)"
            " VALUES (?,?,?,?,?,?,?)",
            (sim_id, strategy_id, recorded_at, kind, payload_json, prev, h),
        )
        return LedgerEntry(cur.lastrowid, sim_id, strategy_id, recorded_at, kind, json.loads(payload_json), prev, h)

    def tail(self, limit: int = 50, sim_id: str | None = None) -> list[LedgerEntry]:
        if sim_id:
            rows = self.db.query("SELECT * FROM ledger WHERE sim_id=? ORDER BY seq DESC LIMIT ?", (sim_id, limit))
        else:
            rows = self.db.query("SELECT * FROM ledger ORDER BY seq DESC LIMIT ?", (limit,))
        return [_row_to_entry(r) for r in rows]

    def count(self) -> int:
        return self.db.query("SELECT COUNT(*) AS n FROM ledger")[0]["n"]

    def verify(self) -> dict[str, Any]:
        """Recompute the whole hash chain. Returns the first broken seq, if any."""
        prev, n = GENESIS, 0
        for r in self.db.query("SELECT * FROM ledger ORDER BY seq"):
            expected = entry_hash(prev, r["sim_id"], r["strategy_id"], r["recorded_at_utc"], r["kind"], r["payload_json"])
            if r["prev_hash"] != prev or r["hash"] != expected:
                return {"ok": False, "entries": n, "first_bad_seq": r["seq"]}
            prev, n = r["hash"], n + 1
        return {"ok": True, "entries": n, "first_bad_seq": None}
