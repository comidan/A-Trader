import { useMemo, useState } from "react";
import { ago, usePoll } from "./api";

interface Node {
  id: string; type: "ticker" | "company" | "filing" | "world"; label: string; name?: string; held_by?: string[];
  watched?: boolean; impact?: "low" | "medium" | "high"; source?: string; url?: string; received_at_utc?: string;
}
interface Link { a: string; b: string; kind: string; via?: string }
interface MapData { nodes: Node[]; links: Link[]; note: string }

const KIND: Record<string, string> = { about: "is about", supplier: "supplier", customer: "customer", competitor: "competitor" };
const W = 1100, H = 560;

/** Small force layout: nodes repel, links pull, everything drifts to the centre. Deterministic start. */
function layout(nodes: Node[], links: Link[]): Map<string, { x: number; y: number }> {
  const pos = nodes.map((n, i) => {
    const a = (i / Math.max(1, nodes.length)) * Math.PI * 2, r = n.type === "ticker" ? 150 : 240;
    return { x: W / 2 + Math.cos(a) * r, y: H / 2 + Math.sin(a) * r * 0.6, vx: 0, vy: 0 };
  });
  const index = new Map(nodes.map((n, i) => [n.id, i]));
  const edges = links.map((l) => [index.get(l.a)!, index.get(l.b)!] as const).filter(([a, b]) => a != null && b != null);
  for (let step = 0; step < 300; step++) {
    const cool = 1 - step / 300;
    for (let i = 0; i < pos.length; i++) {
      for (let j = i + 1; j < pos.length; j++) {
        const dx = pos[i].x - pos[j].x, dy = pos[i].y - pos[j].y, d2 = Math.max(dx * dx + dy * dy, 100);
        const f = 14000 / d2, d = Math.sqrt(d2);
        pos[i].vx += (dx / d) * f; pos[i].vy += (dy / d) * f; pos[j].vx -= (dx / d) * f; pos[j].vy -= (dy / d) * f;
      }
      pos[i].vx += (W / 2 - pos[i].x) * 0.004; pos[i].vy += (H / 2 - pos[i].y) * 0.009;
    }
    for (const [a, b] of edges) {
      const dx = pos[b].x - pos[a].x, dy = pos[b].y - pos[a].y, d = Math.max(Math.sqrt(dx * dx + dy * dy), 1), f = (d - 130) * 0.03;
      pos[a].vx += (dx / d) * f; pos[a].vy += (dy / d) * f; pos[b].vx -= (dx / d) * f; pos[b].vy -= (dy / d) * f;
    }
    for (const p of pos) {
      p.x = Math.min(W - 60, Math.max(60, p.x + p.vx * cool)); p.y = Math.min(H - 30, Math.max(30, p.y + p.vy * cool));
      p.vx *= 0.55; p.vy *= 0.55;
    }
  }
  return new Map(nodes.map((n, i) => [n.id, { x: pos[i].x, y: pos[i].y }]));
}

