# TradeLab

An AI-driven trading **simulation**: real, live market data and pretend money. It exists to answer
one question: *can an LLM agent beat trivial strategies net of every cost?* There is no live-order
code and none will be added; the only trading host in the code base is Alpaca's **paper** endpoint.

## Principle: fidelity to reality

When in doubt the simulation comes out **worse** than reality, never better.

| Rule | Where it is enforced |
|---|---|
| Real data only; nothing synthetic or interpolated outside tests | `ingest/`; a ticker without a fresh quote cannot trade and is logged as suspended |
| Every data point stored with its receive time; no look-ahead | `Quote.recv_ts`; orders fill only on quotes received after the decision was ready |
| Buy at the ask, sell at the bid, first quote after the decision (LLM latency included) | `broker.py` |
| Fill capped at the quoted size; the rest stays queued | `broker.py` |
| Market hours, holidays, early closes, halts, whole shares, broker minimum price | `market.py`, `broker.py` |
| Broker fees, Italian capital gains tax, stamp duty, LLM token cost | `accounting.py`, `models.BrokerProfile` |
| Immutable ledger, no recalculation, no reset | `storage/db.py` (append-only triggers + SHA-256 chain) |

## Setup

```bash
cp .env.example .env        # fill in the keys; .env is git-ignored
docker compose up --build
```

Open http://localhost:8080, create a simulation in **Settings**, press **Start**.

Keys needed in `.env`:
- `ALPACA_API_KEY` / `ALPACA_API_SECRET`: Alpaca **paper** account. Without them there are no prices, no market calendar and nothing trades.
- `ICA_API_KEY`: IBM Consulting Advantage. Without it the AI agents do not run; the baselines still do.
- `SEC_USER_AGENT_EMAIL`: contact address the SEC requires. Without it filings are not ingested.

Data lives in `./data` (two SQLite files) and survives restarts: on start the engine rebuilds every
account and open order from the ledger. The dashboard is bound to localhost because it has no
login; on a cloud host put it behind authentication before setting `DASHBOARD_BIND=0.0.0.0`.

To add the multi-agent strategy: `INSTALL_TRADINGAGENTS=1 docker compose up --build`.

### Development

```bash
cd backend && pip install -e '.[dev]' && pytest          # 91 tests
uvicorn tradelab.main:app --reload                        # http://localhost:8000
cd frontend && npm install && npm run dev                 # http://localhost:5173
```

## Strategies (same starting money each)

| Id | Dashboard name | What it does |
|---|---|---|
| `buy_hold` | Buy & hold S&P 500 | Buys SPY once. The bar to beat. |
| `random` | Random | Random buys and sells. |
| `momentum` | Trend follower | Each afternoon holds the 3 biggest risers of the day. |
| `mean_reversion` | Dip buyer | Each afternoon holds the 3 biggest fallers of the day. |
| `llm_light` | AI trader | Cheap model triages events; one strong-model call decides; validated JSON. |
| `tradingagents` | AI team | TradingAgents multi-agent run per ticker per day (optional). |

Agents are fully autonomous: woken by news, filings, sharp price moves, their own fills, price
alerts, a timer and their own schedule. They pay for every token out of their own account at the
moment of the call and see what their thinking has cost.

## Decisions and their sources

- **Price feed: Alpaca IEX (free).** One exchange, about 4% of US volume: wider spreads and smaller
  sizes than the real market. Pessimistic, and more so for active strategies; the dashboard always
  says so. `ALPACA_FEED=sip` uses the full feed (paid). At most 30 live tickers on IEX: 20 core
  tickers plus rotating slots that agents request; held positions always keep their slot.
- **Quote sizes are treated as shares** (Alpaca changelog, 2025-11-03). If a feed still sent round
  lots the size would be under-estimated, which is the conservative error.
- **Broker: Directa SIM, regime amministrato.** 9 USD flat per executed US order, no currency
  conversion charge (Directa fee sheet, read 2026-11-14). The account is simulated in USD.
- **Tax:** 26% on each realised gain at the sale; losses become a credit for 4 years; fund/ETF gains
  cannot use that credit; average cost basis. 0.2% a year stamp duty on holdings, accrued daily.
- **LLM:** IBM Consulting Advantage, OpenAI-compatible. Token prices default to Anthropic list
  prices; set `LLM_PRICES` to what ICA charges you. A model without a price is refused.
- **News:** Alpaca news stream (company news), SEC EDGAR latest-filings feed, GDELT (world news).
  "Impact" is a rule-based estimate, not a prediction.
- **Company graph:** built once per core company by the cheap model from its latest 10-K, cached.
  Its cost is shown as a platform cost and is not charged to any strategy.
- **Headline number:** net liquidation value, i.e. everything sold at the bid, minus exit fees and
  tax. Unrealised losses are not netted against unrealised gains.
- **FinRL** was evaluated and left out: it needs training on historical data and adds little to the question.

## Known limits (read before trusting a result)

- **Stock splits and other corporate actions are not handled.** A split in a held ticker would
  distort that position's value. Check held tickers around split dates.
- **Dividends are ignored.** This understates every result, buy & hold the most.
- **Currency:** the account is in USD; EUR/USD moves and the euro-based tax computation are ignored.
- **SPY** is simulated as purchasable although an Italian retail account cannot buy it.
- **TradingAgents fetches its own data** (Yahoo Finance and others), which TradeLab cannot
  timestamp. Its orders still fill on our live quotes like everyone else's.
- **Paper cross-check** uses one Alpaca paper account for all strategies; Alpaca's paper engine
  ignores quoted size. It is a price sanity check only.
- **Replay** (`POST /api/replay`) is for debugging and runs the non-LLM baselines only; it is not
  valid for LLM agents and uses quotes conflated to one per second.
- **Not yet exercised against the live services:** the Alpaca streams, the paper mirror, the EDGAR
  poller and the TradingAgents adapter were written from the official documentation and are covered
  by parser and engine tests, but had no credentials available during development. The ICA calls
  and the full Docker stack were run for real.

## Layout

```
backend/tradelab/
  models.py accounting.py broker.py   core: orders, accounts, tax, simulated broker
  market.py                           calendar, halts, quotes, data slots
  engine.py                           event loop: data in, strategies, ledger out
  strategies/                         baselines, LLM agent, TradingAgents adapter
  ingest/                             Alpaca streams and REST, EDGAR, GDELT
  graph.py crosscheck.py metrics.py replay.py llm.py
  storage/                            ledger (append-only) and market data store
  services.py api/app.py              wiring and HTTP/websocket API
frontend/                             React + Vite + TypeScript dashboard
```

## Dependencies and why

| Dependency | Licence | Why |
|---|---|---|
| FastAPI, Uvicorn | MIT, BSD-3 | async HTTP and websocket API |
| Pydantic, pydantic-settings | MIT | validated config, payloads and LLM output |
| httpx, websockets | BSD-3 | REST calls and market data streams |
| tzdata | Apache-2.0 | exchange time zone in slim containers |
| SQLite (stdlib) | public domain | ledger and market data |
| React, Vite, TypeScript | MIT, MIT, Apache-2.0 | dashboard |
| TradingAgents v0.7.1 (optional) | Apache-2.0 | multi-agent strategy, installed as a package |
| pytest (dev) | MIT | tests |

No third-party code is copied into this repository. NautilusTrader (LGPL-3.0) is not used; the
broker sits behind a small interface (`submit`, `cancel`, `on_quote`) so it could replace it later.
