"""Operational audit/review paths are CLHEAR worker commands, not scripts."""
import json
from datetime import datetime, timezone

import sqlalchemy as sa
import pytest

from app.clhear.l1 import inventory, permissions
from app.clhear.workers import handle_envelope
from app.clhear.models import events


def envelope(kind, payload, event_id="test-command"):
    return json.dumps({"event_id": event_id, "kind": kind, "layer": "l1", "subject_ref": "finra",
                       "producer": "test-operator", "ts": datetime.now(timezone.utc).isoformat(), "payload": payload})


def test_audit_command_records_missing_corpus_without_fetching(engine, monkeypatch):
    from app.clhear.l1 import http
    monkeypatch.setenv("CLHEAR_FLEET", "l1")
    monkeypatch.setattr(http, "get", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("audit acquired text")))
    body = envelope("L1InventoryAuditRequested", {"scope": "finra", "discover": False})
    result = handle_envelope(engine, None, body)
    assert result["known_expected"] > 0 and result["verified"] == 0
    assert result["status"] == "gaps" and result["discovery_complete"] is False
    assert handle_envelope(engine, None, body) is None
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(inventory.inventory_audits)).scalar() == 1


def test_artifact_review_cannot_grant_source_permissions(engine, monkeypatch):
    monkeypatch.setenv("CLHEAR_FLEET", "l0")
    payload = {"review_kind": "artifact", "source_key": "iso/27001-2022", "content_hash": "a" * 64,
               "publisher_edition": "ISO/IEC 27001:2022", "canonical_url": "https://www.iso.org/standard/27001",
               "coverage": "full", "evidence_ref": "test-evidence", "approved_by": "test-reviewer", "approved": True}
    result = handle_envelope(engine, None, envelope("L1EvidenceReviewRecorded", payload))
    assert result["requires_new_audit"] is True
    with engine.connect() as conn:
        assert permissions.decision(conn, "iso/27001-2022", "parse")["allowed"] is False


def test_review_rolls_back_when_viewer_outbox_emission_fails(engine, monkeypatch):
    from app.clhear.platform import events as event_service
    monkeypatch.setenv("CLHEAR_FLEET", "l0")
    monkeypatch.setenv("CLHEAR_VIEWER_SNAPSHOT_S3_URI", "s3://private/webui/latest.db")
    source_key = "finra/rule/2210"
    permissions.record_permission(engine, source_key=source_key,
        permissions={"store": True, "display_internal": True}, evidence_ref="test-only:approval",
        approved_by="test-reviewer", approved=True)
    payload = {"review_kind": "permissions", "source_key": source_key,
               "permissions": {}, "evidence_ref": "test-only:revocation", "approved_by": "test-reviewer", "approved": False}
    def fail(*args, **kwargs):
        raise RuntimeError("test outbox failure")
    monkeypatch.setattr(event_service, "emit", fail)
    with pytest.raises(RuntimeError, match="test outbox failure"):
        handle_envelope(engine, None, envelope("L1EvidenceReviewRecorded", payload, "failed-review"))
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(permissions.source_permissions)).scalar_one() == 1
        assert permissions.decision(conn, source_key, "display_internal")["allowed"] is True
        assert conn.execute(sa.select(sa.func.count()).select_from(events).where(events.c.kind == "ViewerSnapshotRequested")).scalar_one() == 0


def test_review_and_exact_viewer_request_commit_together(engine, monkeypatch):
    monkeypatch.setenv("CLHEAR_FLEET", "l0")
    monkeypatch.setenv("CLHEAR_VIEWER_SNAPSHOT_S3_URI", "s3://private/webui/latest.db")
    payload = {"review_kind": "permissions", "source_key": "finra/rule/2210",
               "permissions": {}, "evidence_ref": "test-only:revocation", "approved_by": "test-reviewer", "approved": False}
    body = envelope("L1EvidenceReviewRecorded", payload, "successful-review")
    result = handle_envelope(engine, None, body)
    assert result["record"]["approved"] is False
    assert handle_envelope(engine, None, body) is None
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(permissions.source_permissions)).scalar_one() == 1
        row = conn.execute(sa.select(events).where(events.c.kind == "ViewerSnapshotRequested")).mappings().one()
        assert row["layer"] == "l0" and row["subject_ref"] == "viewer/current"
        assert row["payload"] == {"reason": "source_evidence_updated", "job_id": None}
