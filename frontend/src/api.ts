import { useCallback, useEffect, useRef, useState } from "react";

export interface Feed { id: string; source: string; state: string; last_received_at_utc: string | null; detail: string }
export interface Simulation {
  id: string; name: string; mode: string; starting_capital_usd: string; predecessor_id: string | null;
  created_at_utc: string; state: "created" | "running" | "paused" | "ended";
}
export interface MarketInfo { state: "open" | "closed" | "unknown"; detail: string; opens_at_utc?: string; closes_at_utc?: string }
export interface Status {
  server_time_utc: string; version: string; live_trading: boolean;
  price_feed: { provider: string; feed: string; full_market: boolean; warning: string; streamed_symbols: number; capacity: number };
  market: MarketInfo; feeds: Feed[]; credentials: Record<string, boolean>;
  halted: Record<string, { since: string; message: string }>; ledger_entries: number; malformed_messages: number;
  feed_events: { ts: string; feed: string; event: string; detail: string }[]; company_graphs: number;
  platform_llm_cost: { usd: number; calls: number; note: string };
  llm: { triage_model: string; decision_model: string; prices_usd_per_million: Record<string, number[]> };
  active_simulation: Simulation | null;
}
export interface StrategyRow {
  id: string; name: string; description: string; is_llm: boolean; active: boolean; thinking: boolean;
  market: "stocks" | "crypto"; is_benchmark: boolean;
  net_result: number; gross_result: number; net_liquidation: number; equity: number; cash: number; deposits: number; dividends: number;
  positions: number; open_orders: number;
  costs: { commissions: number; taxes: number; tokens: number; stamp_duty: number; exit_costs_if_sold_now: number };
  quote_gap: number;
  prices_stale_for: string[]; prices_missing_for: string[]; suspended_positions: string[];
  return_pct: number; gross_return_pct: number; max_drawdown_pct: number; sharpe: number | null; trading_days: number;
  win_rate_pct: number | null; closed_trades: number; orders: number; fills: number; turnover: number;
  avg_cost_per_order: number | null; token_spend: number; vs_buy_hold_pct: number | null;
}
export interface Overview {
  simulation: Simulation | null; market: MarketInfo; strategies: StrategyRow[]; kill_switch: boolean;
  blocked: Record<string, string>; frozen: Record<string, string>;
  settings: Record<string, number | null>; not_running: Record<string, string>; server_time_utc: string;
  broker: { name: string; commission_per_order: number; tax_rate_pct: number; source: string };
}
export interface Activity {
  seq: number; at: string; kind: string; strategy_id: string | null; strategy: string | null;
  symbol?: string; side?: string; qty?: number; price?: number; commission?: number; tax?: number;
  realized_gain?: number | null; rationale?: string; summary?: string; confidence?: number; wakes?: string[];
  news?: { id: string; headline: string; source: string; url: string; recv_ts: string }[];
  quote?: { bid: string; ask: string; source: string; recv_ts: string }; by_risk_manager?: boolean; by_exit_plan?: boolean; pieces?: number;
  orders?: number; thinking_ms?: number; token_cost?: number; models?: string[]; error?: string;
  reason?: string; unfilled_qty?: number; amount?: number; detail?: Record<string, unknown>;
}
export interface NewsItem {
  id: string; kind: string; source: string; recv_ts: string; published_ts: string | null; headline: string;
  summary: string; url: string; symbols: string[]; links: { symbol: string; relation: string; via: string }[];
  impact: "low" | "medium" | "high"; extra: Record<string, unknown>;
}
export interface Position {
  symbol: string; name: string; qty: number; avg_cost: number; bid: number | null; value: number | null;
  unrealized: number | null; last_trade: number | null; spread_pct: number | null; price_source: string | null; price_received_at_utc: string | null;
  price_is_fresh: boolean; halted: boolean;
}
export interface Detail { id: string; exit_plans: Record<string, { take_profit: string | null; stop_loss: string | null }>; positions: Position[]; open_orders: Record<string, unknown>[]; alerts: Record<string, unknown>[]; tax_loss_credit: number }
export interface QuoteRow {
  symbol: string; name: string; core: boolean; bid: number | null; ask: number | null; bid_size: number | null;
  ask_size: number | null; crypto?: boolean; source: string | null; received_at_utc: string | null; age_seconds: number | null;
  fresh: boolean; halted: boolean;
}
export interface Report {
  created_at_utc: string; headline: string; benchmark_return_pct: number | null; min_days_for_significance: number;
  strategies: { id: string; name: string; is_llm: boolean; return_pct: number; excess_return_pct: number; days: number;
    p_value: number | null; closed_trades: number; win_rate_pct: number | null; verdict: string }[];
}
export interface Crosscheck {
  orders_mirrored: number; comparable: number; avg_deviation_bps: string | null; worst_deviation_bps: string | null;
  not_comparable: number; note: string;
  recent: { order_id: string; symbol: string; side: string; qty: number; sim_avg_price: string | null;
    paper_avg_price: string | null; deviation_bps: string | null; paper_status: string; detail: string }[];
}
export type Curves = Record<string, [string, number][]>;

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...init });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `${r.status} ${r.statusText}`);
  }
  return r.json();
}

