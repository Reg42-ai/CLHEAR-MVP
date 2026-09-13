"""Route/explain contract between CLHEAR and Reg42 Infer (HLD v2 §8 item 2).

* the handoff YAML is byte-identical to the code that generates it;
* every derivation class ladder is procurement-clean (§9);
* freezing a manifest from Infer's route/explain records exactly what Infer reports;
* a Router bound to that frozen manifest routes on the frozen ladder.
"""
from pathlib import Path

import pytest

from app.clhear.platform import manifest as mm
from app.clhear.platform import task_classes as tc
from app.clhear.platform.gateway import FakeProvider
from app.clhear.platform.router import Router

REPO = Path(__file__).resolve().parents[1]
HANDOFF = REPO / "handoff" / "reg42-infra" / "tasks.clhear.yaml"


def test_handoff_yaml_is_generated_from_code():
    assert HANDOFF.read_text(encoding="utf-8") == tc.to_infer_yaml()


def test_handoff_yaml_parses_and_is_clean():
    import yaml

    doc = yaml.safe_load(HANDOFF.read_text(encoding="utf-8"))
    assert set(doc["tasks"]) == set(tc.REQUIRED_TASK_CLASSES)
    assert doc["policy"]["hosting"] == "aws-bedrock"
    assert "CN" in doc["policy"]["derivation_classes_forbid_origins"]
    for name, spec in doc["tasks"].items():
        if spec["derivation"]:
            for rung in spec["ladder"]:
                assert tc.origin_of(rung) in ("US", "EU"), (name, rung)


def test_no_cn_origin_in_derivation_classes():
    assert tc.validate_ladders({name: tc.default_ladder(name) for name in tc.REQUIRED_TASK_CLASSES}) == []
    bad = {name: tc.default_ladder(name) for name in tc.REQUIRED_TASK_CLASSES}
    bad["l2_extract"] = [tc.DEEPSEEK_R1]
    assert any("not allowed in a derivation class" in p for p in tc.validate_ladders(bad))


def test_freeze_from_infer_records_reported_ladders(monkeypatch):
    from app.clhear.settings import get_settings

    class _Infer:
        def __init__(self, *a, **k):
            pass

        def route_explain(self, task_class):
            ladder = tc.default_ladder(task_class)
            return {"task_class": task_class, "ladder": ladder, "selected": ladder[-1] if task_class == "l2_extract" else ladder[0]}

    monkeypatch.setenv("INFER_BASE_URL", "https://infer.reg42.ai/v1")
    monkeypatch.setenv("INFER_TOKEN", "tok")
    get_settings.cache_clear()
    monkeypatch.setattr("app.clhear.platform.gateway.InferProvider", _Infer)
    frozen = mm.freeze_from_infer("2026.09.28")
    get_settings.cache_clear()
    assert frozen["source"] == "infer"
    assert frozen["task_classes"]["l2_extract"]["model_id"] == tc.default_ladder("l2_extract")[-1]
    assert mm.check_manifest(frozen) == []


def test_freeze_without_infer_uses_defaults(monkeypatch):
    from app.clhear.settings import get_settings

    monkeypatch.setenv("INFER_BASE_URL", "")
    monkeypatch.setenv("INFER_TOKEN", "")
    get_settings.cache_clear()
    frozen = mm.freeze_from_infer("2026.09.28")
    get_settings.cache_clear()
    assert frozen["source"] == "defaults"
    assert mm.check_manifest(frozen) == []


def test_router_replays_frozen_manifest(engine):
    frozen = mm.build_model_manifest(release_id="2026.09.28", resolved={"l2_extract": tc.MISTRAL_LARGE_3})
    r = Router(engine, providers={"fake": FakeProvider()}, model_manifest=frozen)
    ex = r.explain("l2.extract")
    assert ex["ladder_source"] == "frozen"
    assert ex["ladder"][0] == tc.MISTRAL_LARGE_3
    assert ex["selected"] == tc.MISTRAL_LARGE_3


def test_frozen_manifest_with_cn_derivation_is_rejected():
    frozen = mm.build_model_manifest(release_id="2026.09.28", ladders={"l3_decompose": [tc.QWEN_3_5_32B, tc.CLAUDE_SONNET]})
    problems = mm.check_manifest(frozen)
    assert any("l3_decompose" in p and "non-procurement-clean" in p for p in problems)
