"""Read routes expose exact worker evidence without running imports or evals."""
import json

import sqlalchemy as sa

from app.clhear.l1 import inventory, workflow
from app.clhear.l1.models import source_versions
from tests.test_l1_inventory import ENTRY, KEY, grant, imported
from app.clhear.l1.pipeline import LocalStore


def _audit(engine, tmp_path, monkeypatch, *, import_source=True):
    monkeypatch.setattr(inventory, "_declared_entries", lambda scope: {KEY: dict(ENTRY)})
    store = LocalStore(tmp_path / "originals")
    if import_source:
        grant(engine)
        imported(engine, store)
    return inventory.run_inventory_audit(engine, store, job_id="test-only-audit", scope="finra")


def test_inventory_api_is_read_only_and_returns_worker_denominator(engine, client, tmp_path, monkeypatch):
    audit = _audit(engine, tmp_path, monkeypatch)
    statements = []
    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split()[0].upper())
    sa.event.listen(engine, "before_cursor_execute", observe)
    try:
        response = client.get("/api/clhear/l1/inventory?scope=finra")
        source = client.get(f"/api/clhear/sources/{KEY}/inventory")
        evidence = client.get(f"/api/clhear/sources/{KEY}/evals")
    finally:
        sa.event.remove(engine, "before_cursor_execute", observe)
    assert response.status_code == source.status_code == evidence.status_code == 200
    assert response.json()["known_expected"] == 1
    assert response.json()["audit_id"] == audit["audit_id"]
    assert source.json()["source_version_id"] == audit["sources"][0]["source_version_id"]
    assert evidence.json()["inventory_matches_selected_version"] is True
    assert evidence.json()["publisher_checked_at"] == source.json()["publisher_checked_at"]
    assert not {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"}.intersection(statements)
    assert "Test firms must keep test records" not in response.text + source.text + evidence.text


def test_audit_cannot_supply_freshness_for_changed_artifact(engine, client, tmp_path, monkeypatch):
    audit = _audit(engine, tmp_path, monkeypatch)
    with engine.begin() as conn:
        conn.execute(source_versions.update().where(source_versions.c.id == audit["sources"][0]["source_version_id"])
                     .values(content_hash="different-artifact"))
    evidence = client.get(f"/api/clhear/sources/{KEY}/evals").json()
    assert evidence["inventory_matches_selected_version"] is False
    assert evidence["publisher_checked_at"] is None
    assert evidence["inventory"]["current_binding_valid"] is False


def test_expected_but_unimported_source_has_a_metadata_view(engine, client, tmp_path, monkeypatch):
    _audit(engine, tmp_path, monkeypatch, import_source=False)
    detail = client.get(f"/api/clhear/sources/{KEY}")
    document = client.get(f"/api/clhear/sources/{KEY}/document")
    assert detail.status_code == document.status_code == 200
    assert detail.json()["inventory"]["verified"] is False
    codes = {finding["code"] for finding in detail.json()["inventory"]["findings"]}
    assert {"permission_unverified", "awaiting_artifact"} <= codes
    assert document.json()["version"] is None and document.json()["nodes"] == []
    assert client.get(f"/api/clhear/sources/{KEY}/document?version_label=imaginary").status_code == 404
    assert client.get("/api/clhear/sources/unknown/source").status_code == 404


def test_new_read_apis_report_missing_migrations_as_unavailable(engine, client):
    inventory.inventory_audits.drop(engine)
    workflow.jobs.drop(engine)
    inv = client.get("/api/clhear/l1/inventory").json()
    execution = client.get("/api/clhear/l1/workflow").json()
    assert inv["status"] == execution["status"] == "unavailable"
    assert inv["full_scope_verified"] is False
    assert execution["jobs"] == execution["tasks"] == execution["steps"] == []


def test_workflow_api_filters_exact_job_and_preserves_measured_attempts(engine, client):
    workflow.ensure_job(engine, "job-one", "test", "manual", "event-one")
    task = workflow.ensure_task(engine, "job-one", "test/document")
    token = workflow.claim_task(engine, task)
    with workflow.bind_execution(engine, "job-one", task, token):
        with workflow.stage("permission"):
            pass
        with workflow.stage("acquisition_parse"):
            pass
    workflow.ensure_job(engine, "job-other", "test", "manual", "event-other")
    workflow.ensure_task(engine, "job-other", "test/other")
    report = client.get("/api/clhear/l1/workflow?job_id=job-one&source_key=test/document").json()
    assert [job["job_id"] for job in report["jobs"]] == ["job-one"]
    assert [record["task_id"] for record in report["tasks"]] == [task]
    assert report["steps"][1]["depends_on"] == [report["steps"][0]["step_id"]]
    assert all(step["duration_ms"] is not None for step in report["steps"])
    assert token not in json.dumps(report)


def test_homepage_reads_same_inventory_and_keeps_downstream_preview(engine, client, tmp_path, monkeypatch):
    _audit(engine, tmp_path, monkeypatch, import_source=False)
    layers = {row["layer"]: row for row in client.get("/api/clhear/layers").json()["layers"]}
    overview = layers["L1"]["overview"]
    assert overview["inventory"]["known_expected"] == 1
    assert overview["verification"] == "not_evaluated"  # registered scope has no audit
    assert layers["L2"]["overview"]["state"] == "published"
    assert layers["L1"]["overview"]["state"] == "published"
    assert "preview" not in (layers["L2"]["overview"].get("notice") or "").lower()


def test_viewer_origin_marks_omitted_layers_unavailable(engine, client, monkeypatch):
    from app.clhear.l1 import viewer_snapshot

    state = {"status": "available", "viewer_snapshot": True, "kind": "candidate_viewer",
             "revision": "test-only-revision", "source_environment": "local_sqlite_test",
             "database_backend": "sqlite", "generated_at": "2026-09-15T00:00:00Z",
             "omitted_layers": [f"L{i}" for i in range(2, 9)], "redacted_source_keys": [],
             "accepted_release": False, "counts": {}}
    monkeypatch.setattr(viewer_snapshot, "read_viewer_state", lambda engine: state)
    assert client.get("/api/clhear/viewer-snapshot").json() == state
    layers = {row["layer"]: row for row in client.get("/api/clhear/layers").json()["layers"]}
    assert layers["L1"]["overview"]["viewer_snapshot"] == state
    for layer in state["omitted_layers"]:
        assert layers[layer]["overview"]["state"] == "published"
        assert layers[layer]["overview"].get("available_in_projection") is not False


def test_pages_get_the_authorization_binding_as_a_digest_not_the_full_list(engine, client, monkeypatch):
    import hashlib

    from app.clhear.l1 import viewer_snapshot

    binding = [{"source_key": f"synthetic/rule/{n}", "protected": False, "decisions": {}} for n in range(3)]
    state = {"status": "available", "viewer_snapshot": True, "revision": "test-only-revision",
             "omitted_layers": [], "authorization_binding": binding}
    monkeypatch.setattr(viewer_snapshot, "read_viewer_state", lambda engine: state)
    digest = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    summary = {"sources": 3, "sha256": digest}
    assert client.get("/api/clhear/viewer-snapshot").json()["authorization_binding"] == summary
    layers = {row["layer"]: row for row in client.get("/api/clhear/layers").json()["layers"]}
    assert layers["L1"]["overview"]["viewer_snapshot"]["authorization_binding"] == summary
    assert state["authorization_binding"] is binding


def test_evidence_read_endpoints_keep_restricted_access(engine, client, monkeypatch):
    from app.clhear.settings import get_settings

    monkeypatch.setenv("CLHEAR_RESTRICTED_ACCESS", "true")
    monkeypatch.setenv("CLHEAR_AUTH_DEBUG", "false")
    monkeypatch.setenv("CLHEAR_SESSION_SECRET", "test-only-evidence-access-secret-at-least-32-characters")
    get_settings.cache_clear()
    for path in ("/api/clhear/viewer-snapshot", "/api/clhear/l1/inventory", "/api/clhear/l1/workflow",
                 f"/api/clhear/sources/{KEY}/inventory"):
        response = client.get(path)
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
