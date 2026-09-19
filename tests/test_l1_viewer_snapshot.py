"""Private candidate viewer snapshots use the worker's allowlisted projection."""
import hashlib
import json
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from app.clhear.db import make_engine
from app.clhear.l1 import inventory, permissions, viewer_snapshot as viewer, workflow
from app.clhear.l1.adapters.base import DocNode, SourceMeta
from app.clhear.l1.models import clauses, doc_nodes, search_units, source_versions
from app.clhear.l1.pipeline import LocalStore, ensure_source, persist_tree
from app.clhear.models import events, eval_runs, runs

KEY = "finra/rule/2210"
MARKER = "UNIQUE-CLOSED-UNIT-TEST-PROVISION-9917"


def corpus(engine, *, key=KEY):
    meta = SourceMeta(family_key="snapshot-test", family_name="Snapshot tests", source_key=key,
                      name="Test source", kind="regulation", issuer="FINRA", jurisdiction="US",
                      license="restricted", canonical_url="https://www.finra.org/rules-guidance/rulebooks/finra-rules/2210", adapter="finra")
    tree = [DocNode(node_type="provision", ref="2210(a)", heading=MARKER + "-heading", raw_text=MARKER,
                    source_fragment="<p>" + MARKER + "</p>")]
    with engine.begin() as conn:
        _, source_id = ensure_source(conn, meta)
        version_id = conn.execute(source_versions.insert().values(source_id=source_id, version_label="test-edition",
                     content_hash=hashlib.sha256(MARKER.encode()).hexdigest(), s3_uri="s3://private/restricted/test.html")
                     .returning(source_versions.c.id)).scalar_one()
        rows = persist_tree(conn, version_id, tree, public_ok=True)
        clause_id = conn.execute(clauses.insert().values(**rows[0]).returning(clauses.c.id)).scalar_one()
        conn.execute(search_units.insert().values(source_id=source_id, source_version_id=version_id,
            clause_id=clause_id, doc_node_id=rows[0]["doc_node_id"], grain="clause", ref="2210(a)", text=MARKER))
    return version_id


def display_grant(engine, *, approved=True, public=False):
    return permissions.record_permission(engine, source_key=KEY,
        permissions={"store": True, "display_internal": not public, "display_public": public},
        evidence_ref="test-only:display-license", approved_by="unit-test-reviewer", approved=approved)


def open_snapshot(path):
    return make_engine(f"sqlite:///{path}")


def test_projection_initializer_refuses_an_existing_database(engine):
    version_id = corpus(engine)
    with pytest.raises(ValueError, match="new empty SQLite"):
        viewer._empty_schema(engine)
    with engine.connect() as conn:
        assert conn.execute(sa.select(source_versions.c.id)).scalar_one() == version_id
        assert MARKER in conn.execute(sa.select(clauses.c.text)).scalar_one()


def test_closed_text_is_removed_without_erasing_metadata_or_authoritative_rows(engine, tmp_path):
    version_id = corpus(engine)
    path = tmp_path / "viewer.db"
    result = viewer.compile_viewer_snapshot(engine, path)
    assert result["source_environment"] == "local_sqlite_test"
    assert result["accepted_release"] is False and result["redacted_source_keys"] == [KEY]
    assert MARKER.encode() not in path.read_bytes()
    target = open_snapshot(path)
    with target.connect() as conn:
        assert conn.execute(sa.select(source_versions.c.id)).scalar_one() == version_id
        assert conn.execute(sa.select(doc_nodes.c.raw_text)).scalar_one() == ""
        assert conn.execute(sa.select(doc_nodes.c.source_fragment)).scalar_one() == ""
        assert conn.execute(sa.select(clauses.c.text)).scalar_one() == ""
        assert conn.execute(sa.select(search_units.c.text)).scalar_one() == ""
    with engine.connect() as conn:
        assert MARKER in conn.execute(sa.select(clauses.c.text)).scalar_one()
    target.dispose()


