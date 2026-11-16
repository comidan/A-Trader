import math
from datetime import datetime, timezone
from decimal import Decimal as D

from tradelab.crosscheck import deviation_bps
from tradelab.graph import CompanyGraph, business_section, clean_filing
from tradelab.ingest.news import estimate_impact, finish, from_alpaca, parse_edgar_atom, parse_gkg, parse_rss, pick_filing_document, filing_text
from tradelab.llm import cost_usd, extract_json, LLMError
from tradelab.metrics import binomial_p, max_drawdown, sharpe, t_test_p, weekly_report
from tradelab.storage.market_store import MarketStore
import json
import pytest

T = datetime(2026, 11, 16, 15, tzinfo=timezone.utc)
ATOM = """<?xml version="1.0" encoding="ISO-8859-1" ?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>8-K - Apple Inc. (0000320193) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/320193/x-index.htm"/>
<summary type="html">&lt;b&gt;Filed:&lt;/b&gt; 2026-11-16 &lt;b&gt;AccNo:&lt;/b&gt; 0000320193-26-000077
&lt;br&gt;Item 2.02: Results of Operations and Financial Condition&lt;br&gt;Item 9.01: Financial Statements</summary>
<updated>2026-11-16T09:58:39-05:00</updated><category term="8-K"/>
<id>urn:tag:sec.gov,2008:accession-number=0000320193-26-000077</id></entry>
<entry><title>8-K - Apple Inc. (0000320193) (Filer)</title><updated>2026-11-16T09:58:39-05:00</updated>
<id>urn:tag:sec.gov,2008:accession-number=0000320193-26-000077</id></entry>
<entry><title>10-Q - Unlisted Co (0009999999) (Filer)</title><updated>2026-11-14T09:00:00-05:00</updated>
<summary type="html">x</summary><id>urn:tag:sec.gov,2008:accession-number=0009999999-26-000001</id></entry></feed>"""


def test_edgar_feed_is_parsed_deduplicated_and_linked_to_tickers():
    items = parse_edgar_atom(ATOM, T, {320193: "AAPL"})
    assert [i["id"] for i in items] == ["edgar:0000320193-26-000077", "edgar:0009999999-26-000001"]
    a = finish(items[0], None)
    assert a["symbols"] == ["AAPL"] and a["extra"]["items"] == ["2.02", "9.01"] and a["impact"] == "high"
    assert a["recv_ts"] == T.isoformat(timespec="microseconds")
    late = finish(items[1], None)                    # published 2 days before we received it
    assert late["symbols"] == [] and late["impact"] == "low" and late["extra"]["late_minutes"] > 2000


def test_alpaca_news_and_gdelt_items():
    n = from_alpaca({"T": "n", "id": 7, "headline": "Tesla recalls 1M cars", "summary": "s", "created_at": "2026-11-16T14:59:00Z",
                     "url": "u", "symbols": ["TSLA"], "source": "benzinga"}, T)
    assert finish(n, None)["impact"] == "high" and n["id"] == "alpaca:7" and "benzinga" in n["source"]
    assert from_alpaca({"T": "n", "id": 8}, T) is None
    assert estimate_impact("company", "Market wrap", [], [], {}) == "low"


def test_company_graph_links_world_news_through_relationships():
    store = MarketStore(":memory:")
    graph = {"aliases": ["Apple", "iPhone"], "suppliers": [{"name": "TSMC", "ticker": "TSM"}],
             "customers": [], "competitors": [{"name": "Samsung", "ticker": None}]}
    store.exec("INSERT INTO company_graph VALUES ('AAPL','t','{}','m','0',?)", (json.dumps(graph),))
    g = CompanyGraph(store)
    world = finish({"id": "1", "kind": "world", "headline": "Earthquake halts TSMC production", "recv_ts": T.isoformat()}, g)
    assert world["links"] == [{"symbol": "AAPL", "relation": "supplier", "via": "TSMC"}] and world["impact"] == "medium"
    direct = finish({"id": "2", "kind": "world", "headline": "Apple faces new EU rules", "recv_ts": T.isoformat()}, g)
    assert direct["symbols"] == ["AAPL"] and direct["links"] == []
    by_ticker = finish({"id": "3", "kind": "company", "headline": "TSM raises prices", "symbols": ["TSM"], "recv_ts": T.isoformat()}, g)
    assert by_ticker["links"] == [{"symbol": "AAPL", "relation": "supplier", "via": "TSM"}]
    assert finish({"id": "4", "kind": "world", "headline": "Pineapple harvest", "recv_ts": T.isoformat()}, g)["links"] == []


