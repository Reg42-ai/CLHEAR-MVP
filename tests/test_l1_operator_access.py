"""Private exception text needs both signed reviewer and current L0 control."""
import io
import json

import pytest
import sqlalchemy as sa
from botocore.exceptions import ClientError

from app.clhear.l1 import operator_access as access, operator_exceptions as oe, permissions, viewer_snapshot as viewer
from app.clhear.l1.models import clauses, doc_nodes, search_units
from tests.test_l1_operator_exceptions import activate, bind, KEY, URL
from tests.test_l1_viewer_snapshot import corpus, MARKER, open_snapshot, display_grant
from tests.test_review_access import restricted_settings, restricted_client, _session

URI = "s3://private-bucket/webui/l1/candidate.db"
REGION = "us-east-1"


class S3:
    """Atomic object fixture: failed preconditions never mutate, reads are fresh."""
    def __init__(self):
        self.objects, self.calls = {}, []
        self.error = None
        self.serial = 0

    def get_object(self, **kw):
        self.calls.append(("get", kw))
        if self.error:
            raise ClientError({"Error": {"Code": self.error}}, "GetObject")
        value = self.objects.get((kw["Bucket"], kw["Key"]))
        if value is None:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(value[0]), "ETag": value[1]}

    def head_object(self, **kw):
        value = self.get_object(**kw)
        value["Body"].close()
        return {"ETag": value["ETag"]}

    def put_object(self, **kw):
        self.calls.append(("put", {k: v for k, v in kw.items() if k != "Body"}))
        key = (kw["Bucket"], kw["Key"])
        current = self.objects.get(key)
        if ((kw.get("IfNoneMatch") == "*" and current is not None)
                or ("IfMatch" in kw and (current is None or current[1] != kw["IfMatch"]))):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        body = kw["Body"].read() if hasattr(kw["Body"], "read") else kw["Body"]
        self.serial += 1
        etag = f'"object-{self.serial}"'
        self.objects[key] = (body, etag)
        return {"ETag": etag}


def choice(engine):
    with engine.connect() as conn:
        return permissions.candidate_decision(conn, KEY, "display_internal", canonical_url=URL)


def current(engine, s3, **kw):
    return access.verify_current_access(choice(engine), snapshot_uri=URI, region=REGION, s3_client=s3, **kw)


def initialized(engine):
    bound = bind(engine)
    s3 = S3()
    control = access.publish_control(engine, URI, REGION, s3_client=s3)
    return bound, s3, control


def test_current_binding_read_has_no_freshness_cache_and_unrelated_additions_do_not_revoke(engine):
    bound, s3, first = initialized(engine)
    assert current(engine, s3)["allowed"]
    assert current(engine, s3)["allowed"]
    reads = [item for item in s3.calls if item[0] == "get"]
    assert len(reads) == 3  # publisher's initial miss plus every viewer read
    bind(engine, source_key="finra/rule/3110", canonical_url=URL.replace("2210", "3110"))
    next_control = access.publish_control(engine, URI, REGION, s3_client=s3)
    assert next_control["ledger_digest"] != first["ledger_digest"]
    result = current(engine, s3)
    assert result["allowed"] and result["binding_id"] == bound["id"]
    assert result["control_revision"] == next_control["revision"]
    assert result["display_label"] == access.LABEL and not result["release_eligible"]
    assert not result["publisher_permission_verified"]


@pytest.mark.parametrize("failure", ["NoSuchKey", "AccessDenied", "RequestTimeout"])
def test_current_control_failure_never_reuses_prior_authorization(engine, failure):
    _, s3, _ = initialized(engine)
    assert current(engine, s3)["allowed"]
    s3.error = failure
    assert current(engine, s3)["reason"] == "operator_control_unavailable"
    assert not current(engine, s3)["allowed"]


