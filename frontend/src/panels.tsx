import { Fragment, useState } from "react";
import {
  Activity, Crosscheck, Detail, NewsItem, Overview, QuoteRow, Report, Simulation, Status, StrategyRow,
  ago, api, clock, dateTime, pct, seriesColor, signed, usd, usePoll,
} from "./api";

export const Dot = ({ tone }: { tone: "ok" | "warn" | "bad" | "off" }) => <span className={`dot ${tone}`} />;
export const Money = ({ v, digits = 2 }: { v: number; digits?: number }) => (
  <span className={v > 0 ? "up" : v < 0 ? "down" : ""}>{v > 0 ? "▲ " : v < 0 ? "▼ " : ""}{signed(v, digits)}</span>
);
const Hint = ({ text }: { text: string }) => <span className="hint" title={text} aria-label={text}>?</span>;
const WAKE: Record<string, string> = {
  timer: "routine check", scheduled: "its own scheduled check", news: "a news story", filing: "a company filing",
  price_move: "a sharp price move", price_alert: "a price alert it had set", fill: "one of its orders being executed",
  market_open: "the market opening",
};

// ------------------------------------------------------------------ activity
export function ActivityFeed({ refresh, strategy }: { refresh: number; strategy: string }) {
  const q = strategy ? `&strategy_id=${strategy}` : "";
  const [rows] = usePoll<Activity[]>(`/api/activity?limit=80${q}`, [refresh]);
  if (!rows) return <p className="muted">Loading…</p>;
  if (rows.length === 0) return <p className="muted">Nothing has happened yet. Trades and decisions appear here as they are made.</p>;
  return (
    <ul className="feed">
      {rows.filter((a) => !(a.kind === "decision" && a.orders)).map((a) => (
        <li key={a.seq}>
          <span className="swatch" style={{ background: a.strategy_id ? seriesColor(a.strategy_id) : "var(--muted)" }} />
          <div>
            <div className="feed-head"><b>{a.strategy ?? "You"}</b> <ActivityLine a={a} /> <span className="muted">· {dateTime(a.at)}</span></div>
            <ActivityBody a={a} />
          </div>
        </li>
      ))}
    </ul>
  );
}

function ActivityLine({ a }: { a: Activity }) {
  switch (a.kind) {
    case "fill":
      return <>{a.side === "buy" ? "bought" : "sold"} <b>{a.qty} {a.symbol}</b> at {usd(a.price!)}
        {a.realized_gain != null && <> · result <Money v={a.realized_gain - (a.tax ?? 0)} /> after tax</>}</>;
    case "decision":
      return a.orders ? <>decided to trade</> : <>looked and did nothing</>;
    case "order_rejected":
      return <>tried to {a.side} {a.qty} {a.symbol}: <span className="warn-text">refused</span></>;
    case "order_cancelled": return <>order cancelled</>;
    case "order_expired": return <>order expired unfilled</>;
    case "cash_deposit": return <>added {usd(a.amount!)} of cash</>;
    case "kill_switch": return <>{(a.detail as { on?: boolean })?.on ? "pressed STOP ALL" : "released STOP ALL"}</>;
    case "settings_changed": return <>changed the safety settings</>;
    case "alert_triggered": return <>price alert reached</>;
    case "dividend": return <>received a dividend of <span className="up">{usd(a.amount!)}</span> from <b>{a.symbol}</b> <span className="muted">(after {usd(a.tax!)} tax)</span></>;
    case "split": return <>stock split on <b>{a.symbol}</b></>;
    case "corporate_action_unhandled": return <><span className="warn-text">needs your attention:</span> <b>{a.symbol}</b></>;
    case "symbol_unblocked": return <>released <b>{a.symbol}</b></>;
    case "strategy_error": return <span className="warn-text">had an internal error (see logs)</span>;
    default: return <>{a.kind.replace(/_/g, " ")}</>;
  }
}

