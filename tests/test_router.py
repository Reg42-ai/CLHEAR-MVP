"""Inference router: cheapest sufficient tier, caps, classification CPU-only."""
import json

import pytest
import sqlalchemy as sa

from app.clhear.models import llm_calls
from app.clhear.platform.gateway import FakeProvider, Gateway, SpendCapExceeded
from app.clhear.platform.router import Router, TASKS, complete, last_decisions, seed_quality


def _router(engine, quality=None, gpu_open=False, canned=None):
    fake = FakeProvider(canned_text=canned or json.dumps({"ok": True, "classification": "relevant", "confidence": 0.9}))
    providers = {"ollama": fake, "anthropic": fake, "fake": fake}
    return Router(engine, providers=providers, quality=quality, gpu_open=gpu_open), fake


def test_cheapest_sufficient_tier(engine):
    r, _ = _router(engine, gpu_open=False)
    d = r.decide("l2.duty_triage")
    assert d.chosen_tier == "local-small"
    assert d.chosen_model == "qwen3.5:4b"
    assert d.quality >= d.threshold
    assert "frontier skipped" in d.reason or d.chosen_tier != "frontier"


def test_classification_never_leaves_cpu(engine):
    # Even with small/mid below threshold and GPU up, classification must not escalate.
    quality = {
        ("l2.duty_triage", "qwen3.5:4b"): 0.40,
        ("l2.duty_triage", "qwen3.5:9b"): 0.40,
        ("l2.duty_triage", "qwen3.6:27b"): 0.99,
        ("l2.duty_triage", "claude-3-5-haiku-latest"): 0.99,
    }
    r, _ = _router(engine, quality=quality, gpu_open=True)
    d = r.decide("l2.duty_triage")
    assert d.chosen_tier in ("local-small", "local-mid")
    reasons = {x["tier"]: x["reason"] for x in d.rejected}
    assert "local-large" in reasons
    assert "classification" in reasons["local-large"].lower() or "CPU" in reasons["local-large"]
    assert "frontier" in reasons
    assert "classification" in reasons["frontier"].lower() or "CPU" in reasons["frontier"]


def test_frontier_only_when_high_criticality_and_local_below_threshold(engine):
    # Force every local model below the 0.90 revalidation threshold.
    quality = {
        ("l0.revalidate", "qwen3.5:4b"): 0.50,
        ("l0.revalidate", "qwen3.5:9b"): 0.50,
        ("l0.revalidate", "qwen3.6:27b"): 0.50,
        ("l0.revalidate", "claude-3-5-haiku-latest"): 0.95,
    }
    r, _ = _router(engine, quality=quality, gpu_open=True)
    d = r.decide("l0.revalidate")
    assert TASKS["l0.revalidate"].criticality == "high"
    assert d.chosen_tier == "frontier"
    assert d.chosen_model == "claude-3-5-haiku-latest"
    assert any("quality" in x["reason"] for x in d.rejected)


def test_medium_criticality_never_frontiers(engine):
    quality = {
        ("l2.consolidate", "qwen3.5:4b"): 0.10,
        ("l2.consolidate", "qwen3.5:9b"): 0.10,
        ("l2.consolidate", "qwen3.6:27b"): 0.10,
        ("l2.consolidate", "claude-3-5-haiku-latest"): 0.99,
    }
    r, _ = _router(engine, quality=quality, gpu_open=True)
    d = r.decide("l2.consolidate")
    assert d.chosen_tier != "frontier"
    assert any(x["tier"] == "frontier" and "criticality" in x["reason"] for x in d.rejected)


def test_gpu_tier_when_window_open(engine):
    quality = {
        ("l3.block_generate", "qwen3.5:4b"): 0.50,
        ("l3.block_generate", "qwen3.5:9b"): 0.50,
        ("l3.block_generate", "qwen3.6:27b"): 0.87,
        ("l3.block_generate", "claude-3-5-haiku-latest"): 0.99,
    }
    r, _ = _router(engine, quality=quality, gpu_open=True)
    d = r.decide("l3.block_generate")
    assert d.chosen_tier == "local-large"
    assert d.chosen_model == "qwen3.6:27b"