def test_old_snapshot_is_denied_immediately_on_invalidation_and_after_revocation(engine, tmp_path):
    _, s3, _ = initialized(engine)
    corpus(engine)
    path = tmp_path / "old.db"
    viewer.compile_viewer_snapshot(engine, path)
    old = open_snapshot(path)
    stale_choice = choice(old)
    assert stale_choice["allowed"]
    token = access.invalidate_control(URI, REGION, s3_client=s3)["invalidation_token"]
    assert not access.verify_current_access(stale_choice, snapshot_uri=URI, s3_client=s3)["allowed"]
    with pytest.raises(RuntimeError, match="pending ledger mutation"):
        access.publish_control(engine, URI, REGION, s3_client=s3)
    activate(engine, action="revoke", command_id="owner-revoke")
    control = access.publish_control(engine, URI, REGION, s3_client=s3, invalidation_token=token)
    assert control["binding_count"] == 0
    denied = access.verify_current_access(stale_choice, snapshot_uri=URI, s3_client=s3)
    assert denied["reason"] == "operator_control_binding_revoked_or_changed"
    assert not denied["allowed"] and choice(old)["allowed"]  # Historical ledger is deliberately unchanged.
    old.dispose()


def test_failed_ledger_mutation_keeps_access_denied_until_matching_invalidation_token(engine):
    _, s3, _ = initialized(engine)
    first = access.invalidate_control(URI, REGION, s3_client=s3, mutation_id="durable-command-1")
    second = access.invalidate_control(URI, REGION, s3_client=s3, mutation_id="durable-command-1")
    assert first == second
    with pytest.raises(RuntimeError, match="different operator ledger mutation"):
        access.invalidate_control(URI, REGION, s3_client=s3, mutation_id="another-command")
    with pytest.raises(RuntimeError, match="pending"):
        access.publish_control(engine, URI, REGION, s3_client=s3, invalidation_token="wrong-token")
    assert not current(engine, s3)["allowed"]
    access.publish_control(engine, URI, REGION, s3_client=s3, invalidation_token=first["invalidation_token"])
    assert current(engine, s3)["allowed"]


def test_binding_revision_and_reactivation_cannot_authorize_old_snapshot_choice(engine, tmp_path):
    _, s3, _ = initialized(engine)
    corpus(engine)
    path = tmp_path / "old-binding.db"
    viewer.compile_viewer_snapshot(engine, path)
    snapshot = open_snapshot(path)
    original = choice(snapshot)
    assert access.verify_current_access(original, snapshot_uri=URI, s3_client=s3)["allowed"]
    replacement = bind(engine, manifest_hash="b" * 64)
    report = access.publish_control(engine, URI, REGION, s3_client=s3)
    assert not access.verify_current_access(original, snapshot_uri=URI, s3_client=s3)["allowed"]
    assert choice(snapshot)["allowed"]  # Old ledger remains intact; current control is decisive.
    assert current(engine, s3)["binding_id"] == replacement["id"] and current(engine, s3)["allowed"]
    assert report["binding_count"] == 1
    with engine.connect() as conn:
        ledger = oe.control_state(conn)
        assert len(ledger["bindings"]) == 2 and ledger["digest"] == report["ledger_digest"]
    snapshot.dispose()
    activate(engine, action="revoke", command_id="revoke")
    again = activate(engine, command_id="reactivate")
    bind(engine, again)
    access.publish_control(engine, URI, REGION, s3_client=s3)
    assert not access.verify_current_access(original, snapshot_uri=URI, s3_client=s3)["allowed"]
    assert current(engine, s3)["allowed"]


def test_control_integrity_and_wrong_exact_source_cannot_authorize(engine):
    _, s3, _ = initialized(engine)
    key = next(iter(s3.objects))
    raw, etag = s3.objects[key]
    value = json.loads(raw)
    value["bindings"][str(choice(engine)["binding_id"])] = "f" * 64
    s3.objects[key] = (json.dumps(value).encode(), etag)
    assert not current(engine, s3)["allowed"]
    value["sha256"] = access._hash({k: v for k, v in value.items() if k != "sha256"})
    s3.objects[key] = (json.dumps(value).encode(), etag)
    assert current(engine, s3)["reason"] == "operator_control_binding_revoked_or_changed"