function ActivityBody({ a }: { a: Activity }) {
  const woke = (a.wakes ?? []).map((w) => WAKE[w] ?? w).filter((v, i, arr) => arr.indexOf(v) === i).join(", ");
  if (a.kind === "fill")
    return (
      <div className="feed-body">
        {a.rationale && <div><span className="muted">Why:</span> {a.rationale}</div>}
        {a.summary && a.summary !== a.rationale && a.confidence != null && <div><span className="muted">Its plan:</span> {a.summary}</div>}
        {a.by_risk_manager && <div className="muted">Placed automatically by the safety settings.</div>}
        {a.news?.map((n) => (
          <div key={n.id}><span className="muted">News it had seen:</span> {n.url ? <a href={n.url} target="_blank" rel="noreferrer">{n.headline}</a> : n.headline} <span className="muted">({n.source})</span></div>
        ))}
        <div className="muted small">
          Fee {usd(a.commission!)}{a.tax ? ` · tax ${usd(a.tax)}` : ""}{woke && ` · woken by ${woke}`} · filled on quote {a.quote!.bid} / {a.quote!.ask} from {a.quote!.source}, received {clock(a.quote!.recv_ts)}
        </div>
      </div>
    );
  if (a.kind === "decision")
    return (
      <div className="feed-body">
        {a.summary && <div>{a.summary}</div>}
        {a.error && <div className="warn-text">{a.error}</div>}
        <div className="muted small">
          {woke && `Woken by ${woke}`}{a.token_cost ? ` · thinking cost ${usd(a.token_cost, 4)}` : ""}{a.thinking_ms ? ` · took ${(a.thinking_ms / 1000).toFixed(1)}s` : ""}
        </div>
      </div>
    );
  if (a.kind === "order_rejected") return <div className="feed-body"><span className="muted">Reason:</span> {a.reason}</div>;
  if (a.kind === "split" || a.kind === "corporate_action_unhandled") return <div className="feed-body">{a.reason}</div>;
  if (a.kind === "order_cancelled" || a.kind === "order_expired")
    return <div className="feed-body muted">{a.reason}{a.unfilled_qty ? ` · ${a.unfilled_qty} shares not filled` : ""}</div>;
  return null;
}

