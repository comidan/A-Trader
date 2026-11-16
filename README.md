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

Open http://localhost:8080 and press **Start a simulation**.

Run only one TradeLab per Alpaca account: Alpaca allows a single streaming connection per key.

Keys needed in `.env`:
- `ALPACA_API_KEY` / `ALPACA_API_SECRET`: Alpaca **paper** account. Without them there are no prices, no market calendar and nothing trades.
- `ICA_API_KEY`: IBM Consulting Advantage. Without it the AI agents do not run; the baselines still do.
- `SEC_USER_AGENT_EMAIL`: contact address the SEC requires. Without it filings are not ingested.

Data lives in `./data` (two SQLite files) and survives restarts: on start the engine rebuilds every
account and open order from the ledger. The dashboard is bound to localhost by default. To reach
it from elsewhere set `DASHBOARD_PASSWORD` (user name `tradelab`) and `DASHBOARD_BIND=0.0.0.0`,
and serve it over HTTPS.

To add the multi-agent strategy: `INSTALL_TRADINGAGENTS=1 docker compose up --build`.

### Development

```bash
cd backend && pip install -e '.[dev]' && pytest          # 127 tests
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
| `technical_rules` | Rule-based trader | No AI: buys strong stocks after a sharp two-day drop, sells the bounce. Once a day. |
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
  Dividends: 15% US withholding, then 26% Italian tax on the remainder.
- **LLM:** IBM Consulting Advantage, OpenAI-compatible. Token prices default to Anthropic list
  prices; set `LLM_PRICES` to what ICA charges you. A model without a price is refused.
- **News:** Alpaca news stream (company news), SEC EDGAR latest-filings feed, public RSS feeds (BBC,
  MarketWatch, Federal Reserve; `WORLD_NEWS_FEEDS`) and GDELT for world news. GDELT is read from its
  public 15-minute knowledge-graph files, not its query API (which rate-limits by IP and refuses most
  networks). Of the thousand-plus articles per batch only those touching a followed company, plus a
  few macro-economic ones, are kept. "Impact" is a rule-based estimate, not a prediction.
- **Company graph:** built once per core company by the cheap model from its latest 10-K, cached.
  Its cost is shown as a platform cost and is not charged to any strategy.
- **Headline number:** net liquidation value, i.e. everything sold at the bid, minus exit fees and
  tax. Unrealised losses are not netted against unrealised gains.
- **FinRL** was evaluated and left out: it needs training on historical data and adds little to the question.

## Corporate actions

Splits and cash dividends come from Alpaca's corporate actions API, checked at start, every two
hours and at each market open, for every followed or held ticker:

- **Splits** multiply the shares held before the ex-date (fractions are forfeited), keep the cost
  basis and cancel open orders in that ticker. The pre-split price is never used for valuation.
- **Cash dividends** are paid on the payable date for shares held before the ex-date, net of 15% US
  withholding and 26% Italian tax.
- **Anything else** on a holding (spin-off, merger, ticker change, ...) is not simulated: new
  purchases of that ticker are refused and the dashboard asks you to check it.
- **Safety net:** a price that moves more than 40% between sessions with no known split freezes the
  ticker (no fills) until the split arrives or you release it in the dashboard.

## What the AI trader sees and how it is told to work

It is instructed as a professional active trader whose job is to beat the S&P 500 by buying and
selling stocks. It may not buy broad index funds (`LLM_ALLOW_INDEX_FUNDS=false`): holding the
benchmark is not trading. At each decision it receives:

- live quotes, with each stock's adjusted history and its performance against SPY over 20 and 60 days;
- the gap between its own result and buy & hold;
- news and filings since its last decision, with links to suppliers, customers and competitors, and
  the actual text of the most important articles and 8-K press releases (not only headlines);
- today's biggest movers and most traded stocks across the whole US market, so it can look beyond
  the followed tickers and ask for any of them;
- its positions with break-even prices, its recent trades with after-tax results, and its own
  trading journal (its only memory, stored in the ledger).

It also receives technical indicators and the rule-based signals firing on each stock, with the
past record of those rules after costs (Tools tab); the move priced by the options market and the
put/call balance (Alpaca options data); StockTwits crowd sentiment; and, with a free
`FINNHUB_API_KEY`, the next earnings date and analyst ratings. It can write durable lessons for
itself, which are shown to it at every decision. Order-book depth is not available on these feeds.

It is called on a clock (every 15 minutes by default) to scan prices, and by events in between.
Every purchase can carry a take-profit and a stop: the engine then sells the instant either level is
reached, with no model call. Take-profits trigger on the quoted bid; stops trigger on a real traded
price, never on a quote that merely widened.

None of this guarantees it beats buy & hold: commissions, tax and the pessimistic free price feed
all weigh on an active trader. That is what the experiment measures.

## Known limits (read before trusting a result)

- **Currency:** the account is in USD; EUR/USD moves and the euro-based tax computation are ignored.
- **SPY** is simulated as purchasable although an Italian retail account cannot buy it.
- **Corporate actions other than splits and cash dividends** are flagged, not simulated (see above).
  Alpaca gives no guarantee on how early it publishes an action; the price-jump freeze covers the gap.
- **TradingAgents fetches its own data** (Yahoo Finance and others), which TradeLab cannot
  timestamp. Its orders still fill on our live quotes like everyone else's.
- **Paper cross-check** uses one Alpaca paper account for all strategies; Alpaca's paper engine
  ignores quoted size. It is a price sanity check only.
- **Replay** (Tools tab) is for checking the machinery and runs the non-LLM baselines only; it is
  not valid for LLM agents and uses quotes conflated to one per second.
- **Verified live (2026-11-14, market closed):** Alpaca account, calendar, asset list, corporate
  actions, daily bars, the quote/news websockets (authentication, subscription, message parsing on
  Alpaca's test stream) and a paper order submit, lookup and cancel; the ICA calls; the Docker stack.
- **Not yet observed:** a full trading session with live quotes and fills (the market was closed),
  the EDGAR poller (needs your contact email), GDELT from an unthrottled network, and the
  TradingAgents adapter (optional install). Watch the System tab on the first open-market day.

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
