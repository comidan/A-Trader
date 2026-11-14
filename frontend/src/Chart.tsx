import { useEffect, useMemo, useRef, useState } from "react";
import { Curves, dateTime, seriesColor, usd } from "./api";

interface Props { curves: Curves; names: Record<string, string>; baseline: number; hidden: Set<string>; onToggle: (id: string) => void }

/** "Value of the money put in" for every strategy. One y-axis, time on x by observation (no overnight gaps). */
export function EquityChart({ curves, names, baseline, hidden, onToggle }: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(900);
  const [hover, setHover] = useState<number | null>(null);
  useEffect(() => {
    if (!wrap.current) return;
    const ro = new ResizeObserver((e) => setWidth(Math.max(320, e[0].contentRect.width)));
    ro.observe(wrap.current);
    return () => ro.disconnect();
  }, []);

  const ids = Object.keys(names);
  const model = useMemo(() => {
    const minute = (t: string) => t.slice(0, 16);
    const times = [...new Set(Object.values(curves).flatMap((s) => s.map((p) => minute(p[0]))))].sort();
    const index = new Map(times.map((t, i) => [t, i]));
    const series = ids.map((id) => {
      const values: (number | null)[] = new Array(times.length).fill(null);
      for (const [t, v] of curves[id] ?? []) values[index.get(minute(t))!] = v;
      let last: number | null = null;                       // carry the last real value forward between snapshots
      for (let i = 0; i < values.length; i++) { if (values[i] == null) values[i] = last; else last = values[i]; }
      return { id, values };
    });
    return { times, series };
  }, [curves, ids.join(",")]);

  const H = 300, M = { t: 14, r: 16, b: 26, l: 62 };
  const shown = model.series.filter((s) => !hidden.has(s.id));
  const all = shown.flatMap((s) => s.values.filter((v): v is number => v != null)).concat(baseline);
  const lo = Math.min(...all), hi = Math.max(...all);
  const pad = Math.max((hi - lo) * 0.12, baseline * 0.002);
  const y0 = lo - pad, y1 = hi + pad;
  const n = model.times.length;
  const x = (i: number) => M.l + (n <= 1 ? 0 : (i / (n - 1)) * (width - M.l - M.r));
  const y = (v: number) => M.t + (1 - (v - y0) / (y1 - y0)) * (H - M.t - M.b);
  const raw = (y1 - y0) / 5, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((v) => v >= raw)!;      // round, readable tick values
  const ticks: number[] = [];
  for (let t = Math.ceil(y0 / step) * step; t <= y1; t += step) ticks.push(t);
  const path = (values: (number | null)[]) => {
    let d = "", pen = false;
    values.forEach((v, i) => { if (v == null) { pen = false; return; } d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`; pen = true; });
    return d;
  };
  const xLabels = n > 1 ? [0, Math.floor((n - 1) / 2), n - 1] : [];
  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    if (n < 2) return;
    const box = e.currentTarget.getBoundingClientRect();
    const rel = (e.clientX - box.left - M.l) / (width - M.l - M.r);
    setHover(Math.min(n - 1, Math.max(0, Math.round(rel * (n - 1)))));
  };
  const tip = hover != null
    ? shown.map((s) => ({ id: s.id, v: s.values[hover] })).filter((r) => r.v != null).sort((a, b) => b.v! - a.v!)
    : [];

  return (
    <div ref={wrap} className="chart">
      <div className="legend">
        {ids.map((id) => (
          <button key={id} className={`legend-item ${hidden.has(id) ? "off" : ""}`} onClick={() => onToggle(id)}
                  title="Click to show or hide">
            <span className="swatch" style={{ background: seriesColor(id) }} />{names[id]}
          </button>
        ))}
      </div>
      {n < 2 ? (
        <div className="empty">The chart starts once the market is open and the simulation is running. Nothing is drawn until real prices arrive.</div>
      ) : (
        <div className="plot">
          <svg width={width} height={H} onMouseMove={onMove} onMouseLeave={() => setHover(null)} role="img"
               aria-label="Value over time of each strategy">
            {ticks.map((t) => (
              <g key={t}>
                <line x1={M.l} x2={width - M.r} y1={y(t)} y2={y(t)} className="gridline" />
                <text x={M.l - 8} y={y(t) + 4} textAnchor="end" className="axis">{usd(t, 0)}</text>
              </g>
            ))}
            <line x1={M.l} x2={width - M.r} y1={y(baseline)} y2={y(baseline)} className="baseline" />
            <text x={width - M.r} y={y(baseline) - 5} textAnchor="end" className="axis">money put in</text>
            {xLabels.map((i) => (
              <text key={i} x={x(i)} y={H - 6} textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"} className="axis">
                {dateTime(model.times[i] + ":00Z")}
              </text>
            ))}
            {shown.map((s) => <path key={s.id} d={path(s.values)} fill="none" stroke={seriesColor(s.id)} strokeWidth={2} strokeLinejoin="round" />)}
            {hover != null && (
              <g>
                <line x1={x(hover)} x2={x(hover)} y1={M.t} y2={H - M.b} className="crosshair" />
                {tip.map((r) => <circle key={r.id} cx={x(hover)} cy={y(r.v!)} r={4} fill={seriesColor(r.id)} stroke="var(--card)" strokeWidth={2} />)}
              </g>
            )}
          </svg>
          {hover != null && (
            <div className="tooltip" style={x(hover) > width / 2 ? { right: width - x(hover) + 12 } : { left: x(hover) + 12 }}>
              <div className="muted">{dateTime(model.times[hover] + ":00Z")}</div>
              {tip.map((r) => (
                <div key={r.id} className="tip-row">
                  <span className="swatch" style={{ background: seriesColor(r.id) }} />{names[r.id]}<b>{usd(r.v!)}</b>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