def test_internal_grant_permits_inspector_text_but_not_public_search(engine, tmp_path):
    corpus(engine)
    grant = display_grant(engine)
    path = tmp_path / "viewer.db"
    viewer.compile_viewer_snapshot(engine, path)
    target = open_snapshot(path)
    with target.connect() as conn:
        assert MARKER in conn.execute(sa.select(clauses.c.text)).scalar_one()
        assert not conn.execute(sa.select(clauses.c.public_ok)).scalar_one()
        assert conn.execute(sa.select(search_units.c.text)).scalar_one() == ""
        assert permissions.decision(conn, KEY, "display_internal")["permission_id"] == grant["id"]
        assert not permissions.decision(conn, KEY, "display_public")["allowed"]
    target.dispose()
    display_grant(engine, approved=False)
    revoked = tmp_path / "revoked.db"
    state = viewer.compile_viewer_snapshot(engine, revoked)
    assert MARKER.encode() not in revoked.read_bytes() and KEY in state["redacted_source_keys"]


def test_public_grant_copies_public_search_and_record(engine, tmp_path):
    corpus(engine)
    display_grant(engine, public=True)
    path = tmp_path / "public-permitted-private-viewer.db"
    viewer.compile_viewer_snapshot(engine, path)
    target = open_snapshot(path)
    with target.connect() as conn:
        assert conn.execute(sa.select(search_units.c.text)).scalar_one() == MARKER
        assert conn.execute(sa.select(clauses.c.public_ok)).scalar_one()
    target.dispose()


def test_auth_prompts_and_lease_secrets_never_enter_snapshot(engine, tmp_path):
    from app.clhear.community_models import users, api_keys
    secrets = ["PRIVATE-MODEL-PROMPT-553", "PRIVATE-LEASE-TOKEN-552", "PRIVATE-EXCEPTION-551", "PRIVATE-API-HASH-550"]
    with engine.begin() as conn:
        user_id = conn.execute(users.insert().values(email="secret-user@example.test", display_name="Private account")
                               .returning(users.c.id)).scalar_one()
        conn.execute(api_keys.insert().values(id="KEY-test", user_id=user_id, app_id="private-app", label="private", prefix="x", secret_hash=secrets[3]))
        conn.execute(runs.insert().values(fleet="l1.finra", trigger="test", inputs={"source": KEY, "prompt": secrets[0]},
                     outputs={"status": "failed", "error": secrets[2], "nested": {"system_prompt": secrets[0]},
                              "stages": [{"stage": "fetch", "ms": 51, "status": "failed"}]}, reasoning=secrets[0], duration_ms=51))
    workflow.ensure_job(engine, "job-test", "finra", "test", "event-test")
    task = workflow.ensure_task(engine, "job-test", KEY)
    with engine.begin() as conn:
        conn.execute(workflow.tasks.update().where(workflow.tasks.c.task_id == task).values(owner_token=secrets[1], error=secrets[2]))
    path = tmp_path / "viewer.db"
    viewer.compile_viewer_snapshot(engine, path)
    assert all(secret.encode() not in path.read_bytes() for secret in secrets)
    target = open_snapshot(path)
    with target.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(users)).scalar_one() == 0
        assert conn.execute(sa.select(sa.func.count()).select_from(api_keys)).scalar_one() == 0
        assert conn.execute(sa.select(workflow.tasks.c.owner_token)).scalar_one() is None
        output = conn.execute(sa.select(runs.c.outputs)).scalar_one()
        assert output["status"] == "failed" and output["stages"][0]["ms"] == 51
    target.dispose()


