"""Inference router: cheapest sufficient rung of the task-class ladder, caps,
procurement-clean derivation, Infer-only providers (HLD v2 I6, §9)."""
import json

import pytest
import sqlalchemy as sa

from app.clhear.models import llm_calls
from app.clhear.platform import task_classes as tc
from app.clhear.platform.gateway import PREMIUM_MODELS, FakeProvider, Gateway, SpendCapExceeded
from app.clhear.platform.router import (
    TASKS,
    ProcurementViolation,
    Router,
    complete,
    last_decisions,
    seed_quality,
    tiers_public,
)


def _router(engine, quality=None, canned=None, model_manifest=None):
    fake = FakeProvider(canned_text=canned or json.dumps({"ok": True, "classification": "relevant", "confidence": 0.9}))
    return Router(engine, providers={"fake": fake}, quality=quality, model_manifest=model_manifest), fake


def test_every_task_has_a_registered_task_class():
    for spec in TASKS.values():
        assert spec.task_class in tc.TASK_CLASSES, spec.id


def test_cheapest_sufficient_rung(engine):
    r, _ = _router(engine)
    d = r.decide("l2.duty_triage")
    ladder = tc.default_ladder("l2_extract")
    assert d.chosen_tier == "l2_extract"
    assert d.chosen_model == ladder[0]
    assert d.quality >= d.threshold
    assert "cheaper sufficient rung" in d.reason
    assert d.ladder == ladder and d.ladder_source == "default"


def test_steps_up_the_ladder_when_quality_is_below_threshold(engine):
    ladder = tc.default_ladder("l2_extract")
    quality = {("l2.extract", ladder[0]): 0.40, ("l2.extract", ladder[1]): 0.95}
    r, _ = _router(engine, quality=quality)
    d = r.decide("l2.extract")
    assert d.chosen_model == ladder[1]
    assert d.rejected[0]["model"] == ladder[0]
    assert "quality" in d.rejected[0]["reason"]


def test_premium_rung_only_for_high_criticality(engine):
    ladder = tc.default_ladder("l6_explain")
    assert tc.CLAUDE_OPUS_5 in ladder and TASKS["l6.rationale"].criticality == "medium"
    quality = {("l6.rationale", m): 0.10 for m in ladder if m not in PREMIUM_MODELS}
    quality[("l6.rationale", tc.CLAUDE_OPUS_5)] = 0.99
    r, _ = _router(engine, quality=quality)
    d = r.decide("l6.rationale")
    assert d.chosen_model not in PREMIUM_MODELS
    assert any(x["model"] == tc.CLAUDE_OPUS_5 and "criticality" in x["reason"] for x in d.rejected)
    assert "most capable eligible rung" in d.reason


def test_premium_rung_for_high_criticality_when_lower_rungs_fail(engine):
    ladder = tc.default_ladder("judge")
    quality = {("l0.revalidate", m): 0.10 for m in ladder}
    r, _ = _router(engine, quality=quality, model_manifest={
        "task_classes": {"judge": {"model_id": tc.NOVA_LITE, "ladder": [tc.NOVA_LITE, tc.CLAUDE_OPUS_5]}}
    })
    quality[("l0.revalidate", tc.CLAUDE_OPUS_5)] = 0.97
    d = r.decide("l0.revalidate")
    assert d.chosen_model == tc.CLAUDE_OPUS_5
    assert d.ladder_source == "frozen"


def test_premium_monthly_cap_is_hard_stop(engine):
    with engine.begin() as conn:
        conn.execute(
            llm_calls.insert().values(
                fleet="prior", provider="infer", model=tc.CLAUDE_OPUS_5,
                prompt_hash="x" * 64, input_tokens=1, output_tokens=1, cost_usd=50.0,
                task_id="l0.revalidate", tier="judge",
            )
        )
    quality = {("l0.revalidate", tc.NOVA_LITE): 0.10, ("l0.revalidate", tc.CLAUDE_OPUS_5): 0.99}
    r, _ = _router(engine, quality=quality, model_manifest={
        "task_classes": {"judge": {"model_id": tc.NOVA_LITE, "ladder": [tc.NOVA_LITE, tc.CLAUDE_OPUS_5]}}
    })
    d = r.decide("l0.revalidate")
    assert d.chosen_model != tc.CLAUDE_OPUS_5
    assert any("cap" in x["reason"] for x in d.rejected)


