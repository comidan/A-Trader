import { useEffect, useState } from "react";
import { Curves, Overview, Simulation, Status, StrategyRow, ago, api, pct, seriesColor, signed, usd, useLive, usePoll } from "./api";
import { EquityChart } from "./Chart";
import { MapView } from "./MapView";
import { ActivityFeed, Dot, Holdings, Money, News, Scorecard, SettingsPanel, SystemPanel, Tools } from "./panels";

const TABS = ["Activity", "Map", "Holdings", "News", "Scorecard", "System", "Tools", "Settings"] as const;
type Tab = (typeof TABS)[number];

function headline(o: Overview): { title: string; text: string; tone: "up" | "down" | "" } {
  const sim = o.simulation;
  if (!sim) return { title: "Welcome", text: "Create a simulation in Settings to start. Each strategy gets the same amount of pretend money and trades on real, live prices.", tone: "" };
  if (sim.state === "created") return { title: "Ready", text: "Press Start. Trading begins as soon as the US market is open.", tone: "" };
  const bh = o.strategies.find((s) => s.id === "buy_hold");
  const ai = o.strategies.filter((s) => s.is_llm).sort((a, b) => b.net_result - a.net_result)[0];
  if (!ai) return { title: "No AI agent is running", text: Object.values(o.not_running)[0] ?? "Only the simple strategies are trading.", tone: "" };
  if (!bh || o.strategies.every((s) => s.fills === 0))
    return { title: "Waiting for the first trades", text: o.market.state === "open" ? "The market is open; the strategies are deciding." : "The US market is closed. Nothing trades until it opens.", tone: "" };
  const diff = ai.net_result - bh.net_result;
  return diff > 0
    ? { title: "The AI is ahead", text: `${ai.name} is ${usd(diff)} ahead of simply buying the S&P 500, after all costs. Early leads are often luck: see the Scorecard.`, tone: "up" }
    : { title: "The AI is not ahead", text: `${ai.name} is ${usd(-diff)} behind simply buying the S&P 500, after all costs.`, tone: "down" };
}

function Card({ s, rank }: { s: StrategyRow; rank: number }) {
  const totalCosts = s.costs.commissions + s.costs.taxes + s.costs.tokens + s.costs.stamp_duty;
  return (
    <div className="card strat" style={{ borderTopColor: seriesColor(s.id) }} title={s.description}>
      <div className="strat-head">
        <span className="rank">{rank}</span><b>{s.name}</b>
        <span className={`tag ${s.is_llm ? "ai" : ""}`}>{s.is_llm ? "AI" : "simple rule"}</span>
        {s.thinking && <span className="thinking">thinking…</span>}
      </div>
      <div className="net"><Money v={s.net_result} /></div>
      <div className="sub">{pct(s.return_pct)} after all costs · now worth <b>{usd(s.net_liquidation)}</b></div>
      <div className="strat-foot">
        <div className="muted small">
          Before costs {signed(s.gross_result)} · costs so far {usd(totalCosts)}
          {s.id !== "buy_hold" && s.vs_buy_hold_pct != null && <><br />vs buy & hold {pct(s.vs_buy_hold_pct)}</>}
          {(s.suspended_positions.length > 0 || s.prices_missing_for.length > 0) && <><br /><span className="warn-text">No fresh price for {[...s.suspended_positions, ...s.prices_missing_for].join(", ")}</span></>}
        </div>
      </div>
    </div>
  );
}