def test_inventory_and_workflow_tables_and_empty_preview_schema_are_present(engine, tmp_path):
    from app.clhear.derived_models import obligations
    audit = inventory.run_inventory_audit(engine, LocalStore(tmp_path / "originals"), job_id="inventory-test", scope="finra")
    path = tmp_path / "viewer.db"
    state = viewer.compile_viewer_snapshot(engine, path)
    target = open_snapshot(path)
    with target.connect() as conn:
        assert conn.execute(sa.select(inventory.inventory_audits.c.id)).scalar_one() == audit["audit_id"]
        assert conn.execute(sa.select(sa.func.count()).select_from(obligations)).scalar_one() == 0
        assert all(sa.inspect(conn).has_table(t.name) for t in viewer.EVIDENCE_TABLES)
    assert viewer.read_viewer_state(target) == state
    assert state["omitted_layers"] == ["L2", "L3", "L4", "L5", "L6", "L7", "L8"]
    target.dispose()


def test_deployment_evidence_is_visible_without_other_l0_private_outputs(engine, tmp_path):
    result = {"verification_id": "l1-test-1", "phase": "publish", "status": "succeeded",
              "evidence_mode": "manual_deployment_verification", "nightly_schedule_validation": "pending",
              "duration_ms": 71, "secret": "DO-NOT-COPY-DEPLOYMENT-SECRET"}
    with engine.begin() as conn:
        conn.execute(runs.insert().values(fleet="l0.deployment_verification", trigger="publish", outputs=result))
        conn.execute(runs.insert().values(fleet="l0.private_admin", trigger="private", outputs={"secret": "PRIVATE-L0-OUTPUT"}))
    path = tmp_path / "deployment-viewer.db"
    viewer.compile_viewer_snapshot(engine, path)
    target = open_snapshot(path)
    with target.connect() as conn:
        row = conn.execute(sa.select(runs.c.fleet, runs.c.outputs)).one()
        assert row.fleet == "l0.deployment_verification"
        assert row.outputs == {key: value for key, value in result.items() if key != "secret"}
    assert b"PRIVATE-L0-OUTPUT" not in path.read_bytes()
    assert b"DO-NOT-COPY-DEPLOYMENT-SECRET" not in path.read_bytes()
    target.dispose()


def test_missing_evidence_migration_aborts_export_and_preserves_last_good(engine, tmp_path):
    with engine.begin() as conn:
        inventory.artifact_reviews.drop(conn)
    path = tmp_path / "candidate.db"
    with pytest.raises(RuntimeError, match="migrated evidence"):
        viewer.compile_viewer_snapshot(engine, path)
    assert not path.exists()
    previous = tmp_path / "previous.db"
    previous.write_bytes(b"last-good")
    with pytest.raises(FileExistsError):
        viewer.compile_viewer_snapshot(engine, previous)
    assert previous.read_bytes() == b"last-good"


def test_worker_publication_uses_private_atomic_object_and_never_changes_database_setting(engine, monkeypatch):
    previous = b"last-good"
    class Store:
        value = previous
        request = None
        fail = False
        def head_object(self, **kwargs):
            return {"ETag": '"test-etag"'}
        def put_object(self, **kwargs):
            self.request = kwargs
            body = kwargs["Body"].read()
            if self.fail:
                raise RuntimeError("upload failed")
            self.value = body
    store = Store()
    result = viewer.publish_viewer_snapshot(engine, "s3://private-bucket/webui/clhear-latest.db", "us-east-1", s3_client=store)
    assert store.value.startswith(b"SQLite format 3") and result["accepted_release"] is False
    assert store.request["ServerSideEncryption"] == "AES256" and store.request["CacheControl"] == "private, no-store"
    assert store.request["IfMatch"] == '"test-etag"'
    assert result["sha256"] == hashlib.sha256(store.value).hexdigest()
    last_good = store.value
    store.fail = True
    with pytest.raises(RuntimeError, match="upload failed"):
        viewer.publish_viewer_snapshot(engine, "s3://private-bucket/webui/clhear-latest.db", "us-east-1", s3_client=store)
    assert store.value == last_good