// ------------------------------------------------------------------ holdings
export function Holdings({ strategies, refresh }: { strategies: StrategyRow[]; refresh: number }) {
  const [sid, setSid] = useState(strategies[0]?.id ?? "");
  const [d] = usePoll<Detail>(sid ? `/api/strategies/${sid}` : null, [refresh, sid], 5000);
  const row = strategies.find((s) => s.id === sid);
  if (!row) return <p className="muted">No strategies yet.</p>;
  return (
    <div>
      <div className="chips">
        {strategies.map((s) => (
          <button key={s.id} className={`chip ${s.id === sid ? "on" : ""}`} onClick={() => setSid(s.id)}>
            <span className="swatch" style={{ background: seriesColor(s.id) }} />{s.name}
          </button>
        ))}
      </div>
      <div className="row"><span>Cash <b>{usd(row.cash)}</b></span><span>Shares held <b>{row.positions}</b></span>
        <span>Orders waiting <b>{row.open_orders}</b></span>
        {d && d.tax_loss_credit > 0 && <span>Tax credit from past losses <b>{usd(d.tax_loss_credit)}</b></span>}</div>
      {!d ? <p className="muted">Loading…</p> : d.positions.length === 0 ? <p className="muted">Holding only cash right now.</p> : (
        <table>
          <thead><tr><th>Stock</th><th className="num">Shares</th><th className="num">Paid (avg)</th><th className="num">Sell price now</th>
            <th className="num">Value</th><th className="num">Gain / loss</th><th>Price from</th></tr></thead>
          <tbody>{d.positions.map((p) => (
            <tr key={p.symbol}>
              <td><b>{p.symbol}</b> <span className="muted">{p.name}</span>{p.halted && <span className="pill paused">halted</span>}</td>
              <td className="num">{p.qty}</td><td className="num">{usd(p.avg_cost)}</td>
              <td className="num">{p.bid != null ? usd(p.bid) : "—"}</td><td className="num">{p.value != null ? usd(p.value) : "—"}</td>
              <td className="num">{p.unrealized != null ? <Money v={p.unrealized} /> : "—"}</td>
              <td className="muted small"><Dot tone={p.price_is_fresh ? "ok" : "warn"} />{p.price_source ?? "no price"} · {ago(p.price_received_at_utc)}</td>
            </tr>))}</tbody>
        </table>
      )}
      {d && d.open_orders.length > 0 && (
        <table>
          <thead><tr><th>Waiting orders</th><th className="num">Shares</th><th>Type</th><th>Price</th></tr></thead>
          <tbody>{d.open_orders.map((o) => (
            <tr key={String(o.id)}><td>{String(o.side)} <b>{String(o.symbol)}</b></td>
              <td className="num">{Number(o.qty) - Number(o.filled_qty)} of {String(o.qty)}</td><td>{String(o.type)} · {String(o.tif)}</td>
              <td>{String(o.limit_price ?? o.stop_price ?? "market")}</td></tr>))}</tbody>
        </table>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ news
export function News({ refresh }: { refresh: number }) {
  const [rows] = usePoll<NewsItem[]>("/api/news?limit=80", [refresh], 20000);
  if (!rows) return <p className="muted">Loading…</p>;
  if (rows.length === 0) return <p className="muted">No news received yet. Stories and company filings appear here the moment they arrive.</p>;
  return (
    <ul className="feed">
      {rows.map((n) => (
        <li key={n.id}>
          <span className={`impact ${n.impact}`} title="Estimated impact, from simple rules (type of filing, key words, links to companies). Not a prediction.">{n.impact}</span>
          <div>
            <div className="feed-head">{n.url ? <a href={n.url} target="_blank" rel="noreferrer">{n.headline}</a> : n.headline}</div>
            <div className="tickers">
              {n.symbols.map((s) => <span key={s} className="ticker">{s}</span>)}
              {n.links.map((l) => <span key={l.symbol + l.relation} className="ticker linked" title={`${l.via} is a ${l.relation} of ${l.symbol}`}>{l.symbol} · {l.relation}</span>)}
              <span className="muted small"> {n.kind === "filing" ? "Filing" : n.kind === "world" ? "World news" : "Company news"} · {n.source} · received {ago(n.recv_ts)}
                {n.extra.late_minutes ? ` · arrived ${n.extra.late_minutes} min after publication` : ""}</span>
            </div>
          </div>
        </li>
      ))}
    </ul>
  );
}

// ------------------------------------------------------------------ scorecard
const METRICS: [string, string, (s: StrategyRow) => string][] = [
  ["Net result", "What you would have gained or lost after selling everything and paying every cost.", (s) => `${signed(s.net_result)} (${pct(s.return_pct)})`],
  ["Before costs", "The same result ignoring fees, tax and AI cost. Shown only for comparison.", (s) => `${signed(s.gross_result)} (${pct(s.gross_return_pct)})`],
  ["vs buy & hold", "Difference from simply buying the S&P 500 fund and waiting.", (s) => pct(s.vs_buy_hold_pct)],
  ["Worst drop", "The largest fall from a peak so far (max drawdown).", (s) => pct(s.max_drawdown_pct)],
  ["Risk-adjusted (Sharpe)", "Return per unit of risk. Needs at least 5 trading days.", (s) => (s.sharpe == null ? "needs more days" : s.sharpe.toFixed(2))],
  ["Winning trades", "Share of completed trades that made money before tax.", (s) => (s.win_rate_pct == null ? "no closed trades" : `${s.win_rate_pct.toFixed(0)}% of ${s.closed_trades}`)],
  ["Turnover", "Total value traded, as a multiple of the money put in.", (s) => `${s.turnover.toFixed(2)}×`],
  ["Cost per order", "All costs paid divided by the number of orders.", (s) => (s.avg_cost_per_order == null ? "—" : usd(s.avg_cost_per_order))],
  ["Broker fees", "Flat fee per executed order.", (s) => usd(s.costs.commissions)],
  ["Tax paid", "26% of each realised gain.", (s) => usd(s.costs.taxes)],
  ["AI thinking cost", "Price of the model calls, charged as they happen.", (s) => usd(s.costs.tokens, 4)],
  ["Dividends received", "Cash paid by the companies held, after 15% US and 26% Italian tax.", (s) => usd(s.dividends)],
];

export function Scorecard({ strategies, refresh }: { strategies: StrategyRow[]; refresh: number }) {
  const [rep] = usePoll<Report>("/api/report", [refresh], 60000);
  const [stored] = usePoll<{ id: string; created_ts: string; report: string }[]>("/api/reports", [], 300000);
  return (
    <div>
      {rep && (
        <div className="verdict">
          <h3>Do the AI agents beat the baselines, after all costs?</h3>
          <p className="big-answer">{rep.headline}</p>
          {rep.strategies.map((s) => (
            <p key={s.id}><span className="swatch" style={{ background: seriesColor(s.id) }} /><b>{s.name}:</b> {s.verdict}
              <span className="muted small"> ({s.days} trading days, {s.closed_trades} completed trades{s.p_value != null ? `, p = ${s.p_value.toFixed(3)}` : ""})</span></p>
          ))}
          <p className="muted small">A result is only called significant with at least {rep.min_days_for_significance} trading days and less than a 5% chance of being luck. A stored copy is made every Saturday.</p>
        </div>
      )}
      {stored && stored.length > 0 && (
        <details className="verdict">
          <summary>Weekly reports saved so far ({stored.length})</summary>
          {stored.map((r) => { const j = JSON.parse(r.report) as Report & { week: string }; return (
            <p key={r.id}><b>{j.week}</b> <span className="muted small">({dateTime(r.created_ts)})</span>: {j.headline}
              <span className="muted small"> {j.strategies.map((x) => `${x.name} ${pct(x.excess_return_pct)} vs buy & hold`).join(" · ")}</span></p>); })}
        </details>
      )}
      <table className="metrics">
        <thead><tr><th />{strategies.map((s) => <th key={s.id} className="num"><span className="swatch" style={{ background: seriesColor(s.id) }} />{s.name}</th>)}</tr></thead>
        <tbody>{METRICS.map(([label, hint, f]) => (
          <tr key={label}><td>{label} <Hint text={hint} /></td>{strategies.map((s) => <td key={s.id} className="num">{f(s)}</td>)}</tr>))}</tbody>
      </table>
    </div>
  );
}

// ------------------------------------------------------------------ system
const tone = (state: string) => (state === "live" ? "ok" : state === "connecting" ? "warn" : state === "not_started" ? "off" : "bad");

export function SystemPanel({ status, refresh }: { status: Status; refresh: number }) {
  const [quotes] = usePoll<QuoteRow[]>("/api/quotes", [], 3000);
  const [cross] = usePoll<Crosscheck>("/api/crosscheck", [refresh], 20000);
  const [chain] = usePoll<{ ok: boolean; entries: number }>("/api/ledger/verify", [refresh], 60000);
  return (
    <div className="stack">
      <div>
        <h3>Data feeds</h3>
        <table>
          <thead><tr><th>Feed</th><th>State</th><th>Last data received</th><th>Detail</th></tr></thead>
          <tbody>{status.feeds.map((f) => (
            <tr key={f.id}><td>{f.source}</td><td><Dot tone={tone(f.state)} />{f.state.replace("_", " ")}</td>
              <td>{ago(f.last_received_at_utc)}</td><td className="muted wrap">{f.detail}</td></tr>))}</tbody>
        </table>
        <div className="row small">
          <span>Market: <b>{status.market.state}</b> <span className="muted">({status.market.detail})</span></span>
          <span>Live tickers: {status.price_feed.streamed_symbols} of {status.price_feed.capacity} slots</span>
          <span><Dot tone={chain?.ok ? "ok" : chain ? "bad" : "off"} />Ledger: {chain ? (chain.ok ? `${chain.entries} entries, untampered` : "INTEGRITY CHECK FAILED") : "checking"}</span>
          <span>Company relationship maps: {status.company_graphs}</span>
          <span title={status.platform_llm_cost.note}>Platform AI cost: {usd(status.platform_llm_cost.usd, 4)}</span>
          {status.malformed_messages > 0 && <span className="warn-text">{status.malformed_messages} unreadable messages dropped</span>}
        </div>
      </div>
      <div>
        <h3>Live prices <span className="muted small">every price shows its source and when it was received</span></h3>
        {!quotes || quotes.every((q) => q.bid == null) ? <p className="muted">No prices received yet.</p> : (
          <table>
            <thead><tr><th>Ticker</th><th className="num">Sell at (bid)</th><th className="num">Buy at (ask)</th><th className="num">Shares offered</th><th>Source</th><th>Received</th><th>State</th></tr></thead>
            <tbody>{quotes.map((q) => (
              <tr key={q.symbol}><td><b>{q.symbol}</b> <span className="muted">{q.name}</span></td>
                <td className="num">{q.bid != null ? usd(q.bid) : "—"}</td><td className="num">{q.ask != null ? usd(q.ask) : "—"}</td>
                <td className="num">{q.bid_size ?? "—"} / {q.ask_size ?? "—"}</td><td className="muted">{q.source ?? "—"}</td>
                <td className="muted">{q.received_at_utc ? `${clock(q.received_at_utc)} (${ago(q.received_at_utc)})` : "never"}</td>
                <td>{q.halted ? <><Dot tone="bad" />halted</> : q.fresh ? <><Dot tone="ok" />live</> : <><Dot tone="warn" />{status.market.state === "open" ? "suspended: no fresh data" : "last known"}</>}</td></tr>))}</tbody>
          </table>
        )}
      </div>
      <div>
        <h3>Cross-check against Alpaca's paper account</h3>
        {!cross || cross.orders_mirrored === 0 ? <p className="muted">No orders mirrored yet. Every simulated order is also sent to the Alpaca paper account and the two executions are compared here.</p> : (
          <>
            <div className="row"><span>Orders mirrored <b>{cross.orders_mirrored}</b></span><span>Comparable <b>{cross.comparable}</b></span>
              <span>Average difference <b>{cross.avg_deviation_bps ?? "—"} bps</b></span><span>Worst <b>{cross.worst_deviation_bps ?? "—"} bps</b></span>
              <span>Not comparable <b>{cross.not_comparable}</b></span></div>
            <p className="muted small">{cross.note} 1 bps = 0.01%.</p>
            <table>
              <thead><tr><th>Order</th><th className="num">Simulation price</th><th className="num">Paper price</th><th className="num">Difference</th><th>Paper status</th></tr></thead>
              <tbody>{cross.recent.map((r) => (
                <tr key={r.order_id}><td>{r.side} {r.qty} <b>{r.symbol}</b></td><td className="num">{r.sim_avg_price ? usd(Number(r.sim_avg_price)) : "—"}</td>
                  <td className="num">{r.paper_avg_price ? usd(Number(r.paper_avg_price)) : "—"}</td>
                  <td className="num">{r.deviation_bps ? `${r.deviation_bps} bps` : "—"}</td><td className="muted wrap">{r.paper_status} {r.detail}</td></tr>))}</tbody>
            </table>
          </>
        )}
      </div>
      <div>
        <h3>Feed log</h3>
        {status.feed_events.length === 0 ? <p className="muted">No events.</p> : (
          <table><tbody>{status.feed_events.map((e, i) => (
            <tr key={i}><td className="muted">{dateTime(e.ts)}</td><td>{e.feed}</td><td>{e.event.replace(/_/g, " ")}</td><td className="muted wrap">{e.detail}</td></tr>))}</tbody></table>
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ settings
const LIMITS: [string, string, string, string][] = [
  ["max_position_pct", "Largest single holding", "% of the account", "Refuse a purchase that would put more than this share of the account in one stock."],
  ["max_daily_loss_pct", "Stop buying after a daily loss of", "%", "After losing this much in one day, no new purchases until tomorrow."],
  ["stop_loss_pct", "Automatic stop loss", "% below purchase price", "Sell a holding automatically once it falls this far below what was paid."],
  ["daily_token_budget_usd", "Daily AI spending ceiling", "USD per agent", "Protects your wallet: once an agent has spent this much on thinking in a day, it stops thinking until tomorrow."],
  ["extra_latency_ms", "Extra delay before orders reach the market", "milliseconds", "Makes the simulation harsher: orders wait this long before they can be filled."],
  ["slippage_bps", "Extra price penalty", "bps (0.01%)", "Makes the simulation harsher: every market order gets a slightly worse price."],
];

export function SettingsPanel({ overview, sims, reload }: { overview: Overview; sims: Simulation[]; reload: () => void }) {
  const sim = overview.simulation;
  const [name, setName] = useState("");
  const [capital, setCapital] = useState("5000");
  const [cash, setCash] = useState("1000");
  const [target, setTarget] = useState("");
  const [form, setForm] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const run = async (fn: () => Promise<unknown>, ok: string) => {
    try { await fn(); setMsg({ ok: true, text: ok }); reload(); } catch (e) { setMsg({ ok: false, text: (e as Error).message }); }
  };
  const value = (k: string) => form[k] ?? (overview.settings[k] == null || overview.settings[k] === 0 ? "" : String(overview.settings[k]));
  const saveLimits = () => {
    const body: Record<string, number | null> = {};
    for (const [k] of LIMITS) { const v = value(k).trim(); body[k] = v === "" ? (k === "extra_latency_ms" || k === "slippage_bps" ? 0 : null) : Number(v); }
    run(() => api("/api/settings", { method: "PUT", body: JSON.stringify(body) }), "Safety settings saved.").then(() => setForm({}));
  };
  const create = () => {
    if (sim && !confirm(`This ends "${sim.name}" and starts a new simulation from scratch. The old one is kept, never deleted.`)) return;
    run(() => api("/api/simulations", { method: "POST", body: JSON.stringify({ name: name || `Simulation ${sims.length + 1}`, starting_capital_usd: capital }) }),
      "New simulation created. Press Start when ready.").then(() => setName(""));
  };
  return (
    <div className="stack">
      {msg && <p className={msg.ok ? "ok-text" : "error"}>{msg.text}</p>}
      <div>
        <h3>Simulation</h3>
        {sim ? (
          <div className="row">
            <span><b>{sim.name}</b> · {usd(Number(sim.starting_capital_usd), 0)} per strategy · started {dateTime(sim.created_at_utc)}</span>
            <span className={`pill ${sim.state}`}>{sim.state}</span>
            {sim.state === "created" && <button className="primary" onClick={() => run(() => api(`/api/simulations/${sim.id}/start`, { method: "POST" }), "Simulation started.")}>Start</button>}
            {sim.state === "running" && <button onClick={() => run(() => api(`/api/simulations/${sim.id}/pause`, { method: "POST" }), "Paused.")}>Pause</button>}
            {sim.state === "paused" && <button className="primary" onClick={() => run(() => api(`/api/simulations/${sim.id}/resume`, { method: "POST" }), "Resumed.")}>Resume</button>}
          </div>
        ) : <p className="muted">No simulation yet.</p>}
        <div className="row">
          <input placeholder="Name of a new simulation" value={name} onChange={(e) => setName(e.target.value)} />
          <label>Starting money per strategy (USD) <input type="number" min="1" value={capital} onChange={(e) => setCapital(e.target.value)} /></label>
          <button onClick={create}>New simulation</button>
        </div>
        <p className="muted small">There is no reset. A new simulation closes the current one and keeps it in the list below, with its full history.</p>
        {sims.filter((s) => s.id !== sim?.id).length > 0 && (
          <table><thead><tr><th>Earlier simulations</th><th>Created</th><th className="num">Starting money</th><th>State</th></tr></thead>
            <tbody>{sims.filter((s) => s.id !== sim?.id).map((s) => (
              <tr key={s.id}><td>{s.name}</td><td>{dateTime(s.created_at_utc)}</td><td className="num">{usd(Number(s.starting_capital_usd), 0)}</td><td>{s.state}</td></tr>))}</tbody></table>
        )}
      </div>
      {sim && (
        <>
          <div>
            <h3>Add money</h3>
            <div className="row">
              <label>Amount (USD) <input type="number" min="1" value={cash} onChange={(e) => setCash(e.target.value)} /></label>
              <label>To <select value={target} onChange={(e) => setTarget(e.target.value)}>
                <option value="">every strategy (keeps the comparison fair)</option>
                {overview.strategies.map((s) => <option key={s.id} value={s.id}>{s.name} only</option>)}</select></label>
              <button onClick={() => run(() => api("/api/deposit", { method: "POST", body: JSON.stringify({ amount: cash, strategy_id: target || null }) }), "Money added.")}>Add money</button>
            </div>
            <p className="muted small">Only you can add money. The agents trade with what is there. Added money never counts as profit.</p>
          </div>
          <div>
            <h3>Safety settings <span className="muted small">all optional, all off unless you fill them in</span></h3>
            <div className="limits">
              {LIMITS.map(([k, label, unit, hint]) => (
                <label key={k} title={hint}>{label}
                  <span><input type="number" min="0" placeholder="off" value={value(k)} onChange={(e) => setForm({ ...form, [k]: e.target.value })} /> <span className="muted small">{unit}</span></span>
                  <span className="muted small">{hint}</span></label>
              ))}
            </div>
            <button className="primary" onClick={saveLimits}>Save safety settings</button>
            <p className="muted small">The agents are never limited in when or how often they trade. Orders a real broker would refuse (no cash, shares not owned, market closed) are always refused.</p>
          </div>
          <div>
            <h3>Costs applied</h3>
            <p className="muted">{overview.broker.name}: {usd(overview.broker.commission_per_order, 0)} per executed order, {overview.broker.tax_rate_pct}% tax on gains, 0.2% a year stamp duty, plus the real price of every AI call. <span className="small">Source: {overview.broker.source}</span></p>
          </div>
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ tools
interface ReplayResult { label: string; events_replayed: number; strategies: { id: string; name: string; net_result: string; gross_result: string; fills: number; costs: string }[] }
interface LedgerRow { seq: number; recorded_at_utc: string; kind: string; strategy_id: string | null; payload: Record<string, unknown>; hash: string }

export function Tools({ overview, refresh }: { overview: Overview; refresh: number }) {
  const today = new Date().toISOString().slice(0, 10);
  const [day, setDay] = useState(today);
  const [ticker, setTicker] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ReplayResult | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [ledger] = usePoll<LedgerRow[]>(`/api/ledger?limit=20${overview.simulation ? `&sim_id=${overview.simulation.id}` : ""}`, [refresh], 20000);
  const [open, setOpen] = useState<number | null>(null);
  const replay = async () => {
    setBusy(true); setMsg(null); setResult(null);
    try { setResult(await api<ReplayResult>("/api/replay", { method: "POST", body: JSON.stringify({ start: `${day}T00:00:00+00:00`, end: `${day}T23:59:59+00:00` }) })); }
    catch (e) { setMsg({ ok: false, text: (e as Error).message }); }
    setBusy(false);
  };
  const watch = async () => {
    try { await api(`/api/watch/${ticker.trim().toUpperCase()}`, { method: "POST" }); setMsg({ ok: true, text: `${ticker.toUpperCase()} now has a live-data slot.` }); setTicker(""); }
    catch (e) { setMsg({ ok: false, text: (e as Error).message }); }
  };
  return (
    <div className="stack">
      {msg && <p className={msg.ok ? "ok-text" : "error"}>{msg.text}</p>}
      <div>
        <h3>Follow another stock</h3>
        <div className="row">
          <input placeholder="Ticker, e.g. PLTR" value={ticker} onChange={(e) => setTicker(e.target.value)} onKeyDown={(e) => e.key === "Enter" && ticker && watch()} />
          <button onClick={watch} disabled={!ticker.trim()}>Start following</button>
        </div>
        <p className="muted small">The agents can also ask for any US stock themselves. The price feed follows a limited number of stocks at once (see System); stocks that are held always keep their place.</p>
      </div>
      <div>
        <h3>Replay a past day <span className="muted small">for checking the machinery only</span></h3>
        <div className="banner">A replay re-runs the simple strategies on the prices this installation recorded that day. <b>It says nothing about the AI agents</b>: a model may already know what happened afterwards, so they are never replayed.</div>
        <div className="row">
          <label>Day <input type="date" value={day} max={today} onChange={(e) => setDay(e.target.value)} /></label>
          <button onClick={replay} disabled={busy}>{busy ? "Replaying…" : "Run replay"}</button>
        </div>
        {result && (result.events_replayed === 0 ? <p className="muted">No prices were recorded on that day.</p> : (
          <table>
            <thead><tr><th>Strategy (replay, not a real result)</th><th className="num">Net result</th><th className="num">Before costs</th><th className="num">Costs</th><th className="num">Executions</th></tr></thead>
            <tbody>{result.strategies.map((s) => (
              <tr key={s.id}><td>{s.name}</td><td className="num"><Money v={Number(s.net_result)} /></td><td className="num">{signed(Number(s.gross_result))}</td>
                <td className="num">{usd(Number(s.costs))}</td><td className="num">{s.fills}</td></tr>))}</tbody>
          </table>))}
      </div>
      <div>
        <h3>Ledger <span className="muted small">the permanent record: nothing in it can be changed or deleted</span></h3>
        {!ledger || ledger.length === 0 ? <p className="muted">Empty.</p> : (
          <table>
            <thead><tr><th>#</th><th>When</th><th>Who</th><th>What</th><th>Fingerprint</th></tr></thead>
            <tbody>{ledger.map((e) => (
              <Fragment key={e.seq}>
                <tr className="clickable" onClick={() => setOpen(open === e.seq ? null : e.seq)}>
                  <td>{e.seq}</td><td className="muted">{dateTime(e.recorded_at_utc)}</td><td>{e.strategy_id ?? "you"}</td>
                  <td>{e.kind.replace(/_/g, " ")}</td><td className="muted mono">{e.hash.slice(0, 12)}</td></tr>
                {open === e.seq && <tr><td colSpan={5}><pre className="payload">{JSON.stringify(e.payload, null, 2)}</pre></td></tr>}
              </Fragment>))}</tbody>
          </table>
        )}
        <p className="muted small">Click a row to see everything that was recorded with it, including the prices and news seen at that moment.</p>
      </div>
    </div>
  );
}
