"""LLM access through IBM Consulting Advantage (OpenAI-compatible chat completions).

Every call returns its real token usage and its cost in USD. Cached tokens are
priced as normal input tokens (conservative). Sampling parameters are not sent:
the current Claude models behind ICA reject them.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from .config import Settings

MILLION = Decimal(1_000_000)


class LLMError(Exception):
    pass


@dataclass
class LLMResult:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal
    latency_ms: int
    purpose: str = ""

    def usage(self) -> dict[str, Any]:
        return {"model": self.model, "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                "usd": str(self.cost_usd), "latency_ms": self.latency_ms, "purpose": self.purpose}


def cost_usd(prices: dict[str, list[float]], model: str, prompt_tokens: int, completion_tokens: int) -> Decimal:
    if model not in prices:
        raise LLMError(f"no token price configured for model '{model}' (set LLM_PRICES); refusing to run unpriced")
    pin, pout = (Decimal(str(x)) for x in prices[model])
    return (prompt_tokens * pin + completion_tokens * pout) / MILLION


def extract_json(text: str) -> Any:
    """Parse the first JSON object in a model reply (tolerates code fences and prose around it)."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start:end + 1])


class LLM:
    def __init__(self, settings: Settings) -> None:
        self.prices = settings.llm_prices
        self.url = settings.llm_openai_base_url + "/chat/completions"
        self.key = settings.ica_api_key.get_secret_value()
        self.http = httpx.AsyncClient(timeout=180)

    @property
    def configured(self) -> bool:
        return bool(self.key)

    async def chat(self, model: str, system: str, user: str, max_tokens: int = 4000, purpose: str = "") -> LLMResult:
        if not self.configured:
            raise LLMError("ICA_API_KEY is not set")
        cost_usd(self.prices, model, 0, 0)          # fail before spending if the model is unpriced
        t0 = time.monotonic()
        try:
            r = await self.http.post(self.url, headers={"Authorization": f"Bearer {self.key}"}, json={
                "model": model, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        except httpx.HTTPError as e:
            raise LLMError(f"LLM request failed: {type(e).__name__}") from e
        latency = int((time.monotonic() - t0) * 1000)
        if r.status_code != 200:
            raise LLMError(f"LLM returned {r.status_code}: {r.text[:300]}")
        body = r.json()
        u = body.get("usage") or {}
        pt, ct = int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))
        if pt == 0 and ct == 0:
            raise LLMError("LLM reply carried no token usage: cost cannot be accounted")
        text = (body["choices"][0]["message"].get("content") or "") if body.get("choices") else ""
        return LLMResult(text, model, pt, ct, cost_usd(self.prices, model, pt, ct), latency, purpose)