def test_derivation_class_refuses_non_procurement_clean_ladder(engine):
    r, _ = _router(engine, model_manifest={
        "task_classes": {"l2_extract": {"model_id": tc.QWEN_3_5_32B, "ladder": [tc.QWEN_3_5_32B, tc.GPT_OSS_120B]}}
    })
    with pytest.raises(ProcurementViolation):
        r.decide("l2.extract")
    # non-derivation classes may use any Bedrock model a policy allows
    r2, _ = _router(engine, model_manifest={
        "task_classes": {"judge": {"model_id": tc.QWEN_3_5_32B, "ladder": [tc.QWEN_3_5_32B]}}
    })
    assert r2.decide("eval.judge").chosen_model == tc.QWEN_3_5_32B


def test_frozen_manifest_pins_the_rung(engine):
    from app.clhear.platform.manifest import build_model_manifest

    frozen = build_model_manifest(release_id="2026.09.28", resolved={"l3_decompose": tc.CLAUDE_OPUS_5})
    # l3.block_generate is medium criticality, so the premium frozen rung is rejected and
    # the router falls to the next frozen rung rather than the default ladder head.
    r, _ = _router(engine, model_manifest=frozen)
    d = r.decide("l3.block_generate")
    assert d.ladder_source == "frozen"
    assert d.ladder[0] == tc.CLAUDE_OPUS_5
    assert d.chosen_model in tc.default_ladder("l3_decompose") and d.chosen_model != tc.CLAUDE_OPUS_5


def test_run_logs_task_class_and_rejected_alternatives(engine):
    r, fake = _router(engine)
    result = r.run("dummy.triage", prompt="classify this", required_keys=["classification", "confidence"])
    assert result.provider == "fake"
    assert fake.calls == 1
    assert fake.last_kwargs["task_class"] == "judge"
    with engine.connect() as conn:
        row = conn.execute(sa.select(llm_calls)).one()
    assert row.task_id == "dummy.triage"
    assert row.tier == "judge"
    assert row.routing_reason
    assert isinstance(row.rejected_alternatives, list) or row.rejected_alternatives is None
    ledger = last_decisions(engine)
    assert ledger[0]["task_id"] == "dummy.triage" and ledger[0]["task_class"] == "judge"


def test_explain_contract(engine):
    r, _ = _router(engine)
    ex = r.explain("l2.consolidate")
    assert ex["task_class"] == "l2_consolidate" and ex["derivation_class"] is True
    assert ex["selected"] in ex["ladder"]
    assert ex["procurement_clean"] is True and ex["origin"] in ("US", "EU")
    assert set(ex) >= {"ladder", "ladder_source", "rejected", "reason", "quality", "threshold", "provider"}


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


def test_tiers_public_lists_task_classes():
    rows = {t["id"]: t for t in tiers_public()}
    assert set(rows) == set(tc.TASK_CLASSES)
    assert all(t["provider"] == "infer" and t["hosting"] == "aws-bedrock" for t in rows.values())
    for name in tc.DERIVATION_CLASSES:
        assert set(rows[name]["origins"]) <= {"US", "EU"}


def test_build_providers_fake_only_when_requested(monkeypatch):
    from app.clhear.platform.router import build_providers
    from app.clhear.settings import get_settings

    monkeypatch.setenv("CLHEAR_LLM_PROVIDER", "fake")
    get_settings.cache_clear()
    providers = build_providers()
    assert set(providers) == {"fake"}
    get_settings.cache_clear()


def test_build_providers_no_silent_fake(engine, monkeypatch):
    from app.clhear.platform.router import NO_PROVIDER_REASON, Router, build_providers
    from app.clhear.settings import get_settings

    monkeypatch.setenv("CLHEAR_LLM_PROVIDER", "")
    monkeypatch.setenv("INFER_BASE_URL", "")
    monkeypatch.setenv("INFER_TOKEN", "CHANGEME")
    get_settings.cache_clear()
    assert build_providers() == {}
    r = Router(engine, providers={})
    with pytest.raises(SpendCapExceeded):
        r.decide("l2.duty_triage")
    assert "INFER" in NO_PROVIDER_REASON
    get_settings.cache_clear()


def test_build_providers_infer_only(monkeypatch):
    from app.clhear.platform.router import build_providers
    from app.clhear.settings import get_settings

    monkeypatch.setenv("CLHEAR_LLM_PROVIDER", "")
    monkeypatch.setenv("INFER_BASE_URL", "https://infer.reg42.ai/v1")
    monkeypatch.setenv("INFER_TOKEN", "tok-test")
    # Vendor keys are not settings any more; even if present in the env they are ignored.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-used")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    get_settings.cache_clear()
    providers = build_providers()
    assert set(providers) == {"infer"}
    assert providers["infer"].name == "infer"
    assert providers["infer"]._base_url == "https://infer.reg42.ai/v1"
    get_settings.cache_clear()
