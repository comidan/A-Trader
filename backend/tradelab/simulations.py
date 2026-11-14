"""Simulation lifecycle. State is derived from ledger events, never stored mutably.

A "reset" does not exist: creating a new simulation ends the current one and
keeps it, linked through `predecessor_id`.
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

from .clock import iso, now_utc
from .storage.db import Database, Ledger

LIFECYCLE = {
    "simulation_created": "created",
    "simulation_started": "running",
    "simulation_paused": "paused",
    "simulation_resumed": "running",
    "simulation_ended": "ended",
}
# action -> (states it is allowed from, ledger event kind)
ACTIONS = {
    "start": ({"created"}, "simulation_started"),
    "pause": ({"running"}, "simulation_paused"),
    "resume": ({"paused"}, "simulation_resumed"),
    "end": ({"created", "running", "paused"}, "simulation_ended"),
}


class SimulationError(Exception):
    pass


class Simulations:
    def __init__(self, db: Database, ledger: Ledger) -> None:
        self.db, self.ledger = db, ledger

    def _state(self, sim_id: str, conn=None) -> str:
        sql = ("SELECT kind FROM ledger WHERE sim_id=? AND kind LIKE 'simulation_%' ORDER BY seq DESC LIMIT 1")
        rows = conn.execute(sql, (sim_id,)).fetchall() if conn else self.db.query(sql, (sim_id,))
        if not rows:
            raise SimulationError(f"unknown simulation {sim_id}")
        return LIFECYCLE[rows[0]["kind"]]

    def _to_dict(self, r) -> dict[str, Any]:
        d = dict(r)
        d["config"] = json.loads(d.pop("config_json"))
        d["state"] = self._state(r["id"])
        return d

    def list(self) -> list[dict[str, Any]]:
        return [self._to_dict(r) for r in self.db.query("SELECT * FROM simulations ORDER BY created_at_utc DESC")]

    def get(self, sim_id: str) -> dict[str, Any]:
        rows = self.db.query("SELECT * FROM simulations WHERE id=?", (sim_id,))
        if not rows:
            raise SimulationError(f"unknown simulation {sim_id}")
        return self._to_dict(rows[0])

    def active(self) -> dict[str, Any] | None:
        return next((s for s in self.list() if s["state"] != "ended"), None)

    def create(self, name: str, starting_capital_usd: Decimal, mode: str = "forward",
               config: dict[str, Any] | None = None) -> dict[str, Any]:
        if starting_capital_usd <= 0:
            raise SimulationError("starting capital must be positive")
        if mode not in ("forward", "replay"):
            raise SimulationError("mode must be 'forward' or 'replay'")
        sim_id = uuid.uuid4().hex
        created = iso(now_utc())
        config = config or {}
        with self.db.tx() as conn:
            prev = self.active()
            if prev:
                self.ledger.append(prev["id"], "simulation_ended", {"reason": "superseded", "successor_id": sim_id}, conn=conn)
            conn.execute(
                "INSERT INTO simulations (id, name, mode, starting_capital_usd, config_json, predecessor_id, created_at_utc)"
                " VALUES (?,?,?,?,?,?,?)",
                (sim_id, name, mode, str(starting_capital_usd), json.dumps(config, sort_keys=True),
                 prev["id"] if prev else None, created),
            )
            self.ledger.append(sim_id, "simulation_created", {
                "name": name, "mode": mode, "starting_capital_usd": str(starting_capital_usd),
                "config": config, "predecessor_id": prev["id"] if prev else None,
            }, conn=conn)
        return self.get(sim_id)

    def transition(self, sim_id: str, action: str) -> dict[str, Any]:
        if action not in ACTIONS:
            raise SimulationError(f"unknown action {action}")
        allowed, kind = ACTIONS[action]
        with self.db.tx() as conn:
            state = self._state(sim_id, conn)
            if state not in allowed:
                raise SimulationError(f"cannot {action} a simulation that is {state}")
            self.ledger.append(sim_id, kind, {"reason": "manual"}, conn=conn)
        return self.get(sim_id)
