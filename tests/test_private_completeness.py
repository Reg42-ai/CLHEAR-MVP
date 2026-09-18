"""Live-instance completeness mode: fetch every current URL, no extra release hold."""
from pathlib import Path
from unittest.mock import Mock

import sqlalchemy as sa

from app.clhear.l1 import fleet, inventory, permissions, poc_review, source_registry
from app.clhear.settings import get_settings


def _enable(monkeypatch):
    monkeypatch.setenv("CLHEAR_PRIVATE_COMPLETENESS", "true")
    get_settings.cache_clear()


def test_flag_off_keeps_collection_declaration_gap_and_awaiting_artifact(engine, monkeypatch, tmp_path):
    from app.clhear.l1 import pipeline
    from app.clhear.l1.adapters import restricted_file

    entry = next(e for e in source_registry.S if e["key"] == "finra/rulebook")
    assert fleet.adapter_for(entry).declaration_gap["code"] == "collection_requires_document_discovery"
    overlay = next(e for e in source_registry.S if e["key"] == "ovl/be")
    assert fleet.adapter_for(overlay).declaration_gap
    declared = inventory._declared_entries("registered")
    assert "finra/rulebook" not in declared
    assert "iso/27001-2022" in declared
    permissions.record_permission(
        engine, source_key="iso/27001-2022",
        permissions={"acquire": True, "store": True, "parse": True},
        evidence_ref="test-only:no-completeness", approved_by="test-reviewer", approved=True)
    monkeypatch.setattr(restricted_file, "_list_restricted_objects", lambda _: [])
    result = pipeline.ingest(
        engine, restricted_file.RestrictedFileAdapter("iso/27001-2022", "ISO 27001",
                                                      "https://www.iso.org/standard/27001"),
        pipeline.LocalStore(tmp_path / "lake"))
    assert result["status"] == "awaiting-artifact"


def test_flag_on_plans_collection_urls_and_leaves_empty_overlays_blocked(engine, monkeypatch):
    _enable(monkeypatch)
    rulebook = next(e for e in source_registry.S if e["key"] == "finra/rulebook")
    adapter = fleet.adapter_for(rulebook)
    assert not getattr(adapter, "declaration_gap", None)
    assert adapter.meta().source_key == "finra/rulebook"
    overlay = next(e for e in source_registry.S if e["key"] == "ovl/be")
    assert fleet.adapter_for(overlay).declaration_gap
    declared = inventory._declared_entries("registered")
    assert "finra/rulebook" in declared
    assert "pci/dss-v4" in declared
    assert "ovl/be" not in declared
    from app.clhear.l1 import cycles
    planned = cycles.plan_sources(engine, "all_publishers")
    assert "finra/rulebook" in {key for keys in planned.values() for key in keys}


def test_restricted_url_fallback_records_publisher_read_and_artifact_review(engine, monkeypatch, tmp_path):
    from app.clhear.l1 import http, pipeline
    from app.clhear.l1.adapters import restricted_file

    _enable(monkeypatch)
    monkeypatch.setenv("CLHEAR_FLEET", "l1")
    get_settings.cache_clear()
    from tests.test_l1_originals import minimal_pdf
    body = minimal_pdf(["1 Scope", "Original unit-test text. Keep exactly 7 fixture records."])
    monkeypatch.setattr(restricted_file, "_list_restricted_objects", lambda _: [])
    monkeypatch.setattr(http, "get", lambda url, **kwargs: body)
    poc_review.apply_private_review(engine, "activate", "poc:completeness-test",
                                    verification_id="completeness-artifact")
    adapter = restricted_file.RestrictedFileAdapter(
        "iso/27001-2022", "ISO 27001", "https://www.iso.org/standard/27001")
    result = pipeline.ingest(engine, adapter, pipeline.LocalStore(tmp_path / "lake"),
                             index_embeddings=False, job_id="completeness-artifact")
    assert result["status"] == "added", result
    check = result["authorized_artifact_check"]
    assert check["method"] == "publisher_url_read" and check["publisher_check_performed"] is True
    with engine.connect() as conn:
        review = conn.execute(sa.select(inventory.artifact_reviews)
                              .order_by(inventory.artifact_reviews.c.id.desc())).mappings().first()
    assert review["source_key"] == "iso/27001-2022"
    assert review["approved"] is True
    assert review["publisher_edition"] == inventory.EXPECTED_EDITIONS["iso/27001-2022"]
    with engine.connect() as conn:
        assert permissions.decision(conn, "iso/27001-2022", "display_public")["allowed"] is True


def test_bootstrap_records_display_public_and_acceptance_is_not_poisoned(engine, monkeypatch):
    from app.clhear import deployment_verification as verification, workers
    from app.clhear.l1 import registry_etoro
    from app.clhear.l1.pipeline import LocalStore

    _enable(monkeypatch)
    monkeypatch.setenv("CLHEAR_FLEET", "l0")
    monkeypatch.setenv("CLHEAR_L1_ONLY", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(registry_etoro, "seed", Mock())
    snapshot = Mock(return_value={"revision": "revision-one", "sha256": "a" * 64, "byte_count": 4096,
                                  "snapshot_uri": "s3://test/webui/private.db", "accepted_release": False,
                                  "source_environment": "authoritative_postgresql"})
    monkeypatch.setitem(workers.HANDLERS, "ViewerSnapshotRequested", snapshot)
    result = verification.run_phase(engine, None, "bootstrap", "completeness-one",
                                    bootstrap={"applied_migrations": [24]})
    assert result["status"] == "succeeded"
    assert result["steps"]["private_completeness"]["display_public"] is True
    with engine.connect() as conn:
        assert permissions.decision(conn, "iso/27001-2022", "display_public")["allowed"] is True
    store = LocalStore(Path("/tmp/clhear-completeness-inventory"))
    first = inventory.run_inventory_audit(engine, store, job_id="completeness-scope", discover=False)
    poc_review.approve_inventory(engine, first["inventory_hash"], verification_id="completeness-scope")
    inventory.run_inventory_audit(engine, store, job_id="completeness-scope-2", discover=False)
    status = inventory.acceptance_status(engine)
    assert "private_completeness_test_not_release_authority" not in status["reasons"]
    review = inventory.inventory_summary(engine)
    assert review["scope_review"]["approved"] is True
    assert review["scope_review"]["approved_by"] == poc_review.POC_APPROVED_BY