def test_filing_text_cleaning():
    raw = "<html><style>x{}</style><p>Item 1. Business 3</p><p>Item&nbsp;1. Business</p><p>We buy chips from TSMC.</p></html>"
    assert business_section(clean_filing(raw)).startswith("Item 1. Business We buy chips")


def test_token_cost_and_json_extraction():
    prices = {"m": [4, 20]}
    assert cost_usd(prices, "m", 1_000_000, 100_000) == D("6")
    with pytest.raises(LLMError):
        cost_usd(prices, "unknown-model", 1, 1)
    assert extract_json('Sure!\n```json\n{"a": {"b": 1}}\n```') == {"a": {"b": 1}}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_paper_deviation_sign_positive_means_simulation_was_worse():
    assert deviation_bps("buy", D("100.10"), D("100")) == D("10.00")
    assert deviation_bps("sell", D("99.90"), D("100")) == D("10.00")
    assert deviation_bps("buy", D("99.95"), D("100")) == D("-5.00")


def test_statistics_match_known_values():
    t, p = t_test_p([0.1, 0.2, 0.3, 0.4, 0.5])               # t = 4.2426, p = 0.01324 (scipy.stats.ttest_1samp)
    assert math.isclose(t, 4.242640687, rel_tol=1e-9) and math.isclose(p, 0.013235, abs_tol=2e-5)
    assert t_test_p([0.1, 0.1, 0.1]) is None and t_test_p([1.0]) is None
    assert math.isclose(binomial_p(8, 10), 0.109375) and binomial_p(5, 10) == 1.0 and binomial_p(0, 0) is None
    assert math.isclose(max_drawdown([100, 120, 90, 130, 117]), -0.25)
    assert sharpe([0.01, 0.02]) is None


def _row(id_, daily, is_llm=False, ret=0.0):
    return {"id": id_, "name": id_, "is_llm": is_llm, "return_pct": ret, "daily": daily, "closed_trades": 4, "winning_trades": 3}


def test_weekly_report_refuses_to_call_a_winner_early():
    days = {f"2026-11-{d:02d}": 0.001 for d in range(1, 6)}
    rep = weekly_report([_row("buy_hold", days), _row("llm_light", {k: v + 0.01 for k, v in days.items()}, True, 5.0)])
    assert rep["headline"].startswith("Too early") and rep["strategies"][0]["significant"] is None


def test_weekly_report_significance_needs_consistent_outperformance():
    days = [f"d{i}" for i in range(30)]
    bench = {d: 0.001 for d in days}
    winner = {d: 0.001 + 0.004 + (0.001 if i % 2 else -0.001) for i, d in enumerate(days)}
    noisy = {d: 0.001 + (0.03 if i % 2 else -0.029) for i, d in enumerate(days)}
    rep = weekly_report([_row("buy_hold", bench), _row("llm_light", winner, True, 12.0), _row("random", noisy, False, 1.5)])
    by = {s["id"]: s for s in rep["strategies"]}
    assert by["llm_light"]["significant"] is True and by["random"]["significant"] is False
    assert rep["headline"].startswith("Yes")
    assert "could be luck" in by["random"]["verdict"]


