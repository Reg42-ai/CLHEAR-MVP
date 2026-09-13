"""HLD v2 §3 release pipeline: semantic-date ids, spec v2 manifest, frozen model
manifest, gate-driven layer list, verify_release passes end to end."""
import json
import sys
from pathlib import Path

import pytest

from app.clhear import releases
from app.clhear.platform import manifest as mm
from app.clhear.platform import task_classes as tc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_release  # noqa: E402


def test_release_id_is_semantic_date():
    rid = releases.release_id_for()
    assert releases.RELEASE_ID_RE.match(rid)
    assert len(rid) == 10 and rid[4] == "." and rid[7] == "."
    assert releases.is_release_id("2026.09.28")
    assert releases.is_release_id("clhear-v20260101")  # legacy ids stay pinnable
    assert not releases.is_release_id("v1")


def test_model_manifest_complete_and_clean():
    m = mm.build_model_manifest(release_id="2026.09.28")
    assert mm.check_manifest(m) == []
    assert m["hosting"] == "aws-bedrock" and m["provider"] == "reg42-infer"
    assert set(m["task_classes"]) == set(tc.TASK_CLASSES)
    for name in tc.DERIVATION_CLASSES:
        assert m["task_classes"][name]["procurement_clean"]


def test_model_manifest_rejects_cn_origin_in_derivation():
    m = mm.build_model_manifest(release_id="2026.09.28", resolved={"l2_extract": tc.QWEN_3_5_32B})
    problems = mm.check_manifest(m)
    assert any("non-procurement-clean" in p for p in problems)
    tampered = dict(m)
    tampered["hosting"] = "on-prem-ollama"
    assert any("aws-bedrock" in p for p in mm.check_manifest(tampered))


def _pass_gate(engine, layer: str, release: str, passed: bool = True):
    from app.clhear.models import eval_runs
    from app.clhear.platform.gates import LAYER_GATES

    with engine.begin() as conn:
        for suite in LAYER_GATES[layer]:
            conn.execute(eval_runs.insert().values(suite=suite, release=release, scores={"ok": 1}, passed=passed))


def test_fresh_install_publishes_l1_unverified_and_verify_flags_it(engine):
    manifest = releases.publish_release(engine, release_id="2026.09.26")
    assert "L1" in manifest["layers"]
    assert manifest["gates"]["L1"]["unverified"] is True
    ok, problems, _ = verify_release.verify(releases._local_root() / "2026.09.26")
    assert not ok and any("unverified" in p for p in problems)


def test_layer_below_gate_is_reserved_not_published(engine):
    _pass_gate(engine, "L1", "2026.09.25", passed=False)
    manifest = releases.publish_release(engine, release_id="2026.09.25")
    assert "L1" not in manifest["layers"]
    assert "L1" in manifest["reserved_layers"]
    assert manifest["gates"]["L1"]["passed"] is False
    root = releases._local_root() / "2026.09.25"
    assert (root / "l1" / ".reserved").exists()


def test_publish_and_verify_release(engine, tmp_path):
    _pass_gate(engine, "L1", "2026.09.28")
    frozen = mm.build_model_manifest(release_id="2026.09.28")
    releases._FROZEN_MODEL_MANIFEST = frozen
    try:
        manifest = releases.publish_release(engine, release_id="2026.09.28")
    finally:
        releases._FROZEN_MODEL_MANIFEST = None
    assert manifest["spec_version"] == 2
    assert manifest["version"] == "2026.09.28"
    assert manifest["layers"][0] == "L0"
    assert manifest["model_manifest"]["manifest_hash"] == frozen["manifest_hash"]
    assert releases.verify_manifest_hash(manifest)
    assert manifest["signature"]["scheme"] == "sigstore-cosign-keyless" and manifest["signature"]["signed"] is False
    assert set(manifest["licence"]) == {"data", "text_and_schemas", "code"}

    root = releases._local_root() / "2026.09.28"
    assert (root / "manifest.json").exists()
    ok, problems, loaded = verify_release.verify(root)
    assert ok, problems
    assert loaded["id"] == "2026.09.28"

    # tamper → hash check fails
    tampered = json.loads((root / "manifest.json").read_text())
    tampered["layers"].append("L9")
    (root / "manifest.json").write_text(json.dumps(tampered))
    ok, problems, _ = verify_release.verify(root)
    assert not ok and any("manifest_hash" in p for p in problems)

    # signature required but absent → fails
    (root / "manifest.json").write_text(json.dumps(manifest))
    ok, problems, _ = verify_release.verify(root, require_signature=True)
    assert not ok and any("signature" in p.lower() for p in problems)


def test_delta_against_previous(engine):
    first = releases.publish_release(engine, release_id="2026.09.27")
    second = releases.publish_release(engine, release_id="2026.09.28")
    assert first["delta"]["against"] is None
    assert second["delta"]["against"] == "2026.09.27"
    assert set(second["delta"]) == {"against", "counts", "layers_added", "layers_removed"}
    latest = releases.get_latest()
    assert latest["id"] == "2026.09.28"
    assert [r["id"] for r in releases.list_releases()][:2] == ["2026.09.28", "2026.09.27"]