export function MapView({ refresh }: { refresh: number }) {
  const [data] = usePoll<MapData>("/api/map", [refresh], 30000);
  const [hover, setHover] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const focus = hover ?? picked;
  const key = data ? data.nodes.map((n) => n.id).join("|") + data.links.length : "";
  const pos = useMemo(() => (data ? layout(data.nodes, data.links) : new Map()), [key]);
  if (!data) return <p className="muted">Loading…</p>;
  if (data.nodes.length === 0)
    return <p className="muted">The map fills in as news and filings arrive: each story is linked to the companies it mentions and, through their suppliers, customers and competitors, to the stocks it may affect.</p>;

  const near = new Set<string>();
  if (focus) { near.add(focus); for (const l of data.links) { if (l.a === focus) near.add(l.b); if (l.b === focus) near.add(l.a); } }
  const dim = (id: string) => (focus && !near.has(id) ? 0.15 : 1);
  const node = focus ? data.nodes.find((n) => n.id === focus) : null;
  const touching = focus ? data.links.filter((l) => l.a === focus || l.b === focus) : [];
  const label = (id: string) => data.nodes.find((n) => n.id === id)?.label ?? id;

  return (
    <div className="map">
      <div className="legend">
        <span className="legend-item"><svg width="16" height="16"><circle cx="8" cy="8" r="6" className="n-ticker held" /></svg>Stock held by a strategy</span>
        <span className="legend-item"><svg width="16" height="16"><circle cx="8" cy="8" r="6" className="n-ticker" /></svg>Stock</span>
        <span className="legend-item"><svg width="16" height="16"><rect x="3" y="3" width="10" height="10" rx="2" className="n-news high" /></svg>News or filing (brighter = bigger estimated impact)</span>
        <span className="legend-item"><svg width="26" height="16"><line x1="1" x2="25" y1="8" y2="8" className="l-rel" /></svg>Business relationship</span>
      </div>
      <div className="map-body">
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Map of news, stocks and their business relationships" onMouseLeave={() => setHover(null)} onClick={() => setPicked(null)}>
          {data.links.map((l, i) => {
            const a = pos.get(l.a), b = pos.get(l.b);
            if (!a || !b) return null;
            const on = focus && (l.a === focus || l.b === focus);
            return <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y} className={l.kind === "about" ? "l-about" : "l-rel"}
                         opacity={focus ? (on ? 1 : 0.08) : 0.7} />;
          })}
          {data.nodes.map((n) => {
            const p = pos.get(n.id);
            if (!p) return null;
            const isTicker = n.type === "ticker";
            return (
              <g key={n.id} transform={`translate(${p.x},${p.y})`} opacity={dim(n.id)} className="map-node"
                 onMouseEnter={() => setHover(n.id)} onMouseLeave={() => setHover(null)}
                 onClick={(e) => { e.stopPropagation(); setPicked(n.id); }}>
                <circle r={26} fill="transparent" />
                {isTicker
                  ? <><circle r={n.held_by?.length ? 22 : 19} className={`n-ticker ${n.held_by?.length ? "held" : ""}`} />
                      <text textAnchor="middle" dy={4} className="t-ticker">{n.label}</text></>
                  : <rect x={-9} y={-9} width={18} height={18} rx={4} className={`n-news ${n.impact}`} />}
              </g>
            );
          })}
        </svg>
        <aside className="map-side">
          {!node ? <p className="muted">Hover or tap a stock or a news item to see its connections.</p> : node.type === "ticker" ? (
            <>
              <h3>{node.label}</h3>
              <p>{node.name}</p>
              <p className="muted small">{node.held_by?.length ? `Held by ${node.held_by.join(", ")}` : node.watched ? "Watched, not held" : "Mentioned in the news; no live price yet"}</p>
              {touching.map((l, i) => {
                const other = l.a === focus ? l.b : l.a;
                return <p key={i} className="small">{other.startsWith("N:")
                  ? <>{l.kind === "about" ? "In the news:" : `News about its ${l.kind} ${l.via}:`} {label(other)}</>
                  : <>{l.a === focus ? <><b>{label(other)}</b> is a {KIND[l.kind]}</> : <>It is a {KIND[l.kind]} of <b>{label(other)}</b></>}</>}</p>;
              })}
            </>
          ) : (
            <>
              <h3>{node.type === "filing" ? "Company filing" : node.type === "world" ? "World news" : "Company news"}</h3>
              <p>{node.url ? <a href={node.url} target="_blank" rel="noreferrer">{node.label}</a> : node.label}</p>
              <p className="muted small">{node.source} · received {ago(node.received_at_utc)} · estimated impact: {node.impact}</p>
              {touching.map((l, i) => <p key={i} className="small">{l.kind === "about" ? <>About <b>{label(l.b)}</b></>
                : <>May affect <b>{label(l.b)}</b>: {l.via} is its {l.kind}</>}</p>)}
            </>
          )}
        </aside>
      </div>
      <p className="muted small">{data.note}</p>
    </div>
  );
}
