import sqlite3
from decimal import Decimal

import pytest

from tradelab.simulations import SimulationError


def test_ledger_rows_cannot_be_updated_or_deleted(db, sims):
    sims.create("a", Decimal("5000"))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.conn.execute("UPDATE ledger SET payload_json='{}'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.conn.execute("DELETE FROM ledger")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.conn.execute("UPDATE simulations SET starting_capital_usd='999999'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.conn.execute("DELETE FROM simulations")


def test_hash_chain_detects_tampering_done_behind_the_triggers(db, ledger, sims):
    sim = sims.create("a", Decimal("5000"))
    for i in range(5):
        ledger.append(sim["id"], "note", {"i": i})
    assert ledger.verify() == {"ok": True, "entries": 6, "first_bad_seq": None}
    db.conn.execute("DROP TRIGGER ledger_no_update")
    db.conn.execute("UPDATE ledger SET payload_json='{\"i\":99}' WHERE seq=3")
    assert ledger.verify() == {"ok": False, "entries": 2, "first_bad_seq": 3}


def test_removing_an_entry_breaks_the_chain(db, ledger, sims):
    sim = sims.create("a", Decimal("5000"))
    for i in range(3):
        ledger.append(sim["id"], "note", {"i": i})
    db.conn.execute("DROP TRIGGER ledger_no_delete")
    db.conn.execute("DELETE FROM ledger WHERE seq=2")
    assert ledger.verify()["first_bad_seq"] == 3


def test_failed_transaction_leaves_no_partial_entries(db, ledger, sims):
    sim = sims.create("a", Decimal("5000"))
    with pytest.raises(RuntimeError):
        with db.tx() as conn:
            ledger.append(sim["id"], "note", {}, conn=conn)
            raise RuntimeError("boom")
    assert ledger.count() == 1 and ledger.verify()["ok"]


def test_money_is_stored_as_exact_decimal_text(sims):
    sim = sims.create("a", Decimal("5000.10"))
    assert sim["starting_capital_usd"] == "5000.10"


def test_new_simulation_keeps_the_old_one(sims, ledger):
    first = sims.create("first", Decimal("5000"))
    sims.transition(first["id"], "start")
    second = sims.create("second", Decimal("5000"))
    assert second["predecessor_id"] == first["id"]
    assert sims.get(first["id"])["state"] == "ended"
    assert [s["name"] for s in sims.list()] == ["second", "first"]
    assert sims.active()["id"] == second["id"]
    kinds = [e.kind for e in reversed(ledger.tail(sim_id=first["id"]))]
    assert kinds == ["simulation_created", "simulation_started", "simulation_ended"]


def test_lifecycle_transitions(sims):
    s = sims.create("a", Decimal("5000"))
    assert s["state"] == "created"
    with pytest.raises(SimulationError):
        sims.transition(s["id"], "pause")
    assert sims.transition(s["id"], "start")["state"] == "running"
    assert sims.transition(s["id"], "pause")["state"] == "paused"
    assert sims.transition(s["id"], "resume")["state"] == "running"
    assert sims.transition(s["id"], "end")["state"] == "ended"
    with pytest.raises(SimulationError):
        sims.transition(s["id"], "start")


def test_rejects_non_positive_capital(sims):
    with pytest.raises(SimulationError):
        sims.create("a", Decimal("0"))
