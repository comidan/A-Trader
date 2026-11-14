"""Cross-check: mirror every simulated order to the Alpaca PAPER account and compare the executions.

The paper account is a sanity check on prices only: Alpaca's paper engine fills
at the quote but ignores quoted size. One paper account is shared by all
strategies, so a mirrored sell can be rejected there when another strategy's
position differs; such orders are reported as not comparable.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any

from .clock import iso, now_utc
from .ingest.alpaca import AlpacaRest
from .storage.market_store import MarketStore

log = logging.getLogger(__name__)
FINAL = {"filled", "canceled", "expired", "rejected", "rejected_by_paper", "done_for_day"}


def deviation_bps(side: str, sim_price: Decimal, paper_price: Decimal) -> Decimal:
    """Positive = the simulation got a WORSE price than the paper account."""
    sign = 1 if side == "buy" else -1
    return (sign * (sim_price - paper_price) / paper_price * 10000).quantize(Decimal("0.01"))


class PaperMirror:
    def __init__(self, rest: AlpacaRest, store: MarketStore, sim_id: callable) -> None:
        self.rest, self.store, self.sim_id = rest, store, sim_id
        self.queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        self.sim: dict[str, list[tuple[int, Decimal]]] = {}

    def on_order(self, kind: str, p: dict[str, Any]) -> None:
        if kind in ("order_accepted", "fill", "order_cancelled", "order_expired"):
            self.queue.put_nowait((kind, p))

    async def run(self) -> None:
        poll = asyncio.create_task(self._poll())
        try:
            while True:
                kind, p = await self.queue.get()
                try:
                    await self._handle(kind, p)
                except Exception as e:
                    log.warning("paper_mirror_failed", extra={"error": str(e)[:200]})
        finally:
            poll.cancel()

    async def _handle(self, kind: str, p: dict[str, Any]) -> None:
        if kind == "order_accepted":
            o = p["order"]
            body = {"symbol": o["symbol"], "qty": str(o["qty"]), "side": o["side"], "type": o["type"],
                    "time_in_force": o["tif"], "client_order_id": o["id"]}
            if o["limit_price"]:
                body["limit_price"] = o["limit_price"]
            if o["stop_price"]:
                body["stop_price"] = o["stop_price"]
            res = await self.rest.paper_submit(body)
            self.store.exec("INSERT OR REPLACE INTO paper_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                o["id"], self.sim_id(), p["strategy_id"], o["symbol"], o["side"], o["qty"], iso(now_utc()), "open",
                res.get("status", "unknown"), 0, None, 0, None, None, res.get("detail", ""), iso(now_utc())))
        elif kind == "fill":
            fills = self.sim.setdefault(p["order_id"], [])
            fills.append((int(p["qty"]), Decimal(p["price"])))
            qty = sum(q for q, _ in fills)
            avg = sum(q * px for q, px in fills) / qty
            self.store.exec("UPDATE paper_orders SET sim_filled_qty=?, sim_avg_price=?, updated_ts=? WHERE order_id=?",
                            (qty, str(avg), iso(now_utc()), p["order_id"]))
        else:
            row = await self.rest.paper_order(p["order_id"])
            if row and row.get("status") not in FINAL:
                await self.rest.paper_cancel(row["id"])
            self.store.exec("UPDATE paper_orders SET status='closed' WHERE order_id=?", (p["order_id"],))

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(15)
            rows = self.store.query("SELECT * FROM paper_orders WHERE paper_status NOT IN (%s) OR "
                                    "(deviation_bps IS NULL AND sim_avg_price IS NOT NULL AND paper_avg_price IS NOT NULL)"
                                    % ",".join("'%s'" % s for s in FINAL))
            for row in rows[:50]:
                try:
                    o = await self.rest.paper_order(row["order_id"])
                    if not o:
                        continue
                    avg = o.get("filled_avg_price")
                    dev = None
                    if avg and row["sim_avg_price"]:
                        dev = str(deviation_bps(row["side"], Decimal(row["sim_avg_price"]), Decimal(str(avg))))
                    self.store.exec("UPDATE paper_orders SET paper_status=?, paper_filled_qty=?, paper_avg_price=?, "
                                    "deviation_bps=?, updated_ts=? WHERE order_id=?",
                                    (o.get("status"), int(float(o.get("filled_qty") or 0)), str(avg) if avg else None, dev,
                                     iso(now_utc()), row["order_id"]))
                except Exception as e:
                    log.warning("paper_poll_failed", extra={"error": str(e)[:200]})


def summary(store: MarketStore, sim_id: str | None) -> dict[str, Any]:
    rows = store.query("SELECT * FROM paper_orders WHERE sim_id=? ORDER BY submitted_ts DESC LIMIT 200", (sim_id or "",))
    devs = [Decimal(r["deviation_bps"]) for r in rows if r["deviation_bps"] is not None]
    return {
        "orders_mirrored": len(rows), "comparable": len(devs),
        "avg_deviation_bps": str((sum(devs) / len(devs)).quantize(Decimal("0.01"))) if devs else None,
        "worst_deviation_bps": str(max(devs)) if devs else None, "best_deviation_bps": str(min(devs)) if devs else None,
        "not_comparable": len([r for r in rows if r["paper_status"] in ("rejected", "rejected_by_paper")]),
        "note": "Positive = the simulation filled at a worse price than Alpaca paper. Paper ignores quoted size.",
        "recent": rows[:25],
    }
