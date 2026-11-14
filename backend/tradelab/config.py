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
    ica_base_url: str = "https://api.remea.ica.ibm.com/ica/v1"
    ica_namespace: Literal["chat-models", "assistants", "agents", "digital-workforce"] = "chat-models"

    sec_user_agent_email: str = ""

    starting_capital_usd: Decimal = Decimal("5000")
    db_path: str = "data/tradelab.sqlite"
    log_level: str = "INFO"
    status_push_seconds: float = 2.0

    @property
    def llm_openai_base_url(self) -> str:
        return f"{self.ica_base_url.rstrip('/')}/{self.ica_namespace}"

    def credentials_present(self) -> dict[str, bool]:
        return {
            "alpaca": bool(self.alpaca_api_key.get_secret_value() and self.alpaca_api_secret.get_secret_value()),
            "llm": bool(self.ica_api_key.get_secret_value()),
            "sec_user_agent": bool(self.sec_user_agent_email),
        }