def test_refresh_requests_older_than_the_published_compile_are_coalesced(engine):
    """52 queued refreshes must not mean 52 rebuilds of a 2 GB projection."""
    class Store:
        compiled = None
        puts = 0
        def head_object(self, **kwargs):
            metadata = {"revision": "rev-1", "sha256": "ab" * 32}
            if self.compiled:
                metadata["compiled-from"] = self.compiled
            return {"ETag": '"etag"', "Metadata": metadata}
        def put_object(self, **kwargs):
            self.puts += 1
            kwargs["Body"].read()
            self.compiled = kwargs["Metadata"]["compiled-from"]
    store = Store()
    uri = "s3://private/webui/latest.db"
    early = "2026-09-18T20:00:00+00:00"
    first = viewer.publish_viewer_snapshot(engine, uri, "us-east-1", s3_client=store, requested_at=early)
    assert store.puts == 1 and first["sha256"] and store.compiled
    again = viewer.publish_viewer_snapshot(engine, uri, "us-east-1", s3_client=store, requested_at=early)
    assert again["status"] == "coalesced" and again["revision"] == "rev-1" and store.puts == 1
    # A request newer than the last compile, or without a timestamp, still compiles.
    later = datetime.now(timezone.utc).isoformat()
    assert viewer.publish_viewer_snapshot(engine, uri, "us-east-1", s3_client=store, requested_at=later)["sha256"]
    assert store.puts == 2
    assert viewer.publish_viewer_snapshot(engine, uri, "us-east-1", s3_client=store)["sha256"] and store.puts == 3


def test_review_and_audit_commands_request_l0_viewer_refresh(engine, monkeypatch):
    from app.clhear.workers import handle_envelope, WrongFleet
    monkeypatch.setenv("CLHEAR_VIEWER_SNAPSHOT_S3_URI", "s3://private/webui/clhear-latest.db")
    def command(kind, payload, event):
        return json.dumps({"event_id": event, "kind": kind, "layer": "l1", "subject_ref": "test",
                           "producer": "test-operator", "ts": datetime.now(timezone.utc).isoformat(), "payload": payload})
    monkeypatch.setenv("CLHEAR_FLEET", "l1")
    handle_envelope(engine, None, command("L1InventoryAuditRequested", {"scope": "finra"}, "audit-command"))
    with pytest.raises(WrongFleet):
        handle_envelope(engine, None, command("ViewerSnapshotRequested", {}, "wrong-fleet"))
    monkeypatch.setenv("CLHEAR_FLEET", "l0")
    handle_envelope(engine, None, command("L1EvidenceReviewRecorded", {
        "review_kind": "permissions", "source_key": KEY, "permissions": {}, "approved": False,
        "approved_by": "unit-test-reviewer", "evidence_ref": "test-only:revocation"}, "review-command"))
    with engine.connect() as conn:
        requests = list(conn.execute(sa.select(events).where(events.c.kind == "ViewerSnapshotRequested")).mappings())
    assert len(requests) == 2
    assert {row["payload"]["reason"] for row in requests} == {"inventory_audit_finished", "source_evidence_updated"}
    monkeypatch.setattr(viewer, "publish_viewer_snapshot", lambda *a, **kw: {"kind": "candidate_viewer", "accepted_release": False})
    result = handle_envelope(engine, None, command("ViewerSnapshotRequested", {"uri": "s3://attacker/public/x"}, "right-fleet"))
    assert result["kind"] == "candidate_viewer"


def test_direct_database_is_not_labelled_as_published_viewer(engine):
    state = viewer.read_viewer_state(engine)
    assert state["viewer_snapshot"] is False and state["source_environment"] == "local_sqlite"