/** Live status + overview over websocket, with automatic reconnection. */
export function useLive() {
  const [status, setStatus] = useState<Status | null>(null);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [connected, setConnected] = useState(false);
  const [receivedAt, setReceivedAt] = useState<number | null>(null);
  const [eventCount, setEventCount] = useState(0);
  const retry = useRef(0);
  const pending = useRef<number | undefined>(undefined);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let timer: number | undefined;
    let closed = false;
    const connect = () => {
      ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
      ws.onopen = () => { retry.current = 0; setConnected(true); };
      ws.onmessage = (m) => {
        const msg = JSON.parse(m.data);
        if (msg.type === "tick") { setStatus(msg.status); setOverview(msg.overview); setReceivedAt(Date.now()); }
        else if (msg.type === "event" && pending.current === undefined)       // a burst of events = one refresh
          pending.current = window.setTimeout(() => { pending.current = undefined; setEventCount((n) => n + 1); }, 2500);
      };
      ws.onclose = () => {
        setConnected(false);
        if (closed) return;
        retry.current = Math.min(retry.current + 1, 6);
        timer = window.setTimeout(connect, 500 * 2 ** retry.current);
      };
    };
    connect();
    return () => { closed = true; window.clearTimeout(timer); ws?.close(); };
  }, []);
  return { status, overview, connected, receivedAt, eventCount };
}

/** Fetch on mount, when `deps` change, and every `everyMs`. Keeps the last good value on errors. */
export function usePoll<T>(path: string | null, deps: unknown[], everyMs = 15000): [T | null, () => void] {
  const [data, setData] = useState<T | null>(null);
  const load = useCallback(() => {
    if (path) api<T>(path).then(setData).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);
  useEffect(() => {
    load();
    const t = window.setInterval(load, everyMs);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, everyMs, ...deps]);
  return [data, load];
}

// ---- formatting
export const usd = (v: number, digits = 2) =>
  v.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits });
export const signed = (v: number, digits = 2) => (v > 0 ? "+" : v < 0 ? "−" : "") + usd(Math.abs(v), digits);
export const pct = (v: number | null | undefined, digits = 2) =>
  v == null ? "—" : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(digits) + "%";
/** Shares are whole numbers; coins can be fractions. */
export const amount = (v: number) => (Number.isInteger(v) ? String(v) : v.toFixed(v < 1 ? 6 : 4).replace(/0+$/, "").replace(/\.$/, ""));
export const clock = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleTimeString() : "never");
export const dateTime = (iso: string) => new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "never";
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

// Colour follows the strategy, never its rank (validated 6-series palette for dark surfaces).
const SERIES: Record<string, string> = {
  llm_light: "#3987e5", tradingagents: "#d95926", buy_hold: "#199e70", momentum: "#c98500",
  mean_reversion: "#d55181", random: "#0ca30c", technical_rules: "#9085e9", crypto_ai: "#e66767", crypto_buy_hold: "#8b95a8",
};
export const seriesColor = (id: string) => SERIES[id] ?? "#9085e9";
