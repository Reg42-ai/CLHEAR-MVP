"""LLM provider abstraction, spend caps, call ledger (HLD §7.1).

The inference router (`router.run`) is the only production entry; this module
is the provider + ledger layer underneath it. Every call is logged to
l0_platform.llm_calls; daily fleet/global caps and the monthly frontier cap
are hard stops.
"""
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

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

    def complete(
        self,
        *,
        model: str,
        prompt: str,
        system: str | None,
        max_tokens: int,
        temperature: float = 0.0,
        json_schema: dict | None = None,
    ) -> LlmResult: ...


# USD per 1M tokens (input, output). Local Ollama is electricity-only (near-zero).
ANTHROPIC_PRICING = {
    "claude-3-5-haiku-latest": (0.80, 4.00),
    "claude-sonnet-4-20250514": (3.00, 15.00),
}
OPENAI_PRICING = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}
XAI_PRICING = {
    "grok-2": (2.00, 10.00),
    "grok-3-mini": (0.30, 0.50),
}
LOCAL_PRICING = {
    "qwen3.5:4b": (0.0, 0.0),
    "qwen3.5:9b": (0.01, 0.01),
    "qwen3.6:27b": (0.08, 0.08),
}
_DEFAULT_PRICING = (3.00, 15.00)
_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_THINK_OPEN_RE = re.compile(r"<think>.*", re.S | re.I)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def parse_json_object(text: str) -> dict:
    """Parse a JSON object out of model text (think tags, fences, leading prose)."""
    raw = (text or "").strip()
    raw = _THINK_RE.sub("", raw)
    raw = _THINK_OPEN_RE.sub("", raw)
    raw = _FENCE_RE.sub("", raw.strip()).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(raw[start : end + 1])
    if not isinstance(parsed, dict):
        raise StructuredOutputError("response is not a JSON object")
    return parsed


def price_for(model: str) -> tuple[float, float]:
    for table in (ANTHROPIC_PRICING, OPENAI_PRICING, XAI_PRICING, LOCAL_PRICING):
        if model in table:
            return table[model]
    return _DEFAULT_PRICING


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key or get_settings().anthropic_api_key
        if not self._api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")

    def complete(
        self,
        *,
        model: str,
        prompt: str,
        system: str | None,
        max_tokens: int,
        temperature: float = 0.0,
        json_schema: dict | None = None,
    ) -> LlmResult:
        import httpx

        body: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
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
        price_in, price_out = price_for(model)
        cost = (in_tok * price_in + out_tok * price_out) / 1_000_000
        return LlmResult(
            text=text, model=model, provider=self.name,
            input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost,
        )


