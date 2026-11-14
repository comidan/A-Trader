import { useEffect, useRef, useState } from "react";

export interface Feed {
  id: string;
  source: string;
  state: "not_started" | "connecting" | "live" | "stalled" | "error";
  last_received_at_utc: string | null;
  detail: string;
}

export interface Simulation {
  id: string;
  name: string;
  mode: "forward" | "replay";
  starting_capital_usd: string;
  predecessor_id: string | null;
  created_at_utc: string;
  state: "created" | "running" | "paused" | "ended";
}

export interface Status {
  server_time_utc: string;
  version: string;
  price_feed: { provider: string; feed: string; full_market: boolean; warning: string };
  market: { state: string; detail: string };
  feeds: Feed[];
  credentials: Record<string, boolean>;
  active_simulation: Simulation | null;
  ledger_entries: number;
  live_trading: boolean;
}

export interface LedgerEntry {
  seq: number;
  sim_id: string;
  strategy_id: string | null;
  recorded_at_utc: string;
  kind: string;
  payload: Record<string, unknown>;
  hash: string;
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...init });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `${r.status} ${r.statusText}`);
  }
  return r.json();
}

/** Live status over websocket, with automatic reconnection. `receivedAt` is the browser receive time. */
export function useLiveStatus() {
  const [status, setStatus] = useState<Status | null>(null);
  const [connected, setConnected] = useState(false);
  const [receivedAt, setReceivedAt] = useState<number | null>(null);
  const [eventCount, setEventCount] = useState(0);
  const retry = useRef(0);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let timer: number | undefined;
    let closed = false;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onopen = () => {
        retry.current = 0;
        setConnected(true);
      };
      ws.onmessage = (m) => {
        const msg = JSON.parse(m.data);
        if (msg.type === "status") {
          setStatus(msg.payload);
          setReceivedAt(Date.now());
        } else if (msg.type === "event") {
          setEventCount((n) => n + 1);
        }
      };
      ws.onclose = () => {
        setConnected(false);
        if (closed) return;
        retry.current = Math.min(retry.current + 1, 6);
        timer = window.setTimeout(connect, 500 * 2 ** retry.current);
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      ws?.close();
    };
  }, []);

  return { status, connected, receivedAt, eventCount };
}
