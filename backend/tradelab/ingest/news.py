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
import html
import io
import zipfile
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
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


MACRO_THEMES = ("ECON_INTEREST_RATE", "ECON_INFLATION", "ECON_CENTRALBANK", "ECON_TRADE_DISPUTE", "TARIFF", "SANCTION",
                "ECON_STOCKMARKET", "ECON_BANKRUPTCY", "STRIKE", "ECON_OILPRICE", "EMBARGO")
MACRO_WORDS = re.compile(r"\b(tariffs?|sanctions?|interest rates?|rate (cut|hike)s?|inflation|central bank|federal reserve|"
                         r"the fed|ecb|oil prices?|opec|recession|trade war|export controls?|embargo|strike|shutdown|"
                         r"default|stock market|wall street|treasury yields?|bond yields?|jobs report|gdp)\b", re.I)
TITLE_RE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.S)


def parse_gkg(text: str, recv_ts: datetime) -> list[dict[str, Any]]:
    """Rows of a GDELT Global Knowledge Graph 2.1 file (tab separated, 27 columns) as world-news items.

    Columns used: 1 batch time, 3 source, 4 url, 7 themes, 13 organisations, 26 extras (holds the page title).
    """
    out = []
    for line in text.split("\n"):
        c = line.split("\t")
        if len(c) < 27 or not c[4].startswith("http"):
            continue
        m = TITLE_RE.search(c[26])
        if not m or not m.group(1).strip():
            continue
        try:
            pub = datetime.strptime(c[1], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pub = None
        orgs = [o for o in dict.fromkeys(c[13].split(";")) if o][:12]
        themes = c[7].split(";")
        out.append({"id": "gdelt:" + hashlib.sha1(c[4].encode()).hexdigest()[:20], "kind": "world",
                    "source": f"GDELT ({c[3]})", "recv_ts": iso(recv_ts), "published_ts": pub,
                    "headline": html.unescape(m.group(1).strip())[:300],
                    "summary": ("Organisations mentioned: " + ", ".join(orgs)) if orgs else "", "url": c[4], "symbols": [],
                    "extra": {"macro": sorted({t for t in MACRO_THEMES if any(x.startswith(t) for x in themes)})}})
    return out


def parse_rss(xml_text: str, feed_name: str, recv_ts: datetime) -> list[dict[str, Any]]:
    """RSS 2.0 or Atom items as world-news items. Anything without a title and a link is dropped."""
    out = []
    root = ET.fromstring(xml_text)
    for e in list(root.iter("item")) + list(root.iter(f"{ATOM}entry")):
        title = (e.findtext("title") or e.findtext(f"{ATOM}title") or "").strip()
        link = (e.findtext("link") or "").strip()
        if not link:
            a = e.find(f"{ATOM}link")
            link = a.get("href", "") if a is not None else ""
        if not title or not link:
            continue
        pub = None
        raw = e.findtext("pubDate") or e.findtext(f"{ATOM}updated") or e.findtext(f"{ATOM}published")
        if raw:
            try:
                d = parsedate_to_datetime(raw) if "," in raw else datetime.fromisoformat(raw.replace("Z", "+00:00"))
                pub = (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError):
                pub = None
        summary = re.sub(r"<[^>]+>", " ", e.findtext("description") or e.findtext(f"{ATOM}summary") or "")
        out.append({"id": "rss:" + hashlib.sha1(link.encode()).hexdigest()[:20], "kind": "world", "source": feed_name,
                    "recv_ts": iso(recv_ts), "published_ts": pub, "headline": title,
                    "summary": " ".join(summary.split())[:400], "url": link, "symbols": []})
    return out


async def rss_loop(status: FeedStatus, on_item: Callable[[dict[str, Any]], bool], feeds: dict[str, str], poll_s: float) -> None:
    """Public news feeds (RSS): the main source of world news. One failing feed does not stop the others."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": f"TradeLab/{__version__} (news reader)"}) as http:
        while True:
            ok, failed = [], []
            for name, url in feeds.items():
                try:
                    r = await http.get(url)
                    r.raise_for_status()
                    recv = now_utc()
                    for item in parse_rss(r.text, name, recv):
                        on_item(item)
                    ok.append(name)
                    status.last_received_at_utc = iso(recv)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    failed.append(name)
                    log.warning("rss_poll_failed", extra={"feed_name": name, "error": f"{type(e).__name__}: {e}"[:200]})
            status.state = "live" if ok else "error"
            status.detail = f"{', '.join(ok) or 'no feed'} read every {int(poll_s / 60)} min" + (f"; not reachable: {', '.join(failed)}" if failed else "")
            await asyncio.sleep(poll_s)


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
    failures = 0
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
            failures = 0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            failures += 1                          # sec.gov times out now and then: one miss is not an outage
            if failures >= 3:
                status.state = "error"
            status.detail = f"last poll failed ({type(e).__name__}: {e}"[:150] + f"); {failures} in a row, retrying"
            log.warning("edgar_poll_failed", extra={"error": f"{type(e).__name__}: {e}"[:200]})
        await asyncio.sleep(poll_s)


GDELT_INDEX = "https://data.gdeltproject.org/gdeltv2/lastupdate.txt"


async def gdelt_loop(status: FeedStatus, on_item: Callable[[dict[str, Any]], dict[str, Any] | None],
                     poll_s: float, max_macro: int = 15) -> None:
    """GDELT world news from its public 15-minute knowledge-graph files (bulk files, no query API, no rate limit).

    Only stories that touch a followed company (directly or through the relationship map), plus a few clearly
    macro-economic ones per batch, are kept: a batch holds more than a thousand articles.
    """
    last_file = ""
    failures = 0
    async with httpx.AsyncClient(timeout=120, headers={"User-Agent": f"TradeLab/{__version__} (news reader)"}) as http:
        while True:
            try:
                index = (await http.get(GDELT_INDEX)).text
                url = next((l.split()[2] for l in index.splitlines() if l.strip().endswith(".gkg.csv.zip")), "")
                url = url.replace("http://", "https://", 1)
                if url and url != last_file:
                    r = await http.get(url)
                    r.raise_for_status()
                    recv = now_utc()
                    text = await asyncio.to_thread(
                        lambda: zipfile.ZipFile(io.BytesIO(r.content)).read(zipfile.ZipFile(io.BytesIO(r.content)).namelist()[0]).decode("utf-8", "replace"))
                    kept = macro = 0
                    for item in parse_gkg(text, recv):
                        linked = on_item(item, True)
                        if linked:
                            kept += 1
                        elif item["extra"]["macro"] and macro < max_macro and MACRO_WORDS.search(item["headline"]):
                            on_item(item, False)
                            macro += 1
                    last_file = url
                    status.last_received_at_utc = iso(recv)
                    status.detail = f"batch {url.rsplit('/', 1)[-1][:14]}: {kept} stories about followed companies, {macro} macro stories kept"
                failures = 0
                status.state = "live"
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                if failures >= 3:
                    status.state = "error"
                status.detail = f"last check failed ({type(e).__name__}: {e}"[:150] + "); retrying"
                log.warning("gdelt_poll_failed", extra={"error": f"{type(e).__name__}: {e}"[:200]})
            await asyncio.sleep(poll_s)