class OpenAICompatProvider:
    """OpenAI-compatible chat completions (OpenAI, xAI, vLLM, local proxies)."""

    name = "openai-compat"

    def __init__(self, base_url: str = "", api_key: str = "", provider_name: str = "openai-compat"):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.name = provider_name

    def complete(
        self,
        *,
        model: str,
        prompt: str,
        system: str | None,
        max_tokens: int,
        temperature: float = 0.0,
        json_schema: dict | None = None,
    ) -> LlmResult:
        import httpx

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "clhear", "schema": json_schema, "strict": False},
            }
        else:
            body["response_format"] = {"type": "json_object"}
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        resp = httpx.post(
            f"{self._base_url}/chat/completions",
            headers=headers,
            json=body,
            timeout=180,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"].get("content") or ""
        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        price_in, price_out = price_for(model)
        cost = (in_tok * price_in + out_tok * price_out) / 1_000_000
        return LlmResult(
            text=text, model=model, provider=self.name,
            input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost,
        )


class OpenAIProvider(OpenAICompatProvider):
    name = "openai"

    def __init__(self, api_key: str | None = None, base_url: str = "https://api.openai.com/v1"):
        key = api_key if api_key is not None else get_settings().openai_api_key
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        super().__init__(base_url=base_url, api_key=key, provider_name="openai")


class XAIProvider(OpenAICompatProvider):
    name = "xai"

    def __init__(self, api_key: str | None = None, base_url: str = "https://api.x.ai/v1"):
        key = api_key if api_key is not None else get_settings().xai_api_key
        if not key:
            raise RuntimeError("XAI_API_KEY is not configured")
        super().__init__(base_url=base_url, api_key=key, provider_name="xai")


class OllamaProvider:
    """Local Ollama with JSON-schema `format` enforcement."""

    name = "ollama"

    def __init__(self, base_url: str | None = None):
        self._base_url = (base_url or get_settings().ollama_base_url or "http://127.0.0.1:11434").rstrip("/")

    def complete(
        self,
        *,
        model: str,
        prompt: str,
        system: str | None,
        max_tokens: int,
        temperature: float = 0.0,
        json_schema: dict | None = None,
    ) -> LlmResult:
        import httpx

        body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "think": False,  # Qwen3.5 otherwise fills `thinking` and leaves response empty
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if system:
            body["system"] = system
        # Bare format=json makes some Qwen3.5 tags return an empty body.
        # Prefer a schema when we have one; otherwise parse free text.
        if json_schema:
            body["format"] = json_schema
        resp = httpx.post(f"{self._base_url}/api/generate", json=body, timeout=300)
        resp.raise_for_status()
        data = resp.json()
        text = (data.get("response") or data.get("thinking") or "").strip()
        in_tok = int(data.get("prompt_eval_count") or max(1, len(prompt) // 4))
        out_tok = int(data.get("eval_count") or max(1, len(text) // 4))
        price_in, price_out = price_for(model)
        cost = (in_tok * price_in + out_tok * price_out) / 1_000_000
        return LlmResult(
            text=text, model=model, provider=self.name,
            input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost,
        )


class FakeProvider:
    """Deterministic offline provider for tests and the dummy-fleet rehearsal."""

    name = "fake"

    def __init__(
        self,
        canned_text: str = '{"classification": "relevant", "confidence": 0.9}',
        script: Callable[..., str] | None = None,
    ):
        self.canned_text = canned_text
        self.script = script
        self.calls = 0
        self.last_kwargs: dict = {}

    def complete(
        self,
        *,
        model: str,
        prompt: str,
        system: str | None,
        max_tokens: int,
        temperature: float = 0.0,
        json_schema: dict | None = None,
    ) -> LlmResult:
        self.calls += 1
        self.last_kwargs = {
            "model": model, "prompt": prompt, "system": system,
            "max_tokens": max_tokens, "temperature": temperature, "json_schema": json_schema,
        }
        text = self.script(prompt=prompt, system=system, model=model) if self.script else self.canned_text
        return LlmResult(
            text=text, model=model, provider=self.name,
            input_tokens=max(1, len(prompt) // 4), output_tokens=max(1, len(text) // 4),
            cost_usd=0.0001,
        )


def _day_start_utc() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _month_start_utc() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class Gateway:
    def __init__(
        self,
        engine: Engine,
        provider: Provider,
        fleet_daily_cap_usd: float | None = None,
        global_daily_cap_usd: float | None = None,
        frontier_monthly_cap_usd: float | None = None,
    ):
        settings = get_settings()
        self._engine = engine
        self._provider = provider
        self._fleet_cap = fleet_daily_cap_usd if fleet_daily_cap_usd is not None else settings.clhear_gateway_fleet_daily_cap_usd
        self._global_cap = global_daily_cap_usd if global_daily_cap_usd is not None else settings.clhear_gateway_global_daily_cap_usd
        self._frontier_month_cap = (
            frontier_monthly_cap_usd
            if frontier_monthly_cap_usd is not None
            else settings.clhear_frontier_monthly_cap_usd
        )

    def _spend_today(self, fleet: str | None = None) -> float:
        query = sa.select(sa.func.coalesce(sa.func.sum(llm_calls.c.cost_usd), 0)).where(
            llm_calls.c.created_at >= _day_start_utc()
        )
        if fleet is not None:
            query = query.where(llm_calls.c.fleet == fleet)
        with self._engine.connect() as conn:
            return float(conn.execute(query).scalar_one())

    def frontier_spend_month(self) -> float:
        query = sa.select(sa.func.coalesce(sa.func.sum(llm_calls.c.cost_usd), 0)).where(
            llm_calls.c.created_at >= _month_start_utc()
        ).where(llm_calls.c.tier == "frontier")
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
        temperature: float = 0.0,
        json_schema: dict | None = None,
        provider: Provider | None = None,
        task_id: str | None = None,
        tier: str | None = None,
        rejected_alternatives: list | None = None,
        routing_reason: str | None = None,
        quality_at_decision: float | None = None,
    ) -> LlmResult:
        """One gated LLM call: cap check -> provider (retry/backoff) -> ledger.

        If required_keys is given the response must be a JSON object containing
        all of them (structured-output validation), retried within the budget.
        """
        if self._spend_today(fleet) >= self._fleet_cap:
            raise SpendCapExceeded(f"fleet '{fleet}' daily cap ${self._fleet_cap} reached — hard stop")
        if self._spend_today() >= self._global_cap:
            raise SpendCapExceeded(f"global daily cap ${self._global_cap} reached — hard stop")
        if tier == "frontier" and self.frontier_spend_month() >= self._frontier_month_cap:
            raise SpendCapExceeded(
                f"frontier monthly cap ${self._frontier_month_cap} reached — hard stop"
            )

        actor = provider or self._provider
        last_error: Exception | None = None
        result: LlmResult | None = None
        for attempt in range(max_retries):
            try:
                result = actor.complete(
                    model=model, prompt=prompt, system=system, max_tokens=max_tokens,
                    temperature=temperature, json_schema=json_schema,
                )
                if required_keys is not None:
                    parsed = parse_json_object(result.text)
                    missing = [k for k in required_keys if k not in parsed]
                    if missing:
                        raise StructuredOutputError(f"missing keys: {missing}")
                    result = replace(result, text=json.dumps(parsed))
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
                    task_id=task_id,
                    tier=tier,
                    rejected_alternatives=rejected_alternatives,
                    routing_reason=routing_reason,
                    quality_at_decision=quality_at_decision,
                )
            )
        return result