class ConditionalStore:
    def __init__(self, value=b"last-good", etag='"previous"'):
        self.value, self.etag, self.put_calls = value, etag, 0

    def head_object(self, **kwargs):
        from botocore.exceptions import ClientError
        if self.etag is None:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        return {"ETag": self.etag}

    def put_object(self, **kwargs):
        from botocore.exceptions import ClientError
        self.put_calls += 1
        if (("IfMatch" in kwargs and kwargs["IfMatch"] != self.etag)
                or (kwargs.get("IfNoneMatch") == "*" and self.etag is not None)):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.value = kwargs["Body"].read()
        self.etag = '"new"'


def test_revocation_during_compilation_aborts_before_snapshot_replacement(engine, tmp_path, monkeypatch):
    corpus(engine)
    display_grant(engine)
    original = viewer.compile_viewer_snapshot
    def revoke_after_compile(*args, **kwargs):
        result = original(*args, **kwargs)
        display_grant(engine, approved=False)
        return result
    monkeypatch.setattr(viewer, "compile_viewer_snapshot", revoke_after_compile)
    store = ConditionalStore()
    with pytest.raises(PermissionError, match="permissions changed"):
        viewer.publish_viewer_snapshot(engine, "s3://private/webui/latest.db", "us-east-1", s3_client=store)
    assert store.value == b"last-good" and store.put_calls == 0


def test_slow_export_cannot_overwrite_newer_viewer(engine, monkeypatch):
    from botocore.exceptions import ClientError
    store = ConditionalStore()
    original = viewer.compile_viewer_snapshot
    def race_after_compile(*args, **kwargs):
        result = original(*args, **kwargs)
        store.value, store.etag = b"newer-viewer", '"newer-etag"'
        return result
    monkeypatch.setattr(viewer, "compile_viewer_snapshot", race_after_compile)
    with pytest.raises(ClientError, match="PreconditionFailed"):
        viewer.publish_viewer_snapshot(engine, "s3://private/webui/latest.db", "us-east-1", s3_client=store)
    assert store.value == b"newer-viewer"


def test_first_viewer_uses_create_only_upload(engine):
    store = ConditionalStore(etag=None)
    result = viewer.publish_viewer_snapshot(engine, "s3://private/webui/latest.db", "us-east-1", s3_client=store)
    assert result["kind"] == "candidate_viewer" and store.value.startswith(b"SQLite format 3")


def test_redaction_covers_annotations_changes_and_free_form_shared_context(engine, tmp_path):
    from app.clhear.l1.models import clause_annotations, change_events, rights_records, sources, citations
    version_id = corpus(engine)
    with engine.begin() as conn:
        clause_id = conn.execute(sa.select(clauses.c.id)).scalar_one()
        source_id = conn.execute(sa.select(sources.c.id)).scalar_one()
        conn.execute(clause_annotations.insert().values(clause_id=clause_id, origin="llm", summary=MARKER, topics=[MARKER]))
        conn.execute(change_events.insert().values(source_id=source_id, kind="added", new_version="test-edition", clause_refs=[MARKER]))
        conn.execute(rights_records.update().values(basis_ref=MARKER))
        conn.execute(citations.insert().values(from_clause_id=clause_id, raw_text=MARKER, reason=MARKER))
        conn.execute(runs.insert().values(fleet="l1.test", trigger="test", outputs={"detail": MARKER}))
        conn.execute(source_versions.update().where(source_versions.c.id == version_id).values(
            model_manifest={"provider_prompt": MARKER}, review={"detail": MARKER}))
    path = tmp_path / "redacted.db"
    viewer.compile_viewer_snapshot(engine, path)
    assert MARKER.encode() not in path.read_bytes()
    target = open_snapshot(path)
    with target.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(clause_annotations)).scalar_one() == 0
        assert conn.execute(sa.select(change_events.c.clause_refs)).scalar_one() == []
        assert conn.execute(sa.select(rights_records.c.basis_ref)).scalar_one() == ""
        assert conn.execute(sa.select(citations.c.reason)).scalar_one() == ""
    target.dispose()
