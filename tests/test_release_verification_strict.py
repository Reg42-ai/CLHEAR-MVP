"""Promotion verifies actual artifacts and a fixed signer, never declarations alone."""
import hashlib
import io
import json
from types import SimpleNamespace

import boto3
import pytest

from app.clhear import releases
from app.clhear.platform import manifest as model_manifest
from app.clhear.platform import release_verification as verification


def _candidate(tmp_path, *, signature=False):
    snapshot = b"TEST ONLY snapshot bytes"
    (tmp_path / "l1").mkdir()
    (tmp_path / "l1" / "snapshot.db").write_bytes(snapshot)
    digest = hashlib.sha256(snapshot).hexdigest()
    manifest = {
        "id": "2026.09.15", "spec_version": 2, "layers": ["L0", "L1"],
        "gates": {"L1": {"passed": True}}, "l1": {"content_hash": digest},
        "artifacts": {"snapshot": {"uri": "s3://test-only/release/snapshot.db", "sha256": digest}},
        "model_manifest": model_manifest.build_model_manifest(release_id="2026.09.15"),
    }
    if signature:
        sbom = b'{"spdxVersion":"SPDX-2.3","name":"test-only"}'
        (tmp_path / "sbom.spdx.json").write_bytes(sbom)
        manifest.update(sbom_uri="sbom.spdx.json", sbom_sha256=hashlib.sha256(sbom).hexdigest())
        manifest["signature"] = {"scheme": "sigstore-cosign-keyless", "signed": False,
                                 "status": "verification_required", "identity": verification.TRUSTED_RELEASE_IDENTITY,
                                 "bundle_uri": "manifest.sigstore.json"}
        (tmp_path / "manifest.sigstore.json").write_text("TEST ONLY detached bundle")
    _write(tmp_path, manifest)
    return manifest


def _write(root, manifest):
    manifest["manifest_hash"] = releases._manifest_hash(manifest)
    (root / "manifest.json").write_text(json.dumps(manifest))


def test_blank_snapshot_digest_cannot_verify_existing_file(tmp_path):
    manifest = _candidate(tmp_path)
    manifest["artifacts"]["snapshot"]["sha256"] = ""
    _write(tmp_path, manifest)
    ok, problems, _ = verification.verify(tmp_path)
    assert not ok and any("snapshot sha256 must" in problem for problem in problems)


def test_s3_uri_requires_readable_actual_bytes(tmp_path, monkeypatch):
    _candidate(tmp_path)
    (tmp_path / "l1" / "snapshot.db").unlink()

    def unavailable(*args, **kwargs):
        raise RuntimeError("test-only object is inaccessible")

    monkeypatch.setattr(boto3, "client", unavailable)
    ok, problems, _ = verification.verify(tmp_path)
    assert not ok and any("snapshot bytes could not be verified" in problem for problem in problems)


@pytest.mark.parametrize("body,expected", [(b"TEST ONLY snapshot bytes", True), (b"changed bytes", False)])
def test_remote_snapshot_hash_is_computed_from_object_bytes(tmp_path, monkeypatch, body, expected):
    _candidate(tmp_path)
    (tmp_path / "l1" / "snapshot.db").unlink()
    requests = []

    def get_object(**kwargs):
        requests.append(kwargs)
        return {"Body": io.BytesIO(body)}

    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: SimpleNamespace(get_object=get_object))
    ok, problems, _ = verification.verify(tmp_path)
    assert ok is expected, problems
    assert requests == [{"Bucket": "test-only", "Key": "release/snapshot.db"}]


def test_detached_bundle_uses_fixed_identity_and_exact_manifest_bytes(tmp_path, monkeypatch):
    _candidate(tmp_path, signature=True)
    original = (tmp_path / "manifest.json").read_bytes()
    commands = []
    monkeypatch.setattr(verification.shutil, "which", lambda _: "/test-only/cosign")

    def run(command, **kwargs):
        commands.append(command)
        assert (tmp_path / "manifest.json").read_bytes() == original
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(verification.subprocess, "run", run)
    ok, problems, manifest = verification.verify(tmp_path, require_signature=True)
    assert ok, problems
    assert manifest["signature"]["signed"] is False  # immutable declaration; receipt holds verification
    assert "--certificate-identity-regexp" not in commands[0]
    assert commands[0][commands[0].index("--certificate-identity") + 1] == verification.TRUSTED_RELEASE_IDENTITY
    assert (tmp_path / "manifest.json").read_bytes() == original


def test_manifest_cannot_choose_another_trusted_signer(tmp_path, monkeypatch):
    manifest = _candidate(tmp_path, signature=True)
    manifest["signature"]["identity"] = ".*"
    _write(tmp_path, manifest)
    monkeypatch.setattr(verification.shutil, "which", lambda _: None)
    ok, problems, _ = verification.verify(tmp_path, cosign_identity=".*")
    assert not ok
    assert any("identity override differs" in problem for problem in problems)
    assert any("manifest signer identity differs" in problem for problem in problems)


def test_bundle_without_cosign_never_verifies(tmp_path, monkeypatch):
    _candidate(tmp_path, signature=True)
    monkeypatch.setattr(verification.shutil, "which", lambda _: None)
    ok, problems, _ = verification.verify(tmp_path)
    assert not ok and "cosign not installed; cannot verify signature" in problems


def test_changed_signed_manifest_is_rejected_even_with_recomputed_manifest_hash(tmp_path, monkeypatch):
    manifest = _candidate(tmp_path, signature=True)
    signed_bytes = (tmp_path / "manifest.json").read_bytes()
    manifest["extra"] = "changed after signing"
    _write(tmp_path, manifest)
    monkeypatch.setattr(verification.shutil, "which", lambda _: "/test-only/cosign")

    def verify_blob(command, **kwargs):
        assert (tmp_path / "manifest.json").read_bytes() != signed_bytes
        return SimpleNamespace(returncode=1, stderr="test-only signature mismatch")

    monkeypatch.setattr(verification.subprocess, "run", verify_blob)
    ok, problems, _ = verification.verify(tmp_path, require_signature=True)
    assert not ok and any("cosign verify-blob failed" in problem for problem in problems)


def test_changed_sbom_is_rejected_before_promotion(tmp_path, monkeypatch):
    _candidate(tmp_path, signature=True)
    (tmp_path / "sbom.spdx.json").write_text("changed SBOM")
    monkeypatch.setattr(verification.shutil, "which", lambda _: None)
    ok, problems, _ = verification.verify(tmp_path, require_signature=True)
    assert not ok and "SBOM bytes do not match the manifest hash" in problems