export function App() {
  const { status, overview, connected, receivedAt, eventCount } = useLive();
  const [tab, setTab] = useState<Tab>("Activity");
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState("");
  const [now, setNow] = useState(Date.now());
  const [curves] = usePoll<Curves>("/api/equity", [eventCount], 30000);
  const [sims, reloadSims] = usePoll<Simulation[]>("/api/simulations", [eventCount], 60000);
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t); }, []);

  const age = receivedAt ? Math.round((now - receivedAt) / 1000) : null;
  const stale = !connected || age == null || age > 10;
  if (!status || !overview) return <main><section className="card"><p className="muted">Connecting to TradeLab…</p></section></main>;

  const sim = overview.simulation;
  const baseline = sim ? Number(sim.starting_capital_usd) : 0;
  const names = Object.fromEntries(overview.strategies.map((s) => [s.id, s.name]));
  const h = headline(overview);
  const quotes = status.feeds.find((f) => f.id === "alpaca_quotes");
  const dataTone = stale ? "bad" : quotes?.state === "live" ? "ok" : quotes?.state === "connecting" ? "warn" : "bad";
  const toggle = (id: string) => setHidden((p) => { const n = new Set(p); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const stopAll = () => {
    const on = !overview.kill_switch;
    if (on && !confirm("Stop all trading now? Open orders are cancelled and no new orders are accepted until you release it.")) return;
    api(`/api/kill-switch/${on ? "on" : "off"}`, { method: "POST" }).catch((e) => alert(e.message));
  };

  return (
    <main>
      <header>
        <h1>TradeLab</h1>
        <span className="pill sim" title="Real market data, pretend money. This software cannot place real orders.">Simulation · pretend money</span>
        <span className="right chipset">
          <span className="statuschip" title={overview.market.detail}><Dot tone={overview.market.state === "open" ? "ok" : overview.market.state === "closed" ? "off" : "warn"} />
            Market {overview.market.state}{overview.market.state === "closed" && overview.market.opens_at_utc ? ` · opens ${new Date(overview.market.opens_at_utc).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}` : ""}</span>
          <span className="statuschip" title={quotes?.detail}><Dot tone={dataTone} />
            {stale ? "Dashboard disconnected" : quotes?.state !== "live" ? `Prices ${quotes?.state.replace("_", " ")}`
              : quotes.last_received_at_utc ? `Prices live · ${ago(quotes.last_received_at_utc, now)}` : "Price feed connected · no prices until the market opens"}</span>
          {sim && <button className={overview.kill_switch ? "danger on" : "danger"} onClick={stopAll}>{overview.kill_switch ? "Trading stopped · release" : "Stop all trading"}</button>}
        </span>
      </header>

      {status.price_feed.warning && (
        <div className="banner"><b>{status.price_feed.feed.toUpperCase()} price feed, not the full market.</b> {status.price_feed.warning}</div>
      )}
      {overview.kill_switch && <div className="banner bad">Trading is stopped. No orders are accepted until you release the stop.</div>}
      {quotes && quotes.state !== "live" && sim && <div className="banner bad"><b>Live prices are not flowing ({quotes.state.replace("_", " ")}).</b> {quotes.detail} Nothing can trade without live prices.</div>}

      {Object.entries({ ...overview.frozen, ...overview.blocked }).map(([sym, why]) => (
        <div key={sym} className="banner bad"><b>{sym} needs your attention.</b> {why}. {sym in overview.frozen ? "Nothing trades in it until this is explained." : "New purchases are refused; selling is allowed."}{" "}
          <button onClick={() => confirm(`Trust ${sym} again and allow trading in it?`) && api(`/api/release/${sym}`, { method: "POST" }).catch((e) => alert(e.message))}>I checked, release it</button></div>
      ))}

      <section className={`card hero ${h.tone}`}>
        <div>
          <div className="eyebrow">Is the AI beating the market, after all costs?</div>
          <div className="hero-title">{h.title}</div>
          <div className="hero-text">{h.text}</div>
        </div>
        {sim && <div className="hero-side muted small">
          <div><b>{sim.name}</b> · <span className={`pill ${sim.state}`}>{sim.state}</span></div>
          <div>{usd(baseline, 0)} of pretend money per strategy</div>
          <div>Results shown after fees, tax and AI cost</div>
        </div>}
      </section>

      {overview.strategies.length > 0 && (
        <>
          <section className="grid">
            {overview.strategies.map((s, i) => <Card key={s.id} s={s} rank={i + 1} />)}
          </section>
          {Object.keys(overview.not_running).length > 0 && (
            <p className="muted small">Not running: {Object.entries(overview.not_running).map(([k, v]) => `${k} (${v})`).join(" · ")}</p>
          )}
          <section className="card">
            <h2>What {usd(baseline, 0)} has become <span className="muted">after all costs, if everything were sold now</span></h2>
            <EquityChart curves={curves ?? {}} names={names} baseline={baseline} hidden={hidden} onToggle={toggle} />
          </section>
        </>
      )}

      <section className="card">
        <nav className="tabs" role="tablist">
          {TABS.map((t) => <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>{t}</button>)}
          {tab === "Activity" && overview.strategies.length > 0 && (
            <select className="right" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter by strategy">
              <option value="">Everyone</option>{overview.strategies.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          )}
        </nav>
        {tab === "Activity" && <ActivityFeed refresh={eventCount} strategy={filter} />}
        {tab === "Map" && <MapView refresh={eventCount} />}
        {tab === "Tools" && <Tools overview={overview} refresh={eventCount} />}
        {tab === "Holdings" && <Holdings strategies={overview.strategies} refresh={eventCount} />}
        {tab === "News" && <News refresh={eventCount} />}
        {tab === "Scorecard" && <Scorecard strategies={overview.strategies} refresh={eventCount} />}
        {tab === "System" && <SystemPanel status={status as Status} refresh={eventCount} />}
        {tab === "Settings" && <SettingsPanel overview={overview} sims={sims ?? []} reload={reloadSims} />}
      </section>
      <footer className="muted small">TradeLab v{status.version} · simulation only, no real orders · server time {new Date(status.server_time_utc).toLocaleString()}</footer>
    </main>
  );
}