def test_public_model_export_choices_never_use_exception_control(engine):
    _, s3, _ = initialized(engine)
    with engine.connect() as conn:
        for op in ("display_public", "infer", "derive", "translate", "export"):
            # Unsupported operation isn't even a permission: use a forged display
            # choice to prove the private control helper refuses other operations.
            assert not access.verify_current_access({**choice(engine), "operation": op}, snapshot_uri=URI, s3_client=s3)["allowed"]
            if op in permissions.OPERATIONS:
                assert not permissions.candidate_decision(conn, KEY, op)["allowed"]
    assert [call[0] for call in s3.calls] == ["get", "put"]


def test_exception_snapshot_copies_private_text_ledger_and_control_provenance_only(engine, tmp_path):
    corpus(engine)
    bind(engine)
    s3 = S3()
    result = viewer.publish_viewer_snapshot(engine, URI, REGION, s3_client=s3)
    puts = [call[1] for call in s3.calls if call[0] == "put"]
    assert [put["Key"] for put in puts] == ["webui/l1/operator-access-control.json", "webui/l1/candidate.db"]
    assert all(put["ServerSideEncryption"] == "AES256" and put["CacheControl"] == "private, no-store" for put in puts)
    path = tmp_path / "published.db"
    path.write_bytes(s3.objects[("private-bucket", "webui/l1/candidate.db")][0])
    snapshot = open_snapshot(path)
    with snapshot.connect() as conn:
        assert MARKER in conn.execute(sa.select(doc_nodes.c.raw_text)).scalar_one()
        assert conn.execute(sa.select(doc_nodes.c.public_ok)).scalar_one() is False
        assert conn.execute(sa.select(clauses.c.public_ok)).scalar_one() is False
        assert conn.execute(sa.select(search_units.c.text)).scalar_one() == ""
        assert conn.exec_driver_sql("SELECT count(*) FROM search_units_fts WHERE search_units_fts MATCH 'UNIQUE'").scalar_one() == 0
        assert oe.control_state(conn)["digest"] == result["operator_control"]["ledger_digest"]
        assert all(table.name in result["table_allowlist"] for table in oe.TABLES)
    assert result["operator_control"]["published"] and not result["accepted_release"]
    assert result["authorization_binding"][0]["decisions"]["display_internal"]["authority_type"] == "operator_exception"
    snapshot.dispose()


def test_signed_reviewer_reads_each_text_surface_but_revoked_control_denies_stale_database(engine, restricted_client, monkeypatch):
    corpus(engine)
    _, s3, _ = initialized(engine)
    monkeypatch.setenv("CLHEAR_DB_S3_URI", URI)
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: s3)
    with engine.connect() as conn:
        node_id = conn.execute(sa.select(doc_nodes.c.id)).scalar_one()
    paths = [f"/api/clhear/sources/{KEY}/document", f"/api/clhear/sources/{KEY}/clauses", f"/api/clhear/nodes/{node_id}"]
    for path in paths:
        response = restricted_client.get(path)
        assert response.status_code == 401 and MARKER not in response.text
    _session(restricted_client)
    for path in paths:
        response = restricted_client.get(path)
        assert response.status_code == 200 and MARKER in response.text
        assert response.json()["access"]["display_label"] == access.LABEL
        assert response.headers["cache-control"] == "private, no-store"
    # No DB change: this models a viewer with an old SQLite snapshot after the
    # owner has revoked at the authoritative ledger/control boundary.
    access.invalidate_control(URI, REGION, s3_client=s3)
    for path in paths:
        response = restricted_client.get(path)
        assert response.status_code == 200 and MARKER not in response.text
        assert response.json()["locked"] is True


