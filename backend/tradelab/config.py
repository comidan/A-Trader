"""Settings. Secrets come only from the environment / .env and are never serialised."""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    alpaca_api_key: SecretStr = SecretStr("")
    alpaca_api_secret: SecretStr = SecretStr("")
    alpaca_feed: Literal["iex", "sip"] = "iex"

    # IBM Consulting Advantage (OpenAI-compatible): {ica_base_url}/{ica_namespace}/chat/completions
    ica_api_key: SecretStr = SecretStr("")
    ica_base_url: str = "https://api.servicesessentials.ibm.com/v1"
    ica_namespace: Literal["chat-models", "assistants", "agents", "digital-workforce"] = "chat-models"

    sec_user_agent_email: str = ""

    starting_capital_usd: Decimal = Decimal("5000")
    db_path: str = "data/tradelab.sqlite"
    market_db_path: str = ""          # default: next to db_path

    # Always-streamed symbols. The free IEX plan streams at most 30 symbols in total;
    # the remaining slots rotate between tickers the agents ask for.
    core_symbols: str = "SPY,QQQ,AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,AVGO,JPM,V,UNH,XOM,LLY,WMT,COST,AMD,NFLX,BAC"
    stream_capacity: int = 0          # 0 = automatic: 30 on iex, 400 on sip
    quote_stale_seconds: float = 120
    strategies: str = ("buy_hold,random,momentum,mean_reversion,technical_rules,llm_light,tradingagents,"
                       "crypto_buy_hold,crypto_ai")
    # Crypto pairs followed (Alpaca, 24/7). Leave empty to switch crypto off.
    crypto_symbols: str = "BTC/USD,ETH/USD,SOL/USD,LTC/USD,BCH/USD,LINK/USD,AVAX/USD,AAVE/USD,UNI/USD,DOT/USD,XRP/USD,DOGE/USD"
    crypto_agent_interval_minutes: float = 30   # the crypto AI trader runs day and night: each look costs about 3 cents

    llm_triage_model: str = "claude-haiku-5-5"
    llm_decision_model: str = "claude-opus-5-5"
    # USD per million tokens [input, output]. Anthropic list prices read 2026-11-14; ICA's internal
    # rate may differ: override with LLM_PRICES='{"model": [in, out]}'. A model without a price is refused.
    llm_prices: dict[str, list[float]] = {"claude-haiku-5-5": [0.10, 0.50], "claude-sonnet-5-5": [2, 10],
                                          "claude-opus-5-5": [4, 20]}
    llm_agent_interval_minutes: float = 120   # the stock AI trader's own regular look (events wake it in between)
    # Broker fee per executed order. 9 USD is Directa's US fee; change it to simulate a cheaper broker.
    broker_commission_usd: Decimal = Decimal("9")
    broker_name: str = "Directa SIM (US market, regime amministrato)"
    llm_allow_index_funds: bool = False   # the AI trader must pick stocks, not buy the benchmark
    tradingagents_tickers: str = "NVDA,AAPL,MSFT"   # one multi-agent run per ticker per day
    finnhub_api_key: SecretStr = SecretStr("")      # optional, free: earnings dates and analyst ratings
    social_sentiment: bool = True                    # StockTwits crowd mood for followed stocks
    paper_crosscheck: bool = True
    dashboard_password: SecretStr = SecretStr("")   # if set, the dashboard and API ask for it (user: tradelab)
    ingest_enabled: bool = True       # tests switch live connections off
    edgar_poll_seconds: float = 30
    gdelt_poll_seconds: float = 300    # GDELT publishes a new file every 15 minutes; we look every 5
    # World news: public RSS feeds as "Name=url" pairs separated by commas.
    world_news_feeds: str = ("BBC Business=https://feeds.bbci.co.uk/news/business/rss.xml,"
                             "BBC World=https://feeds.bbci.co.uk/news/world/rss.xml,"
                             "MarketWatch=https://feeds.content.dowjones.io/public/rss/mw_topstories,"
                             "Federal Reserve=https://www.federalreserve.gov/feeds/press_all.xml")
    world_news_poll_seconds: float = 180
    log_level: str = "INFO"
    status_push_seconds: float = 2.0

    @property
    def llm_openai_base_url(self) -> str:
        return f"{self.ica_base_url.rstrip('/')}/{self.ica_namespace}"

    @property
    def rss_feeds(self) -> dict[str, str]:
        return dict(pair.split("=", 1) for pair in self.world_news_feeds.split(",") if "=" in pair)

    @property
    def crypto_list(self) -> list[str]:
        return [s.strip().upper() for s in self.crypto_symbols.split(",") if s.strip()]

    @property
    def core_list(self) -> list[str]:
        return [s.strip().upper() for s in self.core_symbols.split(",") if s.strip()]

    @property
    def capacity(self) -> int:
        return self.stream_capacity or (30 if self.alpaca_feed == "iex" else 400)

    @property
    def market_db(self) -> str:
        return self.market_db_path or self.db_path.replace(".sqlite", "") + "-market.sqlite"

    def credentials_present(self) -> dict[str, bool]:
        return {
            "alpaca": bool(self.alpaca_api_key.get_secret_value() and self.alpaca_api_secret.get_secret_value()),
            "llm": bool(self.ica_api_key.get_secret_value()),
            "sec_user_agent": bool(self.sec_user_agent_email),
        }
