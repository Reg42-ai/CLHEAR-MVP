"""Private operator authority can prove fidelity, never publisher permission.

Original prose comes only from authored offline fixtures. The real exception,
permission, audit, eval and release APIs are exercised without live acquisition.
"""
import copy

import pytest
import sqlalchemy as sa

from app.clhear import releases
from app.clhear.l1 import inventory, operator_exceptions, permissions, release_snapshot
from app.clhear.l1.models import source_versions
from app.clhear.l1.pipeline import LocalStore
from app.clhear.platform import evals, release_verification
from tests.test_l1_inventory import BODY, ENTRY, KEY, URL, grant, imported


@pytest.fixture
def candidate(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(inventory, "_declared_entries", lambda scope: {KEY: dict(ENTRY)})
    monkeypatch.setattr(releases, "_s3_parts", lambda: None)
    store = LocalStore(tmp_path / "originals")
    version_id, artifact_key = imported(engine, store, content_hash_method="artifact-set-v2")
    return engine, store, version_id, artifact_key


def activate(engine, *, command_id="fixture-activate", manifest_hash="a" * 64):
    event = operator_exceptions.record_exception(
        engine, command_id=command_id, action="activate", approved_by="fixture-owner",
        evidence_ref="test-only:owner-private-review", rationale="Private technical review; publisher permission unresolved",
    )
    return operator_exceptions.bind_source(
        engine, activation_id=event["id"], manifest_hash=manifest_hash, scope_version="authored-fixture-v1",
        source_key=KEY, canonical_url=URL, source_role="document", bound_by="l0.fixture",
    )


def revoke(engine):
    return operator_exceptions.record_exception(
        engine, command_id="fixture-revoke", action="revoke", approved_by="fixture-owner",
        evidence_ref="test-only:owner-revocation", rationale="Private review ended",
    )


class NoReadStore:
    def get(self, key):
        pytest.fail("An unapproved audit attempted to read original bytes")


def test_active_exception_proves_technical_fidelity_but_cannot_prepare_or_promote_release(candidate, tmp_path, monkeypatch):
    engine, store, version_id, artifact_key = candidate
    binding = activate(engine)
    audit = inventory.run_inventory_audit(engine, store, job_id="l1.fixture-private-review")
    source = audit["sources"][0]
    assert source["technical_verified"], source["findings"]
    assert source["original_comparison"]["verified"]
    assert source["operator_exception_used"] and not source["release_eligible"]
    assert not source["verified"] and audit["verified"] == 0 and audit["technical_verified"] == 1
    assert source["candidate_permissions"]["parse"]["binding_id"] == binding["id"]
    assert {f["code"] for f in source["findings"]} == {"permission_unverified", "operator_exception_used"}
    with engine.connect() as conn:
        assert not permissions.decision(conn, KEY, "store")["allowed"]
        assert conn.execute(sa.select(source_versions.c.id)).scalars().all() == [version_id]
    assert store.get(artifact_key) == BODY

    for suite in ("e1_fidelity", "e2_completeness", "e3_roundtrip"):
        result = evals.run_suite(engine, suite, source_key=KEY)
        assert result["passed"], result
        assert result["scores"]["evaluation_scope"] == "technical_candidate"
        assert result["scores"]["operator_exception_used"]
        assert not result["scores"]["release_eligible"]
    release_eval = evals.run_suite(engine, "l1_inventory_acceptance")
    assert not release_eval["passed"]
    gate = inventory.acceptance_status(engine)
    assert not gate["passed"] and not gate["release_eligible"]
    assert "operator_exception_not_release_authority" in gate["reasons"]
    assert "publisher_permission_unresolved:" + KEY in gate["reasons"]

    # Even a direct compiler invocation cannot turn private review into release.
    destination = tmp_path / "release.db"
    with pytest.raises(PermissionError, match="storage"):
        release_snapshot.compile_snapshot(engine, destination)
    assert not destination.exists()

    root = releases._local_root()
    root.mkdir(parents=True, exist_ok=True)
    previous = b'{"id":"clhear-vprevious","status":"accepted"}'
    pointer = root / releases.LATEST_NAME
    pointer.write_bytes(previous)
    manifest = releases.publish_release(engine, release_id="clhear-voperator-test")
    assert manifest["status"] == "blocked" and manifest["layers"] == ["L0"]
    assert not manifest["l1"]["snapshot_uri"] and not manifest["l1"]["content_hash"]
    assert not (root / manifest["id"] / "l1" / "snapshot.db").exists()

    # Simulate a signed candidate presentation to reach the independent live
    # acceptance check, rather than stopping at missing local signing material.
    signed_claim = copy.deepcopy(manifest)
    signed_claim.update(status="candidate", layers=["L0", "L1"])
    signed_claim["acceptance"]["passed"] = True
    monkeypatch.setattr(release_verification, "verify", lambda *a, **kw: (True, [], signed_claim))
    with pytest.raises(ValueError, match="L1 inventory is not accepted"):
        releases.promote_release(engine, manifest["id"])
    assert pointer.read_bytes() == previous


def test_without_exception_audit_and_evals_do_not_read_protected_original(candidate):
    engine, _, _, _ = candidate
    source = inventory.run_inventory_audit(engine, NoReadStore(), job_id="l1.fixture-no-authority")["sources"][0]
    assert not source["technical_verified"] and not source["operator_exception_used"]
    assert source["artifacts"] == []
    result = evals.run_suite(engine, "e1_fidelity", source_key=KEY)
    assert not result["passed"] and result["scores"]["not_evaluated"]
    assert result["scores"]["operations"] == ["store", "parse"]


def test_revocation_invalidates_audit_and_stops_further_original_reads(candidate):
    engine, store, _, _ = candidate
    activate(engine)
    inventory.run_inventory_audit(engine, store, job_id="l1.fixture-before-revoke")
    assert inventory.source_inventory_evidence(engine, KEY)["technical_verified"]
    revoke(engine)
    summary = inventory.inventory_summary(engine)
    assert not summary["current_binding_valid"] and summary["status"] == "stale"
    assert not inventory.source_inventory_evidence(engine, KEY)["technical_verified"]
    after = inventory.run_inventory_audit(engine, NoReadStore(), job_id="l1.fixture-after-revoke")
    assert not after["sources"][0]["technical_verified"]
    assert after["sources"][0]["artifacts"] == []


def test_revocation_during_technical_eval_cannot_record_a_pass(candidate, monkeypatch):
    engine, store, _, _ = candidate
    activate(engine)
    inventory.run_inventory_audit(engine, store, job_id="l1.fixture-before-eval")
    real_suite = evals.SUITES["e1_fidelity"]
    def revoke_after_evaluation(engine, source_key):
        scores, passed = real_suite(engine, source_key)
        assert passed
        revoke(engine)
        return scores, passed
    monkeypatch.setitem(evals.SUITES, "e1_fidelity", revoke_after_evaluation)
    result = evals.run_suite(engine, "e1_fidelity", source_key=KEY)
    assert not result["passed"]
    assert "authorization changed" in result["scores"]["binding_error"]
    assert not result["scores"]["release_eligible"]


def test_genuine_publisher_grant_supersedes_exception_after_fresh_audit(candidate):
    engine, store, _, _ = candidate
    activate(engine)
    inventory.run_inventory_audit(engine, store, job_id="l1.fixture-before-grant")
    grant(engine)
    assert not inventory.inventory_summary(engine)["current_binding_valid"]
    audit = inventory.run_inventory_audit(engine, store, job_id="l1.fixture-publisher-granted")
    source = audit["sources"][0]
    assert source["verified"] and source["technical_verified"] and source["release_eligible"]
    assert not source["operator_exception_used"]
    assert source["candidate_permissions"]["parse"]["authority_type"] == "publisher_permission"
    result = evals.run_suite(engine, "e2_completeness", source_key=KEY)
    assert result["passed"] and result["scores"]["release_eligible"]
    assert not result["scores"]["operator_exception_used"]
    assert result["scores"]["evaluation_scope"] == "publisher_authorized"
    # Document permission is necessary but does not itself approve full scope.
    assert not inventory.acceptance_status(engine)["passed"]


def test_release_compiler_requires_publisher_acquisition_and_parsing_even_with_display_grant(candidate, tmp_path):
    engine, _, _, _ = candidate
    activate(engine)
    grant(engine, permissions={"store": True, "display_internal": True})
    with pytest.raises(PermissionError, match="Publisher permission is unresolved"):
        release_snapshot.compile_snapshot(engine, tmp_path / "release.db")
    assert not (tmp_path / "release.db").exists()
