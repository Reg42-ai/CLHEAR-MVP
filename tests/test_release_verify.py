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


def test_live_preview_never_exposes_database_connection_url(engine, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(releases, "get_settings", lambda: SimpleNamespace(
        database_url="postgresql://test-user:private-test-password@database.example/test"))
    manifest = releases._live_manifest(engine)
    assert manifest["l1"]["snapshot_uri"] == ""
    assert manifest["artifacts"]["snapshot"]["uri"] == ""
    assert "private-test-password" not in json.dumps(manifest)


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


def test_fresh_install_creates_blocked_candidate_without_advancing_latest(engine):
    manifest = releases.publish_release(engine, release_id="2026.09.26")
    assert "L1" not in manifest["layers"]
    assert manifest["status"] == "blocked"
    assert manifest["acceptance"]["passed"] is False
    assert releases.get_latest() is None
    ok, problems, _ = verify_release.verify(releases._local_root() / "2026.09.26")
    assert not ok and any("snapshot" in p for p in problems)


def test_layer_below_gate_is_reserved_not_published(engine):
    _pass_gate(engine, "L1", "2026.09.25", passed=False)
    manifest = releases.publish_release(engine, release_id="2026.09.25")
    assert "L1" not in manifest["layers"]
    assert "L1" in manifest["reserved_layers"]
    assert manifest["gates"]["L1"]["passed"] is False
    root = releases._local_root() / "2026.09.25"
    assert not (root / "l1" / "snapshot.db").exists()
    assert releases.get_latest() is None


def _accepted_inventory(monkeypatch):
    from app.clhear.l1 import inventory
    acceptance = {"passed": True, "inventory_hash": "reviewed-inventory", "bindings_hash": "reviewed-bindings",
                  "audit_id": "audited-test-corpus", "reasons": []}
    monkeypatch.setattr(inventory, "acceptance_status", lambda *a, **kw: acceptance)
    return acceptance


def _test_corpus(engine, tmp_path):
    from tests.test_l1_pipeline import _StubAdapter, _tree
    from app.clhear.l1.pipeline import LocalStore, ingest
    return ingest(engine, _StubAdapter("v1", _tree(("test-1", "Original test fixture text."))),
                  LocalStore(tmp_path / "source"), index_embeddings=False)


def test_publish_and_verify_release(engine, tmp_path, monkeypatch):
    _accepted_inventory(monkeypatch)
    _test_corpus(engine, tmp_path)
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
    assert releases.get_latest() is None  # unsigned preparation is not promotion
    import sqlite3
    with sqlite3.connect(root / "l1" / "snapshot.db") as snapshot:
        tables = {r[0] for r in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "clauses" in tables
        assert not {"runs", "llm_calls", "accounts", "sessions", "api_keys"}.intersection(tables)

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


def test_failed_candidates_never_replace_previous_accepted_pointer(engine):
    pointer = {"id": "clhear-vaccepted", "verified_signature": True}
    releases._put_json_local(releases._local_root() / "latest.json", pointer)
    first = releases.publish_release(engine, release_id="2026.09.27")
    second = releases.publish_release(engine, release_id="2026.09.28")
    assert first["delta"]["against"] is None
    assert second["delta"]["against"] is None
    assert set(second["delta"]) == {"against", "counts", "layers_added", "layers_removed"}
    assert releases._get_json_local(releases._local_root() / "latest.json") == pointer
    assert [r["id"] for r in releases.list_releases()][:2] == ["2026.09.28", "2026.09.27"]


def test_candidate_manifest_is_immutable_and_read_methods_do_not_create_directories(engine):
    root = releases._local_root()
    assert not root.exists()
    assert releases.get_latest() is None and releases.list_releases() == []
    assert not root.exists()
    first = releases.publish_release(engine, release_id="2026.09.27")
    second = releases.publish_release(engine, release_id="2026.09.27")
    assert first == second


def test_promotion_requires_signature_and_rechecks_current_inventory(engine, tmp_path, monkeypatch):
    from app.clhear.platform import release_verification
    acceptance = _accepted_inventory(monkeypatch)
    imported = _test_corpus(engine, tmp_path)
    _pass_gate(engine, "L1", "2026.09.28")
    manifest = releases.publish_release(engine, release_id="2026.09.28")
    with pytest.raises(ValueError, match="verification failed"):
        releases.promote_release(engine, "2026.09.28")
    called = []
    def verified(root, *, require_signature):
        called.append(require_signature)
        return True, [], manifest
    monkeypatch.setattr(release_verification, "verify", verified)
    receipt = releases.promote_release(engine, "2026.09.28")
    assert called == [True] and receipt["verified_signature"]
    assert releases.get_latest()["id"] == "2026.09.28"
    assert manifest["projection"]["bindings"][0]["source_version_id"] == imported["source_version_id"]
    acceptance["passed"] = False
    with pytest.raises(ValueError, match="not accepted"):
        releases.promote_release(engine, "2026.09.28")


class _ConditionalS3:
    """In-memory S3 conditional writes; no network or publisher material."""
    def __init__(self):
        self.objects = {}
        self.get_denied = set()
        self.put_denied = set()
        self.before_pointer_write = None
        self.writes = []

    @staticmethod
    def _error(code, operation):
        from botocore.exceptions import ClientError
        return ClientError({"Error": {"Code": code, "Message": "test-only failure"}}, operation)

    @staticmethod
    def _etag(body):
        import hashlib
        return '"' + hashlib.sha256(body).hexdigest() + '"'

    def get_object(self, *, Bucket, Key):
        import io
        if Key in self.get_denied:
            raise self._error("AccessDenied", "GetObject")
        if Key not in self.objects:
            raise self._error("NoSuchKey", "GetObject")
        return {"Body": io.BytesIO(self.objects[Key]), "ETag": self._etag(self.objects[Key])}

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        if Key in self.put_denied:
            raise self._error("AccessDenied", "PutObject")
        if Key.endswith("/latest.json") and self.before_pointer_write:
            action, self.before_pointer_write = self.before_pointer_write, None
            action()
        if kwargs.get("IfNoneMatch") == "*" and Key in self.objects:
            raise self._error("PreconditionFailed", "PutObject")
        if "IfMatch" in kwargs and (Key not in self.objects or self._etag(self.objects[Key]) != kwargs["IfMatch"]):
            raise self._error("PreconditionFailed", "PutObject")
        body = Body.read() if hasattr(Body, "read") else Body
        self.objects[Key] = body
        self.writes.append({"key": Key, **kwargs})
        return {"ETag": self._etag(body)}


def test_s3_missing_is_distinguished_from_access_denied(monkeypatch):
    s3 = _ConditionalS3()
    monkeypatch.setattr(releases, "_s3", lambda: s3)
    assert releases._get_json_s3("test", "missing") is None
    assert releases._latest_s3_etag("test", "missing") is None
    s3.get_denied.add("denied")
    with pytest.raises(Exception, match="AccessDenied"):
        releases._get_json_s3("test", "denied")
    with pytest.raises(Exception, match="AccessDenied"):
        releases._latest_s3_etag("test", "denied")


def test_s3_release_artifacts_are_create_only_and_verify_existing_bytes(tmp_path, monkeypatch):
    s3 = _ConditionalS3()
    monkeypatch.setattr(releases, "_s3", lambda: s3)
    file = tmp_path / "artifact"
    file.write_bytes(b"original test artifact")
    releases._immutable_s3_file(file, "test", "release/object")
    releases._immutable_s3_file(file, "test", "release/object")
    assert len(s3.writes) == 1 and s3.writes[0]["IfNoneMatch"] == "*"
    file.write_bytes(b"changed test artifact")
    with pytest.raises(ValueError, match="different bytes"):
        releases._immutable_s3_file(file, "test", "release/object")
    assert s3.objects["release/object"] == b"original test artifact"


def _s3_candidate(engine, tmp_path, monkeypatch):
    from app.clhear.settings import get_settings
    from app.clhear.platform import release_verification
    _accepted_inventory(monkeypatch)
    _test_corpus(engine, tmp_path)
    _pass_gate(engine, "L1", "2026.09.28")
    s3 = _ConditionalS3()
    monkeypatch.setenv("CLHEAR_RELEASES_S3_PREFIX", "s3://test-only/releases")
    get_settings.cache_clear()
    monkeypatch.setattr(releases, "_s3", lambda: s3)
    manifest = releases.publish_release(engine, release_id="2026.09.28")
    # Signature validation has its own strict tampering/identity tests. Here a
    # verified fixture isolates the distributed publication commit boundary.
    monkeypatch.setattr(release_verification, "verify", lambda root, require_signature: (True, [], manifest))
    return s3, manifest


def test_pointer_compare_and_swap_rejects_a_concurrent_promotion(engine, tmp_path, monkeypatch):
    s3, manifest = _s3_candidate(engine, tmp_path, monkeypatch)
    key = "releases/latest.json"
    first = b'{"id":"clhear-vprior","verified_signature":true}'
    winner = b'{"id":"clhear-vconcurrent-winner","verified_signature":true}'
    s3.objects[key] = first
    s3.before_pointer_write = lambda: s3.objects.__setitem__(key, winner)
    with pytest.raises(Exception, match="PreconditionFailed"):
        releases.promote_release(engine, manifest["id"])
    assert s3.objects[key] == winner
    assert "releases/2026.09.28/promotion.json" not in s3.objects


def test_remote_receipt_failure_after_pointer_commit_still_reports_accepted(engine, tmp_path, monkeypatch):
    s3, manifest = _s3_candidate(engine, tmp_path, monkeypatch)
    receipt_key = f"releases/{manifest['id']}/promotion.json"
    s3.put_denied.add(receipt_key)
    receipt = releases.promote_release(engine, manifest["id"])
    assert receipt["status"] == "accepted"
    assert receipt_key not in s3.objects
    assert json.loads(s3.objects["releases/latest.json"]) == receipt
    assert releases.get_promotion(manifest["id"]) == receipt
    pointer_write = next(write for write in s3.writes if write["key"] == "releases/latest.json")
    assert pointer_write["IfNoneMatch"] == "*"


def test_local_receipt_failure_after_pointer_commit_can_use_latest_receipt(engine, tmp_path, monkeypatch):
    from app.clhear.platform import release_verification
    _accepted_inventory(monkeypatch)
    _test_corpus(engine, tmp_path)
    _pass_gate(engine, "L1", "2026.09.28")
    manifest = releases.publish_release(engine, release_id="2026.09.28")
    monkeypatch.setattr(release_verification, "verify", lambda root, require_signature: (True, [], manifest))
    write = releases._put_json_local
    def fail_receipt(path, payload):
        if path.name == "promotion.json":
            raise OSError("test-only receipt disk failure")
        write(path, payload)
    monkeypatch.setattr(releases, "_put_json_local", fail_receipt)
    receipt = releases.promote_release(engine, manifest["id"])
    assert receipt["status"] == "accepted"
    assert releases.get_promotion(manifest["id"]) == receipt
    assert not (releases._local_root() / manifest["id"] / "promotion.json").exists()


def test_changed_manifest_body_cannot_reuse_an_old_promotion_receipt(engine, tmp_path, monkeypatch):
    from app.clhear.platform import release_verification
    _accepted_inventory(monkeypatch)
    _test_corpus(engine, tmp_path)
    _pass_gate(engine, "L1", "2026.09.28")
    manifest = releases.publish_release(engine, release_id="2026.09.28")
    monkeypatch.setattr(release_verification, "verify", lambda root, require_signature: (True, [], manifest))
    releases.promote_release(engine, manifest["id"])
    assert releases.get_promotion(manifest["id"])
    changed = {**manifest, "layers": ["L0", "L1", "L8"]}
    releases._put_json_local(releases._local_root() / manifest["id"] / "manifest.json", changed)
    assert changed["manifest_hash"] == manifest["manifest_hash"]
    assert releases.get_promotion(manifest["id"]) is None


@pytest.mark.parametrize("field", ["id", "destination", "audience", "missing_l1"])
def test_promotion_rejects_inconsistent_signed_release_identity(engine, tmp_path, monkeypatch, field):
    s3, manifest = _s3_candidate(engine, tmp_path, monkeypatch)
    release_id = manifest["id"]
    if field == "id":
        manifest["id"] = "clhear-vdifferent-release"
    elif field == "destination":
        manifest["l1"]["snapshot_uri"] = "s3://different-destination/release/snapshot.db"
    elif field == "audience":
        manifest["audience"] = "public"
    else:
        manifest["layers"] = ["L0"]
    with pytest.raises(ValueError):
        releases.promote_release(engine, release_id)
    assert "releases/latest.json" not in s3.objects