def test_normal_publisher_grant_does_not_require_operator_control(engine, restricted_client, monkeypatch):
    corpus(engine)
    display_grant(engine)
    monkeypatch.setattr("boto3.client", lambda *a, **kw: pytest.fail("No exception control needed"))
    _session(restricted_client)
    response = restricted_client.get(f"/api/clhear/sources/{KEY}/document")
    assert response.status_code == 200 and MARKER in response.text
    assert response.json()["access"].get("authority_type") != "operator_exception"


def test_missing_control_configuration_fails_closed_for_private_exception(engine, monkeypatch):
    bind(engine)
    monkeypatch.delenv("CLHEAR_DB_S3_URI", raising=False)
    from app.clhear.settings import get_settings
    monkeypatch.setenv("CLHEAR_PREVIEW_SNAPSHOT_S3_URI", "")
    get_settings.cache_clear()
    assert access.verify_current_access(choice(engine))["reason"] == "operator_control_not_configured"
    monkeypatch.setenv("AWS_EXECUTION_ENV", "AWS_ECS_FARGATE")
    monkeypatch.delenv("CLHEAR_VIEWER_SNAPSHOT_S3_URI", raising=False)
    with pytest.raises(RuntimeError, match="Production operator control"):
        access.invalidate_configured_control()


def test_concurrent_invalidation_cannot_be_overwritten_by_older_publish(engine, monkeypatch):
    _, s3, _ = initialized(engine)
    original = oe.control_state
    intervening = []

    def read_then_revoke(conn):
        value = original(conn)
        intervening.append(access.invalidate_control(URI, REGION, s3_client=s3, mutation_id="revocation-command"))
        return value

    monkeypatch.setattr(oe, "control_state", read_then_revoke)
    with pytest.raises(ClientError) as failure:
        access.publish_control(engine, URI, REGION, s3_client=s3)
    assert failure.value.response["Error"]["Code"] == "PreconditionFailed"
    assert current(engine, s3)["reason"] == "operator_control_updating"
    record = json.loads(next(iter(s3.objects.values()))[0])
    assert record["revision"] == intervening[0]["revision"]


def test_publisher_does_not_treat_denied_read_as_missing_control(engine):
    _, s3, _ = initialized(engine)
    original = dict(s3.objects)
    s3.error = "AccessDenied"
    with pytest.raises(ClientError):
        access.invalidate_control(URI, REGION, s3_client=s3, mutation_id="command")
    with pytest.raises(ClientError):
        access.publish_control(engine, URI, REGION, s3_client=s3)
    assert s3.objects == original


def test_current_binding_map_is_compact_and_publisher_receipt_is_metadata_only(engine):
    _, s3, report = initialized(engine)
    record = json.loads(next(iter(s3.objects.values()))[0])
    assert record["bindings"] == {str(choice(engine)["binding_id"]): choice(engine)["binding_hash"]}
    assert "bindings" not in report and "exceptions" not in report
    assert report["binding_count"] == 1
    fifty_thousand = {str(n): "f" * 64 for n in range(50_000)}
    assert len(access._bytes({**record, "bindings": fifty_thousand})) < access.MAX_BYTES


def test_snapshot_does_not_clear_inflight_revocation_or_replace_last_viewer(engine):
    corpus(engine)
    _, s3, _ = initialized(engine)
    viewer.publish_viewer_snapshot(engine, URI, REGION, s3_client=s3)
    key = ("private-bucket", "webui/l1/candidate.db")
    original = s3.objects[key]
    access.invalidate_control(URI, REGION, s3_client=s3, mutation_id="revocation")
    with pytest.raises(RuntimeError, match="pending ledger mutation"):
        viewer.publish_viewer_snapshot(engine, URI, REGION, s3_client=s3)
    assert s3.objects[key] == original


@pytest.mark.parametrize("uri", ["s3://private-bucket/private.db", "s3://private-bucket/webui/../bad.db",
                                     "https://example.test/webui/viewer.db", "s3://user@private-bucket/webui/db",
                                     "s3://private-bucket/webui/db?version=old"])
def test_control_location_never_uses_unapproved_snapshot_uri(uri):
    with pytest.raises(ValueError):
        access.control_uri(uri)
