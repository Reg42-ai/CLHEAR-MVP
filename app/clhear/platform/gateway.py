"""LLM provider abstraction, spend caps, call ledger (HLD §7.1).

Determinism rule (HLD §8.3): no LLM call outside this module; in this build
only watcher-candidate triage may call it. Every call is logged to
l0_platform.llm_calls; caps are hard stops ($20/day per fleet, $100/day global).
"""
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.models import llm_calls
from app.clhear.settings import get_settings

log = logging.getLogger("clhear.gateway")


class SpendCapExceeded(RuntimeError):
    pass


class StructuredOutputError(RuntimeError):
    pass


@dataclass(frozen=True)
class LlmResult:
    text: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


class Provider(Protocol):
    name: str

    def complete(self, *, model: str, prompt: str, system: str | None, max_tokens: int) -> LlmResult: ...


# USD per 1M tokens (input, output). Extend as models are used.
ANTHROPIC_PRICING = {
    "claude-3-5-haiku-latest": (0.80, 4.00),
    "claude-sonnet-4-20250514": (3.00, 15.00),
}
_DEFAULT_PRICING = (3.00, 15.00)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key or get_settings().anthropic_api_key
        if not self._api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")

    def complete(self, *, model: str, prompt: str, system: str | None, max_tokens: int) -> LlmResult:
        import httpx

        body: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        text = "".join(block.get("text", "") for block in data.get("content", []))
        usage = data.get("usage", {})
        in_tok = int(usage.get("input_tokens", 0))
        out_tok = int(usage.get("output_tokens", 0))
        price_in, price_out = ANTHROPIC_PRICING.get(model, _DEFAULT_PRICING)
        cost = (in_tok * price_in + out_tok * price_out) / 1_000_000
        return LlmResult(
            text=text, model=model, provider=self.name,
            input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost,
        )


class OpenAICompatProvider:
    """Stub for future vLLM / OpenAI-compatible endpoints (HLD §7.1)."""

    name = "openai-compat"

    def __init__(self, base_url: str = "", api_key: str = ""):
        self._base_url = base_url
        self._api_key = api_key

    def complete(self, *, model: str, prompt: str, system: str | None, max_tokens: int) -> LlmResult:
        raise NotImplementedError("OpenAICompatProvider is a stub in this build")


class FakeProvider:
    """Deterministic offline provider for tests and the dummy-fleet rehearsal."""

    name = "fake"

    def __init__(self, canned_text: str = '{"classification": "relevant", "confidence": 0.9}'):
        self.canned_text = canned_text
        self.calls = 0

    def complete(self, *, model: str, prompt: str, system: str | None, max_tokens: int) -> LlmResult:
        self.calls += 1
        return LlmResult(
            text=self.canned_text, model=model, provider=self.name,
            input_tokens=max(1, len(prompt) // 4), output_tokens=max(1, len(self.canned_text) // 4),
            cost_usd=0.0001,
        )


def _day_start_utc() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


class Gateway:
    def __init__(
        self,
        engine: Engine,
        provider: Provider,
        fleet_daily_cap_usd: float | None = None,
        global_daily_cap_usd: float | None = None,
    ):
        settings = get_settings()
        self._engine = engine
        self._provider = provider
        self._fleet_cap = fleet_daily_cap_usd if fleet_daily_cap_usd is not None else settings.clhear_gateway_fleet_daily_cap_usd
        self._global_cap = global_daily_cap_usd if global_daily_cap_usd is not None else settings.clhear_gateway_global_daily_cap_usd

    def _spend_today(self, fleet: str | None = None) -> float:
        query = sa.select(sa.func.coalesce(sa.func.sum(llm_calls.c.cost_usd), 0)).where(
            llm_calls.c.created_at >= _day_start_utc()
        )
        if fleet is not None:
            query = query.where(llm_calls.c.fleet == fleet)
        with self._engine.connect() as conn:
            return float(conn.execute(query).scalar_one())

    def call(
        self,
        *,
        fleet: str,
        model: str,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 1024,
        required_keys: list[str] | None = None,
        max_retries: int = 3,
    ) -> LlmResult:
        """One gated LLM call: cap check -> provider (retry/backoff) -> ledger.

        If required_keys is given the response must be a JSON object containing
        all of them (structured-output validation), retried within the budget.
        """
        if self._spend_today(fleet) >= self._fleet_cap:
            raise SpendCapExceeded(f"fleet '{fleet}' daily cap ${self._fleet_cap} reached — hard stop")
        if self._spend_today() >= self._global_cap:
            raise SpendCapExceeded(f"global daily cap ${self._global_cap} reached — hard stop")

        last_error: Exception | None = None
        result: LlmResult | None = None
        for attempt in range(max_retries):
            try:
                result = self._provider.complete(model=model, prompt=prompt, system=system, max_tokens=max_tokens)
                if required_keys is not None:
                    parsed = json.loads(result.text)
                    if not isinstance(parsed, dict):
                        raise StructuredOutputError("response is not a JSON object")
                    missing = [k for k in required_keys if k not in parsed]
                    if missing:
                        raise StructuredOutputError(f"missing keys: {missing}")
                break
            except (json.JSONDecodeError, StructuredOutputError, ConnectionError, TimeoutError) as exc:
                last_error = exc
                result = None
                time.sleep(2**attempt * 0.5)
        if result is None:
            raise StructuredOutputError(f"gateway call failed after {max_retries} attempts: {last_error}")

        with self._engine.begin() as conn:
            conn.execute(
                llm_calls.insert().values(
                    fleet=fleet,
                    provider=result.provider,
                    model=result.model,
                    prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(),
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    cost_usd=result.cost_usd,
                )
            )
        return result
