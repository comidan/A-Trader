# TradeLab

An AI-driven trading **simulation**: real market data, fake money. It exists to answer one
question: *can an LLM agent beat trivial strategies net of every cost?* There is no live-order
code and none will be added.

## Principle: fidelity to reality

When in doubt the simulation must come out **worse** than reality, never better: real data only
(no synthetic or interpolated values outside automated tests), buys at the ask and sells at the
bid from the first quote after the decision is ready, fills capped at the quoted size, real broker
fees, Italian capital gains tax and LLM token cost, and an immutable ledger.

## Status

| Milestone | Content | State |
|---|---|---|
| M0 | Skeleton: compose, API, dashboard shell, append-only ledger, simulation lifecycle | done |
| M1 | Price ingestion (quotes, bars, halts, calendar, stale-feed suspension) | next |
| M2 | Simulated broker, accounting, risk manager | |
| M3 | Strategy runtime and baselines | |
| M4 | News and filings (Alpaca news, SEC EDGAR, GDELT) | |
| M5 | Lightweight LLM agent | |
| M6 | Alpaca paper cross-check | |
| M7 | Company relationship graph, world-news mapping | |
| M8 | TradingAgents integration | |
| M9 | Weekly report, replay mode, hardening | |

## Setup

```bash
cp .env.example .env      # fill in keys; .env is git-ignored
docker compose up --build
```

Open http://localhost:8080. Data lives in `./data` and survives restarts. The dashboard is bound
to localhost because it has no login yet; set `DASHBOARD_BIND=0.0.0.0` only behind authentication.

### Development

```bash
cd backend && pip install -e '.[dev]' && pytest
uvicorn tradelab.main:app --reload          # http://localhost:8000
cd frontend && npm install && npm run dev   # http://localhost:5173, proxies to the backend
```

## Decisions so far

- **Price feed: Alpaca IEX (free plan).** IEX is one exchange with roughly 4% of US volume, so
  spreads are wider and quoted sizes smaller than the real market. The error is pessimistic but
  falls on active strategies rather than buy & hold; the dashboard shows a permanent banner.
  `ALPACA_FEED=sip` switches to the full feed (paid plan). The app never downgrades silently.
- **Account currency USD**, starting capital 5,000 USD per strategy. Only the operator can add cash.
- **S&P 500 baseline: SPY**, simulated as purchasable although an Italian retail account cannot
  buy it. Labelled as such.
- **Tax: Italian "regime amministrato"**; broker fee schedule to be fixed at M2.
- **LLM: IBM Consulting Advantage** through its OpenAI-compatible endpoint.
- **Ledger:** SQLite, append-only by trigger, SHA-256 hash chain over every entry, money as exact
  decimal text. A reset does not exist: a new simulation ends the previous one and keeps it.

## Layout

```
backend/tradelab/
  config.py        settings from .env (secrets never serialised)
  bus.py           in-process event bus
  feeds.py         feed health registry
  storage/db.py    SQLite, append-only hash-chained ledger
  simulations.py   simulation lifecycle derived from ledger events
  api/app.py       FastAPI HTTP + websocket
frontend/          React + Vite + TypeScript dashboard
```

## Dependencies and why

| Dependency | Licence | Why |
|---|---|---|
| FastAPI, Uvicorn | MIT, BSD-3 | async HTTP and websocket API |
| Pydantic, pydantic-settings | MIT | validated config and payloads |
| SQLite (stdlib) | public domain | durable single-file ledger |
| React, Vite, TypeScript | MIT, MIT, Apache-2.0 | dashboard |
| pytest, httpx (dev) | MIT, BSD-3 | tests |

No third-party code is copied into this repository.
