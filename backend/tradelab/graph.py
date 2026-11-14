"""Supplier / customer / competitor graph, built once per company by the LLM from its latest 10-K and cached.

It links world news to tickers: a headline that names a company's supplier,
customer or competitor is attached to that company with the relation.
"""
from __future__ import annotations

import html
import json
import logging
import re
from typing import Any

from .clock import iso, now_utc
from .ingest.news import SecClient
from .llm import LLM, extract_json
from .storage.market_store import MarketStore

log = logging.getLogger(__name__)
MAX_CHARS = 120_000
GENERIC = {"company", "group", "holdings", "corporation", "international", "global", "systems", "technologies", "the"}
SYSTEM = """You extract business relationships from a company's annual report (10-K). Use only what the text says. \
Reply with JSON only: {"aliases": ["names and brands the company itself is known by"], \
"suppliers": [{"name": "...", "ticker": "US ticker or null"}], "customers": [...], "competitors": [...]}. \
List at most 12 per category, named companies only (no generic categories)."""


def clean_filing(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style|ix:header).*?</\1>", " ", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text)


def business_section(text: str) -> str:
    """From the second 'Item 1.' (the first is the table of contents) onward."""
    hits = [m.start() for m in re.finditer(r"Item\s+1\.", text, re.I)]
    return text[hits[1]:] if len(hits) > 1 else text


class CompanyGraph:
    def __init__(self, store: MarketStore) -> None:
        self.store = store
        self.graphs: dict[str, dict[str, Any]] = {}
        self._patterns: list[tuple[re.Pattern, str, str, str]] = []     # (pattern, symbol, relation, name)
        self._by_ticker: dict[str, list[tuple[str, str]]] = {}          # related ticker -> [(symbol, relation)]
        self.reload()

    def reload(self) -> None:
        self.graphs = {r["symbol"]: json.loads(r["graph"]) for r in self.store.query("SELECT * FROM company_graph")}
        self._patterns, self._by_ticker = [], {}
        for sym, g in self.graphs.items():
            for name in g.get("aliases", []):
                self._add(name, sym, "self")
            for rel in ("suppliers", "customers", "competitors"):
                for c in g.get(rel, []):
                    self._add(c.get("name", ""), sym, rel[:-1])
                    if c.get("ticker"):
                        self._by_ticker.setdefault(str(c["ticker"]).upper(), []).append((sym, rel[:-1]))

    def _add(self, name: str, sym: str, relation: str) -> None:
        name = name.strip()
        if len(name) >= 4 and name.lower() not in GENERIC:
            self._patterns.append((re.compile(r"\b" + re.escape(name) + r"\b", re.I), sym, relation, name))

    def links(self, item: dict[str, Any]) -> list[dict[str, str]]:
        text = f"{item.get('headline', '')} {item.get('summary', '')}"
        direct = set(item.get("symbols", []))
        out: dict[tuple[str, str], dict[str, str]] = {}
        for pattern, sym, relation, name in self._patterns:
            if pattern.search(text):
                if relation == "self":
                    if sym not in direct:
                        item["symbols"].append(sym)
                        direct.add(sym)
                else:
                    out.setdefault((sym, relation), {"symbol": sym, "relation": relation, "via": name})
        for t in list(direct):
            for sym, relation in self._by_ticker.get(t, []):
                out.setdefault((sym, relation), {"symbol": sym, "relation": relation, "via": t})
        return [l for l in out.values() if l["symbol"] not in direct][:12]

    async def build(self, symbol: str, cik: int, sec: SecClient, llm: LLM, model: str) -> dict[str, Any] | None:
        subs = (await sec.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")).json()
        recent = subs["filings"]["recent"]
        idx = next((i for i, f in enumerate(recent["form"]) if f == "10-K"), None)
        if idx is None:
            return None
        acc, doc = recent["accessionNumber"][idx], recent["primaryDocument"][idx]
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}"
        full = business_section(clean_filing((await sec.get(url)).text))
        used = full[:MAX_CHARS]
        r = await llm.chat(model, SYSTEM, f"Company: {subs.get('name')} ({symbol})\n\n{used}", max_tokens=2500,
                           purpose=f"graph:{symbol}")
        g = extract_json(r.text)
        g = {k: g.get(k, []) for k in ("aliases", "suppliers", "customers", "competitors")}
        source = {"url": url, "filed": recent["filingDate"][idx], "chars_used": len(used), "chars_total": len(full)}
        self.store.exec("INSERT OR REPLACE INTO company_graph VALUES (?,?,?,?,?,?)",
                        (symbol, iso(now_utc()), json.dumps(source), model, str(r.cost_usd), json.dumps(g)))
        self.store.exec("INSERT INTO platform_costs VALUES (?,?,?,?,?)",
                        (iso(now_utc()), "company graph", model, str(r.cost_usd), symbol))
        self.reload()
        log.info("graph_built", extra={"symbol": symbol, "cost_usd": str(r.cost_usd), **source})
        return g
