"""Alpaca market data (websocket + REST) and PAPER trading client.

Message formats follow docs.alpaca.markets "Real-time Stock Data" and
"WebSocket Stream" (read 2026-11-14). Quote sizes are treated as SHARES: Alpaca
switched from round lots to shares on 2025-11-03; if a feed still reported
round lots we would under-estimate the size, which is the conservative error.

There is no live-trading client here: the trading host is hard-coded to the
paper endpoint and cannot be configured.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Awaitable, Callable

import httpx
import websockets

from ..clock import iso, now_utc
from ..feeds import FeedStatus
from ..models import Quote

log = logging.getLogger(__name__)

PAPER_HOST = "https://paper-api.alpaca.markets"      # paper only, by construction
DATA_HOST = "https://data.alpaca.markets"
STREAM_HOST = "wss://stream.data.alpaca.markets"

_last_seq = 0


def next_seq(recv_ts: datetime) -> int:
    """Strictly increasing id derived from receive time (survives restarts)."""
    global _last_seq
    _last_seq = max(_last_seq + 1, int(recv_ts.timestamp() * 1_000_000))
    return _last_seq


def parse_quote(m: dict[str, Any], recv_ts: datetime, source: str) -> Quote | None:
    try:
        return Quote(m["S"], Decimal(str(m["bp"])), int(m["bs"]), Decimal(str(m["ap"])), int(m["as"]),
                     recv_ts, next_seq(recv_ts), source, m.get("t"))
    except (KeyError, ValueError, ArithmeticError):
        return None


def parse_bar(m: dict[str, Any], recv_ts: datetime) -> dict[str, Any] | None:
    try:
        return {"t": m["t"], "recv_ts": recv_ts, "o": Decimal(str(m["o"])), "h": Decimal(str(m["h"])),
                "l": Decimal(str(m["l"])), "c": Decimal(str(m["c"])), "v": int(m["v"])}
    except (KeyError, ValueError, ArithmeticError):
        return None


class AlpacaRest:
    def __init__(self, key: str, secret: str, feed: str) -> None:
        self.feed = feed
        self.http = httpx.AsyncClient(timeout=20, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    async def _get(self, url: str, **params: Any) -> Any:
        r = await self.http.get(url, params=params)
        r.raise_for_status()
        return r.json()

    async def calendar(self, start: str, end: str) -> list[dict[str, str]]:
        return await self._get(f"{PAPER_HOST}/v2/calendar", start=start, end=end)

    async def assets(self) -> list[dict[str, Any]]:
        return await self._get(f"{PAPER_HOST}/v2/assets", status="active", asset_class="us_equity")

    async def corporate_actions(self, symbols: list[str], start: str, end: str) -> list[dict[str, Any]]:
        """Flat list of {type, id, symbol, ex_date, ...} from /v1/corporate-actions (docs read 2026-11-14)."""
        out: list[dict[str, Any]] = []
        for i in range(0, len(symbols), 50):
            token = None
            while True:
                params = {"symbols": ",".join(symbols[i:i + 50]), "start": start, "end": end, "limit": 1000}
                if token:
                    params["page_token"] = token
                body = await self._get(f"{DATA_HOST}/v1/corporate-actions", **params)
                for plural, rows in (body.get("corporate_actions") or {}).items():
                    kind = plural[:-1] if plural.endswith("s") else plural
                    out += [{**r, "type": kind} for r in rows or []]
                token = body.get("next_page_token")
                if not token:
                    break
        return out

    async def daily_bars(self, symbols: list[str], start: str, end: str) -> dict[str, list[dict[str, Any]]]:
        """Split- and dividend-adjusted daily bars. `end` must be at least 15 minutes old on the free plan."""
        out: dict[str, list[dict[str, Any]]] = {}
        for i in range(0, len(symbols), 30):
            token = None
            while True:
                params = {"symbols": ",".join(symbols[i:i + 30]), "timeframe": "1Day", "start": start, "end": end,
                          "adjustment": "all", "limit": 10000, "feed": "sip"}
                if token:
                    params["page_token"] = token
                body = await self._get(f"{DATA_HOST}/v2/stocks/bars", **params)
                for sym, bars in (body.get("bars") or {}).items():
                    out.setdefault(sym, []).extend(bars)
                token = body.get("next_page_token")
                if not token:
                    break
        return out

    # ---- paper orders (cross-check only)
    async def paper_submit(self, order: dict[str, Any]) -> dict[str, Any]:
        r = await self.http.post(f"{PAPER_HOST}/v2/orders", json=order)
        if r.status_code >= 400:
            return {"status": "rejected_by_paper", "detail": r.text[:300]}
        return r.json()

    async def paper_order(self, client_order_id: str) -> dict[str, Any] | None:
        r = await self.http.get(f"{PAPER_HOST}/v2/orders:by_client_order_id", params={"client_order_id": client_order_id})
        return r.json() if r.status_code == 200 else None

    async def paper_cancel(self, alpaca_id: str) -> None:
        await self.http.delete(f"{PAPER_HOST}/v2/orders/{alpaca_id}")

    async def close(self) -> None:
        await self.http.aclose()


Handler = Callable[[dict[str, Any], datetime], None]


class AlpacaStream:
    """One websocket with automatic re-authentication, re-subscription and backoff."""

    def __init__(self, url: str, key: str, secret: str, status: FeedStatus, on_message: Handler,
                 channels: Callable[[], dict[str, list[str]]], on_state: Callable[[str, str], None] | None = None) -> None:
        self.url, self.key, self.secret = url, key, secret
        self.status, self.on_message, self.channels, self.on_state = status, on_message, channels, on_state
        self.ws: Any = None
        self.last_message_at: datetime | None = None
        self._stop = False

    def _set(self, state: str, detail: str = "") -> None:
        if (self.status.state, self.status.detail) != (state, detail):
            self.status.state, self.status.detail = state, detail
            log.info("feed_state", extra={"feed": self.status.id, "state": state, "detail": detail})
            if self.on_state:
                self.on_state(state, detail)

    async def send(self, msg: dict[str, Any]) -> None:
        if self.ws is not None:
            try:
                await self.ws.send(json.dumps(msg))
            except Exception:           # the reconnect loop re-subscribes everything
                pass

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop:
            try:
                self._set("connecting")
                async with websockets.connect(self.url, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
                    await ws.send(json.dumps({"action": "auth", "key": self.key, "secret": self.secret}))
                    self.ws = ws
                    async for raw in ws:
                        recv_ts = now_utc()
                        self.last_message_at = recv_ts
                        for m in json.loads(raw, parse_float=Decimal):
                            t = m.get("T")
                            if t == "success":                      # "connected", then "authenticated": not data
                                if m.get("msg") == "authenticated":
                                    await ws.send(json.dumps({"action": "subscribe", **self.channels()}))
                                    backoff = 1.0
                            elif t == "subscription":
                                self._set("live", "subscribed")
                            elif t == "error":
                                # 402 auth failed, 406 connection limit, 405 symbol limit, 409 insufficient subscription
                                self._set("error", f"{m.get('code')}: {m.get('msg')}")
                                log.error("feed_error", extra={"feed": self.status.id, "code": m.get("code"), "msg": m.get("msg")})
                                if m.get("code") in (402, 406, 409):
                                    backoff = max(backoff, 30.0)
                            else:
                                self.status.last_received_at_utc = iso(recv_ts)
                                self.on_message(m, recv_ts)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self.status.state != "error":
                    self._set("error", f"disconnected: {type(e).__name__}: {e}"[:200])
            finally:
                self.ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    def stop(self) -> None:
        self._stop = True