def test_gpu_tier_deferred_when_window_closed(engine):
    quality = {
        ("l3.block_generate", "qwen3.5:4b"): 0.50,
        ("l3.block_generate", "qwen3.5:9b"): 0.50,
        ("l3.block_generate", "qwen3.6:27b"): 0.87,
        ("l3.block_generate", "claude-3-5-haiku-latest"): 0.99,
    }
    r, _ = _router(engine, quality=quality, gpu_open=False)
    d = r.decide("l3.block_generate")
    assert d.chosen_tier != "local-large"
    assert any("GPU" in x["reason"] for x in d.rejected)


def test_run_logs_task_and_rejected_alternatives(engine):
    r, fake = _router(engine)
    result = r.run("dummy.triage", prompt="classify this", required_keys=["classification", "confidence"])
    assert result.provider == "fake"
    assert fake.calls == 1
    with engine.connect() as conn:
        row = conn.execute(sa.select(llm_calls)).one()
    assert row.task_id == "dummy.triage"
    assert row.tier == "local-small"
    assert row.routing_reason
    assert isinstance(row.rejected_alternatives, list) or row.rejected_alternatives is None
    ledger = last_decisions(engine)
    assert ledger[0]["task_id"] == "dummy.triage"


def test_frontier_monthly_cap_is_hard_stop(engine):
    quality = {
        ("l0.revalidate", "qwen3.5:4b"): 0.1,
        ("l0.revalidate", "qwen3.5:9b"): 0.1,
        ("l0.revalidate", "qwen3.6:27b"): 0.1,
        ("l0.revalidate", "claude-3-5-haiku-latest"): 0.99,
    }
    with engine.begin() as conn:
        conn.execute(
            llm_calls.insert().values(
                fleet="prior", provider="anthropic", model="claude-3-5-haiku-latest",
                prompt_hash="x" * 64, input_tokens=1, output_tokens=1, cost_usd=50.0,
                task_id="l0.revalidate", tier="frontier",
            )
        )
    r, _ = _router(engine, quality=quality, gpu_open=True)
    d = r.decide("l0.revalidate")
    assert d.chosen_tier != "frontier"
    assert any("cap" in x["reason"] for x in d.rejected)


def test_complete_helper_accepts_legacy_gateway(engine):
    gw = Gateway(engine, FakeProvider())
    result = complete(gw, "dummy.triage", prompt="hello", required_keys=["classification", "confidence"])
    assert result.text


def test_seed_quality_is_idempotent(engine):
    assert seed_quality(engine) > 0
    assert seed_quality(engine) == 0


def test_unknown_task_raises(engine):
    r, _ = _router(engine)
    with pytest.raises(KeyError):
        r.decide("not.a.task")


def test_local_mid_is_9b_not_14b():
    from app.clhear.platform.router import TIERS, SEED_QUALITY, tiers_public

    assert TIERS["local-mid"].model == "qwen3.5:9b"
    assert all("14b" not in model for _task, model in SEED_QUALITY)
    mid = next(t for t in tiers_public() if t["id"] == "local-mid")
    assert mid["model"] == "qwen3.5:9b"


def test_build_providers_fake_only_when_requested(monkeypatch):
    from app.clhear.platform.router import build_providers
    from app.clhear.settings import get_settings

    monkeypatch.setenv("CLHEAR_LLM_PROVIDER", "fake")
    get_settings.cache_clear()
    providers = build_providers()
    assert providers["ollama"].name == "fake"
    assert providers["anthropic"].name == "fake"
    get_settings.cache_clear()


def test_build_providers_no_silent_fake(engine, monkeypatch):
    from app.clhear.platform.router import NO_PROVIDER_REASON, Router, build_providers
    from app.clhear.settings import get_settings

    monkeypatch.setenv("CLHEAR_LLM_PROVIDER", "")
    monkeypatch.setenv("OLLAMA_BASE_URL", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "CHANGEME")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("XAI_API_KEY", "")
    get_settings.cache_clear()
    assert build_providers() == {}
    r = Router(engine, providers={})
    with pytest.raises(SpendCapExceeded):
        r.decide("l2.duty_triage")
    assert "FakeProvider" in NO_PROVIDER_REASON or "fake" in NO_PROVIDER_REASON.lower()
    get_settings.cache_clear()
