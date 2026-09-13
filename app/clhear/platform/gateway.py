"""LLM provider abstraction, spend caps, call ledger (HLD v2 §3, I6).

The inference router (`router.run`) is the only production entry; this module
is the provider + ledger layer underneath it. The only production provider is
:class:`InferProvider` (Reg42 Infer → Bedrock). Every call is logged to
l0_platform.llm_calls; daily fleet/global caps and the monthly premium cap are
hard stops.
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
from app.clhear.platform.task_classes import (
    CLAUDE_OPUS,
    CLAUDE_SONNET,
    COHERE_EMBED_MULTI,
    DEEPSEEK_R1,
    GPT_OSS_120B,
    LLAMA_3_3_70B,
    MISTRAL_LARGE_3,
    NOVA_LITE,
    NOVA_PRO,
    PROCUREMENT_CLEAN_ORIGINS,
    QWEN_3_5_32B,
    TASK_CLASSES,
    TITAN_EMBED_V2,
    origin_of,
)
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
        task_class: str | None = None,
    ) -> LlmResult: ...


# USD per 1M tokens (input, output) — Bedrock on-demand list prices for the
# frozen model ids. Infer returns `usage.cost_usd` when it has the exact figure;
# this table is the ledger fallback so every call has a cost.
BEDROCK_PRICING: dict[str, tuple[float, float]] = {
    GPT_OSS_120B: (0.15, 0.60),
    MISTRAL_LARGE_3: (2.00, 6.00),
    CLAUDE_SONNET: (3.00, 15.00),
    CLAUDE_OPUS: (5.00, 25.00),
    NOVA_PRO: (0.80, 3.20),
    NOVA_LITE: (0.06, 0.24),
    LLAMA_3_3_70B: (0.72, 0.72),
    QWEN_3_5_32B: (0.15, 0.60),
    DEEPSEEK_R1: (1.35, 5.40),
    # embeddings: input tokens only (no generation)
    TITAN_EMBED_V2: (0.02, 0.0),
    COHERE_EMBED_MULTI: (0.10, 0.0),
}
# Rungs counted against the monthly premium cap (settings.clhear_frontier_monthly_cap_usd).
PREMIUM_MODELS: frozenset[str] = frozenset({CLAUDE_OPUS})
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
        # Reasoning models (gpt-oss) often follow the object with prose or a second
        # object ("Extra data"): decode the first complete object and drop the rest.
        start = raw.find("{")
        if start < 0:
            raise
        parsed, _ = json.JSONDecoder().raw_decode(raw, start)
    if not isinstance(parsed, dict):
        raise StructuredOutputError("response is not a JSON object")
    return parsed


def price_for(model: str) -> tuple[float, float]:
    return BEDROCK_PRICING.get(model, _DEFAULT_PRICING)


class InferError(RuntimeError):
    pass


class InferProvider:
    """Reg42 Infer — the only production provider (HLD v2 I6, §9).

    OpenAI-compatible wire (`/chat/completions`) fronting Bedrock inside the
    account. Every request carries the employee id, the CLHEAR task class and the
    data class; Infer applies the task-class ladder (fallback, procurement policy)
    and reports the model it actually used, which is what the ledger and the
    release model manifest record.
    """

    name = "infer"

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        employee_id: str | None = None,
        data_class: str | None = None,
        timeout: float = 180.0,
        client=None,
    ):
        settings = get_settings()
        self._base_url = (base_url or settings.infer_base_url or "").rstrip("/")
        self._token = token if token is not None else settings.infer_token
        if not self._base_url:
            raise RuntimeError("INFER_BASE_URL is not configured")
        if not self._token or self._token == "CHANGEME":
            raise RuntimeError("INFER_TOKEN is not configured")
        self.employee_id = employee_id or settings.infer_employee_id
        self.data_class = data_class or settings.infer_data_class
        self._timeout = timeout
        self._client = client

    def _headers(self, task_class: str | None) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {self._token}",
            "X-Employee-Id": self.employee_id,
            "X-Data-Class": self.data_class,
        }
        if task_class:
            headers["X-Task-Class"] = task_class
        return headers

    def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def complete(
        self,
        *,
        model: str,
        prompt: str,
        system: str | None,
        max_tokens: int,
        temperature: float = 0.0,
        json_schema: dict | None = None,
        task_class: str | None = None,
    ) -> LlmResult:
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
        if task_class:
            body["metadata"] = {"task_class": task_class, "employee_id": self.employee_id}
        resp = self._http().post(f"{self._base_url}/chat/completions", headers=self._headers(task_class), json=body)
        if resp.status_code >= 400:
            raise InferError(f"infer {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            text = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise InferError(f"malformed infer response: {str(data)[:200]}") from exc
        used_model = str(data.get("model") or model)
        self._assert_procurement_clean(task_class, used_model)
        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        if usage.get("cost_usd") is not None:
            cost = float(usage["cost_usd"])
        else:
            price_in, price_out = price_for(used_model)
            cost = (in_tok * price_in + out_tok * price_out) / 1_000_000
        return LlmResult(
            text=text, model=used_model, provider=self.name,
            input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost,
        )

    @staticmethod
    def _assert_procurement_clean(task_class: str | None, used_model: str) -> None:
        """I6: a derivation class must never be answered by a model outside the
        procurement-clean set. Infer routes by task class and may ignore the
        requested model, so the check is on what it reports having run; the
        call fails and nothing is written rather than silently accepting."""
        spec = TASK_CLASSES.get(task_class or "")
        if spec is None or not spec.derivation:
            return
        origin = origin_of(used_model)
        if origin not in PROCUREMENT_CLEAN_ORIGINS:
            raise InferError(
                f"procurement policy: derivation task {task_class!r} was answered by {used_model!r} "
                f"(origin {origin}); Infer must route clhear derivation classes to the clean ladder"
            )

    def route_explain(self, task_class: str) -> dict:
        """`GET /route/explain?task_class=` — the ladder Infer will apply and the
        rung it would select now. Used to freeze the release model manifest."""
        resp = self._http().get(
            f"{self._base_url}/route/explain",
            headers=self._headers(task_class),
            params={"task_class": task_class, "employee_id": self.employee_id},
        )
        if resp.status_code >= 400:
            raise InferError(f"infer route/explain {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if not isinstance(data, dict):
            raise InferError("route/explain did not return an object")
        return data

    def models(self) -> list[dict]:
        resp = self._http().get(f"{self._base_url}/models", headers=self._headers(None))
        if resp.status_code >= 400:
            raise InferError(f"infer models {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        return list(data.get("data") or []) if isinstance(data, dict) else list(data)


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
        task_class: str | None = None,
    ) -> LlmResult:
        self.calls += 1
        self.last_kwargs = {
            "model": model, "prompt": prompt, "system": system,
            "max_tokens": max_tokens, "temperature": temperature, "json_schema": json_schema,
            "task_class": task_class,
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

    def premium_spend_month(self) -> float:
        """Month-to-date spend on premium rungs (Opus-class) — the hard monthly cap."""
        query = sa.select(sa.func.coalesce(sa.func.sum(llm_calls.c.cost_usd), 0)).where(
            llm_calls.c.created_at >= _month_start_utc()
        ).where(sa.or_(llm_calls.c.model.in_(sorted(PREMIUM_MODELS)), llm_calls.c.tier == "frontier"))
        with self._engine.connect() as conn:
            return float(conn.execute(query).scalar_one())

    frontier_spend_month = premium_spend_month

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
        task_class: str | None = None,
    ) -> LlmResult:
        """One gated LLM call: cap check -> provider (retry/backoff) -> ledger.

        If required_keys is given the response must be a JSON object containing
        all of them (structured-output validation), retried within the budget.
        """
        if self._spend_today(fleet) >= self._fleet_cap:
            raise SpendCapExceeded(f"fleet '{fleet}' daily cap ${self._fleet_cap} reached — hard stop")
        if self._spend_today() >= self._global_cap:
            raise SpendCapExceeded(f"global daily cap ${self._global_cap} reached — hard stop")
        if (model in PREMIUM_MODELS or tier == "frontier") and self.premium_spend_month() >= self._frontier_month_cap:
            raise SpendCapExceeded(
                f"premium monthly cap ${self._frontier_month_cap} reached — hard stop"
            )

        actor = provider or self._provider
        last_error: Exception | None = None
        result: LlmResult | None = None
        for attempt in range(max_retries):
            try:
                extra = {"task_class": task_class} if task_class else {}
                result = actor.complete(
                    model=model, prompt=prompt, system=system, max_tokens=max_tokens,
                    temperature=temperature, json_schema=json_schema, **extra,
                )
                if required_keys is not None:
                    parsed = parse_json_object(result.text)
                    missing = [k for k in required_keys if k not in parsed]
                    if missing:
                        raise StructuredOutputError(f"missing keys: {missing}")
                    result = replace(result, text=json.dumps(parsed))
                break
            except (json.JSONDecodeError, StructuredOutputError, ConnectionError, TimeoutError, InferError) as exc:
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
