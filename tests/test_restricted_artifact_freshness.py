"""Authorized artifact availability is separate from publisher freshness."""
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.clhear.l1 import inventory, pipeline, permissions, translation
from app.clhear.l1.adapters import restricted_file
from app.clhear.models import runs
from tests.test_l1_originals import minimal_pdf


KEY = "iso/27001-2022"
URL = "https://www.iso.org/standard/27001"
BODY = minimal_pdf(["1 Scope", "Original unit-test text. Keep exactly 7 fixture records."])


@pytest.fixture
def imported_artifact(engine, monkeypatch, tmp_path):
    entry = {"key": KEY, "name": "ISO edition fixture", "canonical_url": URL,
             "adapter": "restricted_file", "license": "restricted"}
    monkeypatch.setattr(inventory, "_declared_entries", lambda scope: {KEY: entry})
    monkeypatch.setattr(restricted_file, "_list_restricted_objects", lambda key: [("original.pdf", BODY, "application/pdf")])
    permissions.record_permission(engine, source_key=KEY, permissions={"acquire": True, "store": True, "parse": True,
        "display_internal": True}, evidence_ref="test-only:authorized-artifact", approved_by="fixture-reviewer", approved=True)
    adapter = restricted_file.RestrictedFileAdapter(KEY, "ISO edition fixture", URL)
    store = pipeline.LocalStore(tmp_path / "originals")
    imported = pipeline.ingest(engine, adapter, store, index_embeddings=False)
    assert imported["status"] == "added", imported
    inventory.record_artifact_review(engine, KEY, imported["content_hash"], inventory.EXPECTED_EDITIONS[KEY], URL,
        coverage="full", evidence_ref="test-only:exact-reviewed-edition", approved_by="fixture-reviewer", approved=True)
    return adapter, store, imported


def audit(engine, store, job="artifact-audit"):
    return inventory.run_inventory_audit(engine, store, job_id=job, scope="finra")


def codes(source):
    return {finding["code"] for finding in source["findings"]}


def test_reviewed_artifact_passes_source_integrity_without_publisher_claim(engine, imported_artifact):
    adapter, store, imported = imported_artifact
    assert imported["publisher_checked_at"] is None
    receipt = imported["authorized_artifact_check"]
    assert receipt["publisher_check_performed"] is False
    assert receipt["method"] == "authorized_artifact_store_read"
    source = audit(engine, store)["sources"][0]
    assert source["verified"], source["findings"]
    assert source["freshness_basis"] == "reviewed_immutable_artifact"
    assert source["artifact_checked_at"] == receipt["checked_at"]
    assert source["publisher_checked_at"] is None
    gate = inventory.acceptance_status(engine, "finra")
    assert not gate["passed"]  # artifact availability cannot establish full publisher scope
    assert "publisher_inventory_overdue" in gate["reasons"] and "scope_not_verified" in gate["reasons"]
    assert not any(reason.startswith("publisher_check_overdue:") for reason in gate["reasons"])
    again = pipeline.ingest(engine, adapter, store, index_embeddings=False)
    assert again["status"] == "unchanged" and again["source_version_id"] == imported["source_version_id"]
    assert again["publisher_checked_at"] is None
    assert again["authorized_artifact_check"]["checked_at"] >= receipt["checked_at"]


def test_complete_reviewed_scope_can_accept_immutable_artifact_freshness(engine, imported_artifact, monkeypatch):
    _, store, imported = imported_artifact
    monkeypatch.setattr(inventory, "_discover", lambda *args: ({}, {"complete": True,
        "checked_at": datetime.now(timezone.utc).isoformat(), "pages": [], "categories": [], "findings": []}))
    first = inventory.run_inventory_audit(engine, store, job_id="scope-discovery", scope="finra", discover=True)
    inventory.record_scope_review(engine, first["inventory_hash"], "test-only:complete-fixture-inventory", "fixture-reviewer", True)
    result = audit(engine, store)
    assert result["status"] == "verified", result["findings"]
    with engine.begin() as conn:
        translation.record_language_binding(conn, source_version_id=imported["source_version_id"], language="en",
            document_key=KEY, authority="authoritative", evidence_ref="test-only:authored-English", approved_by="fixture-reviewer")
    assert translation.build_english_view(engine, None, imported["source_version_id"])["english_ready"]
    gate = inventory.acceptance_status(engine, "finra")
    assert gate["passed"], gate["reasons"]
    assert gate["evidence"]["sources"][0]["publisher_checked_at"] is None


@pytest.mark.parametrize("mutation", ["missing", "wrong_hash", "stale", "future", "publisher_impersonation"])
def test_unbound_or_stale_artifact_receipts_cannot_refresh_version(engine, imported_artifact, mutation):
    _, store, _ = imported_artifact
    with engine.begin() as conn:
        row = conn.execute(sa.select(runs).where(runs.c.inputs["source"].as_string() == KEY)).mappings().one()
        output = row["outputs"]
        receipt = output["authorized_artifact_check"]
        if mutation == "missing":
            output.pop("authorized_artifact_check")
        elif mutation == "wrong_hash":
            receipt["artifacts"][0]["sha256"] = "0" * 64
        elif mutation == "publisher_impersonation":
            receipt["publisher_check_performed"] = True
        else:
            receipt["checked_at"] = (datetime.now(timezone.utc) + timedelta(days=2 if mutation == "future" else -2)).isoformat()
        conn.execute(runs.update().where(runs.c.id == row["id"]).values(outputs=output))
    source = audit(engine, store)["sources"][0]
    assert not source["verified"] and "artifact_check_overdue" in codes(source)
    assert source["publisher_checked_at"] is None
    assert not inventory.acceptance_status(engine, "finra")["passed"]


@pytest.mark.parametrize("mutation", ["absent", "store_error", "wrong_edition", "excerpt", "revoked"])
def test_actual_dependency_failures_remain_gaps(engine, imported_artifact, monkeypatch, mutation):
    adapter, store, imported = imported_artifact
    if mutation == "absent":
        monkeypatch.setattr(restricted_file, "_list_restricted_objects", lambda key: [])
        assert pipeline.ingest(engine, adapter, store, index_embeddings=False)["status"] == "awaiting-artifact"
    elif mutation == "store_error":
        def fail(*args):
            raise RuntimeError("fixture store unavailable")
        monkeypatch.setattr(restricted_file, "_list_restricted_objects", fail)
        assert pipeline.ingest(engine, adapter, store, index_embeddings=False)["status"] == "stale"
    elif mutation == "revoked":
        permissions.record_permission(engine, source_key=KEY, permissions={"acquire": False, "store": False, "parse": False},
            evidence_ref="test-only:revoked", approved_by="fixture-reviewer", approved=True)
    else:
        inventory.record_artifact_review(engine, KEY, imported["content_hash"],
            "wrong edition" if mutation == "wrong_edition" else inventory.EXPECTED_EDITIONS[KEY], URL,
            coverage="excerpt" if mutation == "excerpt" else "full", evidence_ref="test-only:review-change", approved_by="fixture-reviewer", approved=True)
    source = audit(engine, store)["sources"][0]
    assert not source["verified"]
    expected = {"absent": "artifact_availability_unverified", "store_error": "artifact_availability_unverified",
                "wrong_edition": "edition_unverified", "excerpt": "partial_artifact", "revoked": "permission_blocked"}[mutation]
    assert expected in codes(source)
    assert source["publisher_checked_at"] is None
