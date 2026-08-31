"""Inference Optimization Engine — the only LLM entry point.

Every production call is `router.run(task_id, …)`. The router picks the
cheapest tier whose measured quality meets the task threshold, logs the
decision (chosen model, rejected alternatives, reason, cost), and only then
invokes the gateway.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.models import llm_calls, router_quality
from app.clhear.platform.gateway import (
    FakeProvider,
    Gateway,
    LlmResult,
    Provider,
    SpendCapExceeded,
)
from app.clhear.settings import get_settings

log = logging.getLogger("clhear.router")

SHAPES = (
    "classification",
    "extraction",
    "structured_drafting",
    "long_reasoning",
    "doc_analysis",
    "graph_mapping",
    "judging",
)

TIER_ORDER = ("local-small", "local-mid", "local-large", "frontier")


@dataclass(frozen=True)
class ModelTier:
    id: str
    model: str
    provider: str  # ollama | frontier
    requires_gpu: bool = False
    cpu_ok: bool = True
    nightly_only: bool = False


TIERS: dict[str, ModelTier] = {
    "local-small": ModelTier("local-small", "qwen3.5:4b", "ollama", requires_gpu=False, cpu_ok=True),
    "local-mid": ModelTier("local-mid", "qwen3.5:14b", "ollama", requires_gpu=False, cpu_ok=True),
    "local-large": ModelTier("local-large", "qwen3.6:27b", "ollama", requires_gpu=True, cpu_ok=False, nightly_only=True),
    "frontier": ModelTier("frontier", "claude-3-5-haiku-latest", "frontier", requires_gpu=False, cpu_ok=True),
}

FRONTIER_MODELS = {
    "anthropic": "claude-3-5-haiku-latest",
    "openai": "gpt-4o-mini",
    "xai": "grok-3-mini",
}


@dataclass(frozen=True)
class TaskSpec:
    id: str
    shape: str
    complexity: str  # low | medium | high
    criticality: str  # low | medium | high
    quality_threshold: float
    fleet: str
    layer: str
    latency_tolerance: str = "nightly"  # interactive | hours | nightly
    domain: str = ""
    context_size: str = "small"
    nightly_volume: int = 0
    determinism: dict = field(default_factory=lambda: {"temperature": 0.0})
    description: str = ""


TASKS: dict[str, TaskSpec] = {
    "dummy.triage": TaskSpec(
        "dummy.triage", "classification", "low", "low", 0.80, "dummy", "L0",
        latency_tolerance="interactive", description="P0 rehearsal classification",
    ),
    "l1.parse_repair": TaskSpec(
        "l1.parse_repair", "extraction", "medium", "high", 0.90, "l1.repair", "L1",
        latency_tolerance="hours", domain="web-structure",
        description="Extractive parse hints — output must byte-match publisher text",
    ),
    "l1.annotate": TaskSpec(
        "l1.annotate", "structured_drafting", "low", "low", 0.75, "l1.annotate", "L1",
        latency_tolerance="nightly", description="Grounded clause annotation; origin=llm",
    ),
    "l2.duty_triage": TaskSpec(
        "l2.duty_triage", "classification", "low", "medium", 0.85, "l2.triage", "L2",
        latency_tolerance="nightly", domain="legal",
        description="Weak-modality duty verdict + evidence-span contract",
    ),
    "l2.consolidate": TaskSpec(
        "l2.consolidate", "structured_drafting", "medium", "medium", 0.85, "l2.consolidate", "L2",
        latency_tolerance="nightly", domain="legal",
        description="Cross-jurisdiction concept draft; closed-world OBL: members",
    ),
    "l3.block_generate": TaskSpec(
        "l3.block_generate", "structured_drafting", "high", "medium", 0.82, "l3.generate", "L3",
        latency_tolerance="nightly", domain="legal",
        description="Building-block synthesis grounded on live obligation ids",
    ),
    "l4.license_extract": TaskSpec(
        "l4.license_extract", "extraction", "medium", "high", 0.92, "l4.licenses", "L4",
        latency_tolerance="nightly", domain="legal", context_size="large",
        description="Extract license types only from retrieved clause text",
    ),
    "l5.activity_map": TaskSpec(
        "l5.activity_map", "graph_mapping", "medium", "medium", 0.84, "l5.map", "L5",
        latency_tolerance="nightly", domain="legal",
        description="Closed-world obligation↔activity mapping",
    ),
    "l6.rationale": TaskSpec(
        "l6.rationale", "long_reasoning", "medium", "medium", 0.80, "l6.narrate", "L6",
        latency_tolerance="interactive", description="Citation-checked program rationale",
    ),
    "l7.narrative": TaskSpec(
        "l7.narrative", "long_reasoning", "medium", "medium", 0.82, "l7.narrate", "L7",
        latency_tolerance="nightly", domain="risk-quant",
        description="Number-echo risk commentary over formula outputs",
    ),
    "l0.revalidate": TaskSpec(
        "l0.revalidate", "judging", "high", "high", 0.90, "l0.referee", "L0",
        latency_tolerance="hours", domain="legal",
        description="Correction revalidation judge — frontier-eligible",
    ),
    "eval.judge": TaskSpec(
        "eval.judge", "judging", "medium", "medium", 0.85, "eval.studio", "L0",
        latency_tolerance="interactive", description="Eval Studio disagreement judge",
    ),
}

# Seeded from published-style benches; Eval Studio overwrites with agreement scores.
SEED_QUALITY: dict[tuple[str, str], float] = {
    ("dummy.triage", "qwen3.5:4b"): 0.93,
    ("dummy.triage", "qwen3.5:14b"): 0.95,
    ("l1.parse_repair", "qwen3.5:14b"): 0.88,
    ("l1.parse_repair", "qwen3.6:27b"): 0.93,
    ("l1.parse_repair", "claude-3-5-haiku-latest"): 0.96,
    ("l1.annotate", "qwen3.5:4b"): 0.80,
    ("l1.annotate", "qwen3.5:14b"): 0.88,
    ("l2.duty_triage", "qwen3.5:4b"): 0.88,
    ("l2.duty_triage", "qwen3.5:14b"): 0.92,
    ("l2.consolidate", "qwen3.5:14b"): 0.86,
    ("l2.consolidate", "qwen3.6:27b"): 0.89,
    ("l2.consolidate", "claude-3-5-haiku-latest"): 0.94,
    ("l3.block_generate", "qwen3.5:14b"): 0.80,
    ("l3.block_generate", "qwen3.6:27b"): 0.87,
    ("l3.block_generate", "claude-3-5-haiku-latest"): 0.93,
    ("l4.license_extract", "qwen3.5:14b"): 0.84,
    ("l4.license_extract", "qwen3.6:27b"): 0.91,
    ("l4.license_extract", "claude-3-5-haiku-latest"): 0.96,
    ("l5.activity_map", "qwen3.5:14b"): 0.85,
    ("l5.activity_map", "qwen3.6:27b"): 0.90,
    ("l6.rationale", "qwen3.5:14b"): 0.82,
    ("l6.rationale", "qwen3.6:27b"): 0.88,
    ("l7.narrative", "qwen3.5:14b"): 0.83,
    ("l7.narrative", "qwen3.6:27b"): 0.89,
    ("l0.revalidate", "qwen3.6:27b"): 0.86,
    ("l0.revalidate", "claude-3-5-haiku-latest"): 0.95,
    ("eval.judge", "qwen3.5:14b"): 0.84,
    ("eval.judge", "claude-3-5-haiku-latest"): 0.94,
}

TIER_DEFAULT_QUALITY = {
    "local-small": 0.70,
    "local-mid": 0.82,
    "local-large": 0.89,
    "frontier": 0.95,
}


@dataclass
class RoutingDecision:
    task_id: str
    chosen_tier: str
    chosen_model: str
    provider_name: str
    quality: float
    threshold: float
    rejected: list[dict]
    reason: str
    deferred: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def build_providers(settings=None) -> dict[str, Provider]:
    """Construct whatever providers the environment actually has."""
    settings = settings or get_settings()
    out: dict[str, Provider] = {}
    if (settings.clhear_llm_provider or "").lower() == "fake":
        fake = FakeProvider()
        return {"ollama": fake, "fake": fake, "anthropic": fake, "openai": fake, "xai": fake}
    if settings.ollama_base_url:
        from app.clhear.platform.gateway import OllamaProvider

        out["ollama"] = OllamaProvider(settings.ollama_base_url)
    if settings.anthropic_api_key and settings.anthropic_api_key != "CHANGEME":
        from app.clhear.platform.gateway import AnthropicProvider

        try:
            out["anthropic"] = AnthropicProvider(settings.anthropic_api_key)
        except Exception:
            log.exception("anthropic provider unavailable")
    if settings.openai_api_key and settings.openai_api_key != "CHANGEME":
        from app.clhear.platform.gateway import OpenAIProvider

        try:
            out["openai"] = OpenAIProvider(settings.openai_api_key)
        except Exception:
            log.exception("openai provider unavailable")
    if settings.xai_api_key and settings.xai_api_key != "CHANGEME":
        from app.clhear.platform.gateway import XAIProvider

        try:
            out["xai"] = XAIProvider(settings.xai_api_key)
        except Exception:
            log.exception("xai provider unavailable")
    if not out:
        fake = FakeProvider()
        out = {"fake": fake, "ollama": fake}
    return out


def seed_quality(engine: Engine) -> int:
    """Idempotent seed of the quality table from SEED_QUALITY."""
    written = 0
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        for (task_id, model), quality in SEED_QUALITY.items():
            exists = conn.execute(
                sa.select(router_quality.c.task_id)
                .where(router_quality.c.task_id == task_id)
                .where(router_quality.c.model == model)
            ).first()
            if exists:
                continue
            conn.execute(
                router_quality.insert().values(
                    task_id=task_id, model=model, quality=quality, n_samples=0,
                    source="seed", updated_at=now,
                )
            )
            written += 1
    return written


def upsert_quality(engine: Engine, task_id: str, model: str, quality: float, n_samples: int, source: str = "eval_studio") -> None:
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        exists = conn.execute(
            sa.select(router_quality.c.task_id)
            .where(router_quality.c.task_id == task_id)
            .where(router_quality.c.model == model)
        ).first()
        if exists:
            conn.execute(
                router_quality.update()
                .where(router_quality.c.task_id == task_id)
                .where(router_quality.c.model == model)
                .values(quality=quality, n_samples=n_samples, source=source, updated_at=now)
            )
        else:
            conn.execute(
                router_quality.insert().values(
                    task_id=task_id, model=model, quality=quality, n_samples=n_samples,
                    source=source, updated_at=now,
                )
            )


def quality_table(engine: Engine) -> dict[tuple[str, str], float]:
    seed_quality(engine)
    out = dict(SEED_QUALITY)
    with engine.connect() as conn:
        for row in conn.execute(sa.select(router_quality)):
            out[(row.task_id, row.model)] = float(row.quality)
    return out


class Router:
    """Cheapest sufficient model. Classification never leaves CPU. Frontier is
    high-criticality + local-below-threshold + monthly budget remaining."""

    def __init__(
        self,
        engine: Engine,
        providers: dict[str, Provider] | None = None,
        quality: dict[tuple[str, str], float] | None = None,
        gpu_open: bool | None = None,
        gateway: Gateway | None = None,
    ):
        self.engine = engine
        self.providers = providers if providers is not None else build_providers()
        # A ledger gateway; per-call provider is overridden.
        lead = next(iter(self.providers.values()))
        self.gateway = gateway or Gateway(engine, lead)
        self._quality_override = quality
        self._gpu_open = gpu_open

    def _quality(self, task_id: str, model: str, tier_id: str) -> float:
        table = self._quality_override if self._quality_override is not None else quality_table(self.engine)
        if (task_id, model) in table:
            return table[(task_id, model)]
        return TIER_DEFAULT_QUALITY.get(tier_id, 0.70)

    def _gpu_available(self) -> bool:
        if self._gpu_open is not None:
            return self._gpu_open
        try:
            from app.clhear.platform.gpu import is_gpu_open

            return is_gpu_open(self.engine)
        except Exception:
            return False

    def _frontier_provider(self) -> tuple[str, Provider, str] | None:
        for name in ("anthropic", "openai", "xai"):
            if name in self.providers:
                return name, self.providers[name], FRONTIER_MODELS[name]
        return None

    def _tier_available(self, tier: ModelTier, task: TaskSpec) -> tuple[bool, str]:
        if task.shape == "classification" and (tier.requires_gpu or tier.id == "frontier"):
            return False, "classification never leaves CPU"
        if tier.id == "frontier":
            if task.criticality != "high":
                return False, f"criticality {task.criticality} — frontier reserved for high"
            if self._frontier_provider() is None:
                return False, "no frontier provider configured"
            try:
                spent = self.gateway.frontier_spend_month()
            except Exception:
                spent = 0.0
            cap = get_settings().clhear_frontier_monthly_cap_usd
            if spent >= cap:
                return False, f"frontier monthly cap ${cap} exhausted (${spent:.2f} spent)"
            return True, "frontier eligible"
        if "ollama" not in self.providers and "fake" not in self.providers:
            return False, "no local provider"
        if tier.requires_gpu and not self._gpu_available():
            if task.latency_tolerance == "nightly":
                return False, "GPU window closed — queued for nightly"
            return False, "GPU window closed — step down"
        return True, "local available"

    def decide(self, task_id: str) -> RoutingDecision:
        if task_id not in TASKS:
            raise KeyError(f"unknown task {task_id}")
        task = TASKS[task_id]
        rejected: list[dict] = []
        chosen: RoutingDecision | None = None
        deferred = False
        for tier_id in TIER_ORDER:
            tier = TIERS[tier_id]
            ok, why = self._tier_available(tier, task)
            if tier_id == "frontier":
                model = FRONTIER_MODELS["anthropic"]
                provider_name = "frontier"
                if self._frontier_provider():
                    provider_name, _, model = self._frontier_provider()
            else:
                model = tier.model
                provider_name = "ollama" if "ollama" in self.providers else next(iter(self.providers))
            quality = self._quality(task_id, model, tier_id)
            if not ok:
                rejected.append({"tier": tier_id, "model": model, "quality": quality, "reason": why})
                if "queued for nightly" in why:
                    deferred = True
                continue
            if quality + 1e-9 < task.quality_threshold:
                rejected.append({
                    "tier": tier_id, "model": model, "quality": quality,
                    "reason": f"quality {quality:.2f} < {task.quality_threshold:.2f} threshold",
                })
                continue
            reason = (
                f"{task.id} → {model} ({tier_id}): measured quality {quality:.2f} ≥ "
                f"{task.quality_threshold:.2f} threshold"
            )
            if tier_id != "frontier":
                reason += "; frontier skipped (cheaper sufficient tier)"
            chosen = RoutingDecision(
                task_id=task_id, chosen_tier=tier_id, chosen_model=model,
                provider_name=provider_name, quality=quality, threshold=task.quality_threshold,
                rejected=rejected, reason=reason, deferred=False,
            )
            break
        if chosen is None:
            # Fall back to the best available local even if below threshold (never silent fail).
            for tier_id in TIER_ORDER:
                tier = TIERS[tier_id]
                ok, why = self._tier_available(tier, task)
                if not ok:
                    continue
                model = tier.model if tier_id != "frontier" else (
                    self._frontier_provider()[2] if self._frontier_provider() else tier.model
                )
                provider_name = (
                    self._frontier_provider()[0] if tier_id == "frontier" and self._frontier_provider()
                    else ("ollama" if "ollama" in self.providers else next(iter(self.providers)))
                )
                quality = self._quality(task_id, model, tier_id)
                return RoutingDecision(
                    task_id=task_id, chosen_tier=tier_id, chosen_model=model,
                    provider_name=provider_name, quality=quality, threshold=task.quality_threshold,
                    rejected=rejected,
                    reason=f"{task.id} → {model} ({tier_id}): no tier met threshold; using best available (quality {quality:.2f})",
                    deferred=deferred,
                )
            raise SpendCapExceeded(f"no provider available for task {task_id}")
        return chosen

    def run(
        self,
        task_id: str,
        *,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 1024,
        required_keys: list[str] | None = None,
        json_schema: dict | None = None,
        max_retries: int = 3,
    ) -> LlmResult:
        task = TASKS[task_id]
        decision = self.decide(task_id)
        provider = self.providers.get(decision.provider_name) or self.providers.get("ollama") or next(iter(self.providers.values()))
        temperature = float((task.determinism or {}).get("temperature", 0.0))
        try:
            from app.clhear import ai_ops

            ai_ops.record(
                self.engine,
                kind="router_decision",
                layer=task.layer,
                fleet=task.fleet,
                reasoning=decision.reason,
                detail=decision.as_dict(),
            )
        except Exception:
            log.exception("ai_ops router_decision write failed")
        return self.gateway.call(
            fleet=task.fleet,
            model=decision.chosen_model,
            prompt=prompt,
            system=system,
            max_tokens=max_tokens,
            required_keys=required_keys,
            max_retries=max_retries,
            temperature=temperature,
            json_schema=json_schema,
            provider=provider,
            task_id=task_id,
            tier=decision.chosen_tier,
            rejected_alternatives=decision.rejected,
            routing_reason=decision.reason,
            quality_at_decision=decision.quality,
        )


def is_router(llm: Any) -> bool:
    return isinstance(llm, Router)


def complete(llm: Any, task_id: str, **kwargs) -> LlmResult:
    """Call through the router when we have one; Gateway.call for legacy tests."""
    if is_router(llm):
        return llm.run(task_id, **kwargs)
    task = TASKS.get(task_id)
    fleet = task.fleet if task else kwargs.pop("fleet", "unknown")
    model = kwargs.pop("model", None) or (TIERS["local-small"].model if task else "claude-3-5-haiku-latest")
    return llm.call(fleet=fleet, model=model, **kwargs)


def last_decisions(engine: Engine, limit: int = 40) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(llm_calls).where(llm_calls.c.task_id.isnot(None)).order_by(llm_calls.c.id.desc()).limit(limit)
        ).all()
    out = []
    for row in rows:
        out.append({
            "task_id": row.task_id,
            "model": row.model,
            "tier": row.tier,
            "provider": row.provider,
            "cost_usd": float(row.cost_usd),
            "routing_reason": row.routing_reason,
            "rejected_alternatives": row.rejected_alternatives if isinstance(row.rejected_alternatives, list) else [],
            "quality": float(row.quality_at_decision) if row.quality_at_decision is not None else None,
            "created_at": str(row.created_at),
        })
    return out


def registry_public() -> list[dict]:
    return [
        {
            "id": t.id, "shape": t.shape, "complexity": t.complexity, "criticality": t.criticality,
            "quality_threshold": t.quality_threshold, "fleet": t.fleet, "layer": t.layer,
            "latency_tolerance": t.latency_tolerance, "domain": t.domain, "description": t.description,
        }
        for t in TASKS.values()
    ]
