"""News and filings: Alpaca news stream, SEC EDGAR "latest filings" feed, GDELT world news.

Every item is stored with our receive time. Items that reach us long after
publication (for example the backlog on the first poll after a restart) are
marked late and do not wake the agents.

"Impact" is a transparent rule-based estimate (see `estimate_impact`), not a
model prediction.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from .. import __version__
from ..clock import iso, now_utc
from ..feeds import FeedStatus

log = logging.getLogger(__name__)
ATOM = "{http://www.w3.org/2005/Atom}"
LATE_AFTER = timedelta(minutes=15)
HIGH_8K_ITEMS = {"1.01", "1.02", "1.03", "2.01", "2.02", "2.05", "2.06", "3.01", "4.02", "5.01"}
HIGH_WORDS = re.compile(r"\b(earnings|guidance|forecast|acquir\w*|merger|takeover|buyout|fda|lawsuits?|recall\w*|bankrupt\w*|"
                        r"investigat\w*|downgrad\w*|upgrad\w*|resign\w*|layoffs?|tariffs?|sanctions?|profit warning|"
                        r"beats?|miss(es|ed)?|raises?|cuts?|halts?)\b", re.I)
GDELT_QUERY = ('(tariff OR sanctions OR "export controls" OR "supply chain" OR strike OR recall OR antitrust OR '
               '"interest rates" OR "trade war" OR embargo OR shortage) sourcelang:english')


def estimate_impact(kind: str, headline: str, symbols: list[str], links: list[dict], extra: dict[str, Any]) -> str:
    if kind == "filing":
        if extra.get("form") == "8-K":
            return "high" if set(extra.get("items", [])) & HIGH_8K_ITEMS else "medium"
        return "medium"
    if kind == "company":
        if not symbols:
            return "low"
        return "high" if len(symbols) <= 3 and HIGH_WORDS.search(headline) else "medium"
    return "medium" if (symbols or links) else "low"


def finish(item: dict[str, Any], graph: Any | None) -> dict[str, Any]:
    """Add graph links, lateness and impact to a raw item."""
    item.setdefault("symbols", [])
    item.setdefault("extra", {})
    item["links"] = graph.links(item) if graph else []
    item["impact"] = estimate_impact(item["kind"], item["headline"], item["symbols"], item["links"], item["extra"])
    pub = item.get("published_ts")
    if pub:
        try:
            late = datetime.fromisoformat(item["recv_ts"]) - datetime.fromisoformat(pub.replace("Z", "+00:00"))
            if late > LATE_AFTER:
                item["extra"]["late_minutes"] = int(late.total_seconds() / 60)
                item["impact"] = "low"
        except ValueError:
            pass
    return item


def from_alpaca(m: dict[str, Any], recv_ts: datetime) -> dict[str, Any] | None:
    if m.get("T") != "n" or not m.get("headline"):
        return None
    return {"id": f"alpaca:{m['id']}", "kind": "company", "source": f"Alpaca news ({m.get('source') or 'unknown'})",
            "recv_ts": iso(recv_ts), "published_ts": m.get("created_at"), "headline": m["headline"],
            "summary": m.get("summary") or "", "url": m.get("url") or "", "symbols": list(m.get("symbols") or [])}


def parse_edgar_atom(xml_text: str, recv_ts: datetime, cik_to_ticker: dict[int, str]) -> list[dict[str, Any]]:
    out, seen = [], set()
    for e in ET.fromstring(xml_text).iter(f"{ATOM}entry"):
        title = e.findtext(f"{ATOM}title") or ""
        m = re.match(r"(?P<form>\S+) - (?P<name>.*) \((?P<cik>\d+)\) \((?P<role>[^)]*)\)", title)
        acc = (e.findtext(f"{ATOM}id") or "").rsplit("=", 1)[-1]
        if not m or not acc or acc in seen:
            continue
        seen.add(acc)
        summary = re.sub(r"<[^>]+>", " ", e.findtext(f"{ATOM}summary") or "")
        items = re.findall(r"Item (\d+\.\d+)", summary)
        ticker = cik_to_ticker.get(int(m["cik"]))
        link = e.find(f"{ATOM}link")
        out.append({"id": f"edgar:{acc}", "kind": "filing", "source": "SEC EDGAR", "recv_ts": iso(recv_ts),
                    "published_ts": e.findtext(f"{ATOM}updated"),
                    "headline": f"{m['form']} filed by {m['name']}" + (f": items {', '.join(items)}" if items else ""),
                    "summary": " ".join(summary.split())[:500], "url": link.get("href") if link is not None else "",
                    "symbols": [ticker] if ticker else [],
                    "extra": {"form": m["form"], "items": items, "cik": int(m["cik"]), "accession": acc}})
    return out


def parse_gdelt(body: dict[str, Any], recv_ts: datetime) -> list[dict[str, Any]]:
    out = []
    for a in body.get("articles") or []:
        if not a.get("url") or not a.get("title"):
            continue
        seen = a.get("seendate") or ""
        try:
            pub = datetime.strptime(seen, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pub = None
        out.append({"id": "gdelt:" + hashlib.sha1(a["url"].encode()).hexdigest()[:20], "kind": "world",
                    "source": f"GDELT ({a.get('domain', '')})", "recv_ts": iso(recv_ts), "published_ts": pub,
                    "headline": a["title"], "summary": "", "url": a["url"], "symbols": [],
                    "extra": {"country": a.get("sourcecountry")}})
    return out


class SecClient:
    """SEC fair-access: declared User-Agent with a contact address, well under 10 requests/second."""

    def __init__(self, contact_email: str) -> None:
        self.http = httpx.AsyncClient(timeout=30, headers={"User-Agent": f"TradeLab/{__version__} {contact_email}",
                                                           "Accept-Encoding": "gzip, deflate"})
        self._lock = asyncio.Lock()

    async def get(self, url: str) -> httpx.Response:
        async with self._lock:
            await asyncio.sleep(0.25)
            r = await self.http.get(url)
        r.raise_for_status()
        return r

    async def tickers(self) -> dict[int, str]:
        data = (await self.get("https://www.sec.gov/files/company_tickers.json")).json()
        out: dict[int, str] = {}
        for row in data.values():                      # ordered by size: keep the first (primary) ticker per company
            out.setdefault(int(row["cik_str"]), row["ticker"])
        return out


async def edgar_loop(sec: SecClient, status: FeedStatus, on_item: Callable[[dict[str, Any]], bool], poll_s: float) -> None:
    ciks: dict[int, str] = {}
    fetched_ciks: datetime | None = None
    while True:
        try:
            if fetched_ciks is None or now_utc() - fetched_ciks > timedelta(hours=24):
                ciks, fetched_ciks = await sec.tickers(), now_utc()
            new = 0
            for form in ("8-K", "10-Q", "10-K"):
                r = await sec.get(f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}&count=40&output=atom")
                recv = now_utc()
                for item in parse_edgar_atom(r.text, recv, ciks):
                    if item["extra"]["form"].split("/")[0] in ("8-K", "10-Q", "10-K"):
                        new += on_item(item)
                status.last_received_at_utc = iso(recv)
            status.state, status.detail = "live", f"polled every {int(poll_s)}s"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            status.state, status.detail = "error", f"{type(e).__name__}: {e}"[:200]
            log.warning("edgar_poll_failed", extra={"error": str(e)[:200]})
        await asyncio.sleep(poll_s)


async def gdelt_loop(status: FeedStatus, on_item: Callable[[dict[str, Any]], bool], poll_s: float) -> None:
    async with httpx.AsyncClient(timeout=40) as http:
        while True:
            try:
                r = await http.get("https://api.gdeltproject.org/api/v2/doc/doc", params={
                    "query": GDELT_QUERY, "mode": "artlist", "format": "json", "maxrecords": 75,
                    "timespan": "30min", "sort": "datedesc"})
                recv = now_utc()
                if r.status_code != 200 or not r.text.lstrip().startswith("{"):
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:80]}")      # throttling replies are plain text
                for item in parse_gdelt(r.json(), recv):
                    on_item(item)
                status.state, status.last_received_at_utc = "live", iso(recv)
                status.detail = f"polled every {int(poll_s / 60)} min (GDELT itself updates every 15 min)"
                wait = poll_s
            except asyncio.CancelledError:
                raise
            except Exception as e:
                status.state = "error"
                status.detail = f"{type(e).__name__}: {e}"[:160] + " (GDELT throttles or refuses some networks; retrying every minute)"
                log.warning("gdelt_poll_failed", extra={"error": f"{type(e).__name__}: {e}"[:200]})
                wait = min(poll_s, 60.0)
            await asyncio.sleep(wait)
