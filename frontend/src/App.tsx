import { useCallback, useEffect, useState } from "react";
import { api, LedgerEntry, Simulation, Status, useLiveStatus } from "./api";

const time = (iso: string | null) => (iso ? new Date(iso).toLocaleTimeString() : "never");
const usd = (v: string) => Number(v).toLocaleString("en-US", { style: "currency", currency: "USD" });

function Dot({ tone }: { tone: "ok" | "warn" | "bad" | "off" }) {
  return <span className={`dot ${tone}`} />;
}

const credentialLabel: Record<string, string> = { alpaca: "Alpaca keys", llm: "LLM key", sec_user_agent: "SEC contact email" };
const feedTone = { live: "ok", connecting: "warn", stalled: "bad", error: "bad", not_started: "off" } as const;

function SystemStatus({ status }: { status: Status }) {
  return (
    <section className="card">
      <h2>System status</h2>
      <table>
        <thead>
          <tr><th>Feed</th><th>Source</th><th>State</th><th>Last data received</th><th>Detail</th></tr>
        </thead>
        <tbody>
          {status.feeds.map((f) => (
            <tr key={f.id}>
              <td>{f.id}</td>
              <td>{f.source}</td>
              <td><Dot tone={feedTone[f.state]} />{f.state.replace("_", " ")}</td>
              <td>{time(f.last_received_at_utc)}</td>
              <td className="muted">{f.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row">
        <span>Market: <b>{status.market.state}</b> <span className="muted">({status.market.detail})</span></span>
        {Object.entries(status.credentials).map(([k, ok]) => (
          <span key={k}><Dot tone={ok ? "ok" : "bad"} />{credentialLabel[k] ?? k} {ok ? "set" : "missing"}</span>
        ))}
      </div>
    </section>
  );
}

function SimulationPanel({ status, sims, refresh }: { status: Status; sims: Simulation[]; refresh: () => void }) {
  const [name, setName] = useState("");
  const [capital, setCapital] = useState("5000");
  const [error, setError] = useState("");
  const active = status.active_simulation;

  const run = async (fn: () => Promise<unknown>) => {
    try {
      setError("");
      await fn();
      refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const act = (a: string) => run(() => api(`/api/simulations/${active!.id}/${a}`, { method: "POST" }));
  const create = () => {
    if (active && !confirm(`This ends "${active.name}" (it is kept, never deleted) and starts a new simulation.`)) return;
    run(async () => {
      await api("/api/simulations", {
        method: "POST",
        body: JSON.stringify({ name: name || `Simulation ${sims.length + 1}`, starting_capital_usd: capital }),
      });
      setName("");
    });
  };

  return (
    <section className="card">
      <h2>Simulation</h2>
      {active ? (
        <div className="row">
          <span><b>{active.name}</b> · {active.mode} · {usd(active.starting_capital_usd)} per strategy</span>
          <span className={`pill ${active.state}`}>{active.state}</span>
          {active.state === "created" && <button onClick={() => act("start")}>Start</button>}
          {active.state === "running" && <button onClick={() => act("pause")}>Pause</button>}
          {active.state === "paused" && <button onClick={() => act("resume")}>Resume</button>}
        </div>
      ) : (
        <p className="muted">No simulation yet.</p>
      )}
      <div className="row">
        <input placeholder="Name" value={name} onChange={(e) => setName(e.target.value)} />
        <label>Starting capital (USD) <input type="number" min="1" value={capital} onChange={(e) => setCapital(e.target.value)} /></label>
        <button onClick={create}>New simulation</button>
      </div>
      {error && <p className="error">{error}</p>}
      {sims.length > 1 && (
        <table>
          <thead><tr><th>Past simulations</th><th>Created</th><th>Capital</th><th>State</th></tr></thead>
          <tbody>
            {sims.filter((s) => s.id !== active?.id).map((s) => (
              <tr key={s.id}>
                <td>{s.name}</td><td>{new Date(s.created_at_utc).toLocaleString()}</td>
                <td>{usd(s.starting_capital_usd)}</td><td>{s.state}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function LedgerPanel({ entries, chainOk }: { entries: LedgerEntry[]; chainOk: boolean | null }) {
  return (
    <section className="card">
      <h2>
        Ledger <span className="muted">(append-only)</span>
        {chainOk !== null && (
          <span className="right"><Dot tone={chainOk ? "ok" : "bad"} />{chainOk ? "hash chain intact" : "HASH CHAIN BROKEN"}</span>
        )}
      </h2>
      {entries.length === 0 ? <p className="muted">Empty.</p> : (
        <table>
          <thead><tr><th>#</th><th>Recorded</th><th>Event</th><th>Data</th><th>Hash</th></tr></thead>
          <tbody>
            {entries.map((e) => (
              <tr key={e.seq}>
                <td>{e.seq}</td><td>{new Date(e.recorded_at_utc).toLocaleString()}</td><td>{e.kind}</td>
                <td className="mono muted">{JSON.stringify(e.payload)}</td><td className="mono muted">{e.hash.slice(0, 10)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

export function App() {
  const { status, connected, receivedAt, eventCount } = useLiveStatus();
  const [sims, setSims] = useState<Simulation[]>([]);
  const [entries, setEntries] = useState<LedgerEntry[]>([]);
  const [chainOk, setChainOk] = useState<boolean | null>(null);
  const [now, setNow] = useState(Date.now());

  const refresh = useCallback(() => {
    api<Simulation[]>("/api/simulations").then(setSims).catch(() => {});
    api<LedgerEntry[]>("/api/ledger?limit=20").then(setEntries).catch(() => {});
    api<{ ok: boolean }>("/api/ledger/verify").then((v) => setChainOk(v.ok)).catch(() => setChainOk(null));
  }, []);

  useEffect(refresh, [refresh, eventCount, connected]);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const age = receivedAt ? Math.round((now - receivedAt) / 1000) : null;
  const stale = !connected || age === null || age > 10;

  return (
    <main>
      <header>
        <h1>TradeLab</h1>
        <span className="pill sim">SIMULATION · fake money · no real orders</span>
        <span className="right">
          <Dot tone={stale ? "bad" : "ok"} />
          {connected ? `backend live · updated ${age ?? "?"}s ago` : "backend disconnected, retrying"}
        </span>
      </header>

      {status?.price_feed.warning && (
        <div className="banner">
          <b>{status.price_feed.feed.toUpperCase()} feed, not the full market.</b> {status.price_feed.warning}
        </div>
      )}

      <section className="card headline">
        <div className="muted">Net result (after fees, tax and token cost)</div>
        <div className="big">—</div>
        <div className="muted">No strategy has run yet. Nothing is shown until real data produces it.</div>
      </section>

      {status ? (
        <>
          <SimulationPanel status={status} sims={sims} refresh={refresh} />
          <SystemStatus status={status} />
          <LedgerPanel entries={entries} chainOk={chainOk} />
          <footer className="muted">v{status.version} · server time {new Date(status.server_time_utc).toLocaleString()}</footer>
        </>
      ) : (
        <section className="card"><p className="muted">Waiting for the backend…</p></section>
      )}
    </main>
  );
}