def test_rss_and_atom_world_news_items():
    rss = """<rss><channel><item><title>Tariffs on chips announced</title><link>https://x/1</link>
      <pubDate>Mon, 16 Nov 2026 14:58:00 GMT</pubDate><description>&lt;p&gt;Details &lt;b&gt;here&lt;/b&gt;&lt;/p&gt;</description></item>
      <item><title>No link</title></item></channel></rss>"""
    items = parse_rss(rss, "BBC Business", T)
    assert len(items) == 1
    i = items[0]
    assert (i["kind"], i["source"], i["headline"], i["summary"]) == ("world", "BBC Business", "Tariffs on chips announced", "Details here")
    assert i["published_ts"] == "2026-11-16T14:58:00+00:00" and i["id"].startswith("rss:")
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Fed statement</title>
      <link href="https://fed/1"/><updated>2026-11-16T14:00:00Z</updated></entry></feed>"""
    a = parse_rss(atom, "Federal Reserve", T)[0]
    assert a["url"] == "https://fed/1" and a["published_ts"] == "2026-11-16T14:00:00+00:00"
    assert parse_rss(rss, "x", T)[0]["id"] == i["id"]              # same link, same id: no duplicates


def _gkg_row(url, title, orgs="", themes="", when="20261116153000"):
    c = [""] * 27
    c[1], c[3], c[4], c[7], c[13], c[26] = when, "example.com", url, themes, orgs, f"<PAGE_LINKS>x</PAGE_LINKS><PAGE_TITLE>{title}</PAGE_TITLE>"
    return "\t".join(c)


def test_gdelt_knowledge_graph_rows_become_world_news_linked_through_the_company_map():
    text = "\n".join([
        _gkg_row("https://example.com/a", "Quake halts chip production &amp; exports", "tsmc;taiwan government", "NATURAL_DISASTER;ECON_STOCKMARKET"),
        _gkg_row("https://example.com/b", "PS5 deals for Black Friday", "sony;gamers"),
        _gkg_row("ftp://bad", "ignored"), "too\tshort", _gkg_row("https://example.com/c", "")])
    items = parse_gkg(text, T)
    assert [i["url"] for i in items] == ["https://example.com/a", "https://example.com/b"]
    a = items[0]
    assert a["headline"] == "Quake halts chip production & exports" and a["source"] == "GDELT (example.com)"
    assert a["published_ts"] == "2026-11-16T15:30:00+00:00" and a["extra"]["macro"] == ["ECON_STOCKMARKET"]
    assert a["summary"] == "Organisations mentioned: tsmc, taiwan government"
    store = MarketStore(":memory:")
    store.exec("INSERT INTO company_graph VALUES ('AAPL','t','{}','m','0',?)",
               (json.dumps({"aliases": ["Apple"], "suppliers": [{"name": "TSMC", "ticker": "TSM"}], "customers": [], "competitors": []}),))
    linked = finish(a, CompanyGraph(store))
    assert linked["links"] == [{"symbol": "AAPL", "relation": "supplier", "via": "TSMC"}]     # matched on the organisation list
    assert finish(items[1], CompanyGraph(store))["links"] == []


def test_macro_keyword_filter_keeps_market_news_and_drops_noise():
    from tradelab.ingest.news import MACRO_WORDS
    keep = ["Oil prices fall as markets weigh supply", "Fed signals rate cut in December", "New tariffs on chips", "ECB holds interest rates"]
    drop = ["Best sparkling wine under 20", "Local pension rules explained", "Football club appoints manager"]
    assert all(MACRO_WORDS.search(h) for h in keep) and not any(MACRO_WORDS.search(h) for h in drop)


def test_filing_document_choice_and_text_and_article_content():
    idx = {"directory": {"item": [{"name": "0001-index.htm"}, {"name": "aapl-8k.htm"}, {"name": "aapl-ex991.htm"}, {"name": "R1.xml"}]}}
    assert pick_filing_document(idx) == "aapl-ex991.htm"                     # the press release when there is one
    assert pick_filing_document({"directory": {"item": [{"name": "main8k.htm"}, {"name": "x.jpg"}]}}) == "main8k.htm"
    assert pick_filing_document({}) is None
    t = filing_text("<html><style>p{}</style><p>Revenue &amp; profit rose.</p><script>x()</script></html>" + "<p>more</p>" * 5000, limit=40)
    assert t["text"] == "Revenue & profit rose. more more more mo" and t["text_chars_total"] > 20000
    n = from_alpaca({"T": "n", "id": 9, "headline": "h", "content": "<p>Shares are <b>down</b> 8%.</p>", "symbols": ["X"]}, T)
    assert n["extra"]["text"] == "Shares are down 8%."
