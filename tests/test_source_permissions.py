"""Operation grants are explicit, source-exact, time-bounded and fail closed."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.clhear.l1 import permissions as p

NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
KEY = "finra/rule/2210"


@pytest.fixture
def permission_engine():
    engine = sa.create_engine("sqlite://", execution_options={"schema_translate_map": {"l1_sources": None}})
    from migrations.m0024_source_permissions import upgrade
    with engine.begin() as conn:
        upgrade(conn)
        upgrade(conn)  # migration is idempotent and grants nothing
    yield engine
    engine.dispose()


def grant(engine, **overrides):
    args = dict(source_key=KEY, permissions={"acquire": True, "store": True, "parse": True},
                evidence_ref="contract:reviewed-source-permission-42", approved_by="rights-owner@reg42.ai",
                approved=True, valid_from=NOW - timedelta(days=1), expires_at=NOW + timedelta(days=1))
    args.update(overrides)
    return p.record_permission(engine, **args)


def decide(engine, operation="parse", key=KEY, now=NOW):
    with engine.connect() as conn:
        return p.decision(conn, key, operation, now=now)


def test_missing_grant_denies_every_operation_and_needs_no_source_row(permission_engine):
    for operation in p.OPERATIONS:
        assert decide(permission_engine, operation)["reason"] == "missing_permission"
    assert not decide(permission_engine, "unknown")["allowed"]
    grant(permission_engine)
    assert decide(permission_engine)["allowed"]


def test_grant_is_exact_and_permissions_are_independent(permission_engine):
    row = grant(permission_engine)
    assert set(row["permissions"]) == set(p.OPERATIONS)
    for operation in ("acquire", "store", "parse"):
        result = decide(permission_engine, operation)
        assert result["allowed"] and result["permission_id"] == row["id"]
        assert result["evidence_ref"] == row["evidence_ref"]
    for operation in ("embed", "infer", "train", "derive", "display_internal", "display_public", "redistribute"):
        assert decide(permission_engine, operation)["reason"] == "operation_not_granted"
    assert not decide(permission_engine, key="finra/rule/3110")["allowed"]
    assert not decide(permission_engine, key="finra/rule/2210/subsection")["allowed"]


def test_internal_display_is_not_publication_or_redistribution(permission_engine):
    grant(permission_engine, permissions={"display_internal": True})
    assert decide(permission_engine, "display_internal")["allowed"]
    assert not decide(permission_engine, "display_public")["allowed"]
    assert not decide(permission_engine, "redistribute")["allowed"]
    assert not decide(permission_engine, "parse")["allowed"]


def test_time_boundaries_and_timezone_conversion(permission_engine):
    grant(permission_engine, valid_from="2026-09-15T15:00:00+03:00", expires_at="2026-09-15T13:00:00Z")
    assert decide(permission_engine, now=NOW - timedelta(microseconds=1))["reason"] == "not_yet_valid"
    assert decide(permission_engine, now=NOW)["allowed"]
    assert decide(permission_engine, now=NOW + timedelta(hours=1))["reason"] == "expired"


def test_latest_snapshot_revokes_and_expiry_never_revives_old_grant(permission_engine):
    first = grant(permission_engine, expires_at=None)
    second = grant(permission_engine, valid_from=NOW, expires_at=NOW + timedelta(hours=1))
    assert first["id"] != second["id"]
    assert decide(permission_engine, now=NOW + timedelta(hours=2))["reason"] == "expired"
    grant(permission_engine, approved=False, valid_from=NOW, expires_at=None)
    assert decide(permission_engine)["reason"] == "not_approved"
    with permission_engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(p.source_permissions)).scalar_one() == 3


def test_future_snapshot_does_not_replace_current_until_effective(permission_engine):
    first = grant(permission_engine)
    future = grant(permission_engine, approved=False, valid_from=NOW + timedelta(hours=1), expires_at=None)
    assert decide(permission_engine)["permission_id"] == first["id"]
    later = decide(permission_engine, now=NOW + timedelta(hours=1))
    assert later["permission_id"] == future["id"] and not later["allowed"]


def test_replacement_does_not_inherit_omitted_permissions(permission_engine):
    grant(permission_engine, permissions={"parse": True, "train": True})
    grant(permission_engine, permissions={"store": True}, valid_from=NOW)
    assert decide(permission_engine, "store")["allowed"]
    assert not decide(permission_engine, "parse")["allowed"]
    assert not decide(permission_engine, "train")["allowed"]


@pytest.mark.parametrize("override", [
    {"source_key": "finra/*"}, {"source_key": ""}, {"source_key": " finra/rule/2210"},
    {"permissions": {"all": True}}, {"permissions": {"parse": "true"}},
    {"permissions": {"train": 1}}, {"permissions": []}, {"approved": 1},
    {"evidence_ref": " "}, {"approved_by": ""},
    {"valid_from": datetime(2026, 9, 15)}, {"valid_from": "tomorrow"},
    {"expires_at": NOW - timedelta(days=2)},
])
def test_malformed_grant_is_rejected_without_write(permission_engine, override):
    with pytest.raises(ValueError):
        grant(permission_engine, **override)
    with permission_engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(p.source_permissions)).scalar_one() == 0


def test_malformed_stored_json_cannot_be_truthy_permission(permission_engine):
    grant(permission_engine)
    with permission_engine.begin() as conn:
        conn.execute(p.source_permissions.update().values(permissions={"parse": "false"}))
    assert decide(permission_engine)["reason"] == "invalid_permission"


@pytest.mark.parametrize("meta", [
    {"source_key": KEY, "adapter": "sec_edgar", "license": "open"},
    {"source_key": "renamed", "adapter": "finra", "license": "open"},
    {"source_key": "finra/enforcement", "adapter": "finra_enforcement"},
    {"source_key": "iso27001", "adapter": "restricted_file", "license": "open"},
    {"source_key": "private-standard", "adapter": "other", "license": "restricted"},
    {"source_key": "other", "canonical_url": "https://www.finra.org/rules-guidance/rulebooks/finra-rules/2210"},
    {"source_key": "demo/iso27001", "license": "restricted"},
    {"source_key": KEY, "adapter": "synthetic", "canonical_url": "https://example.invalid/synthetic"},
    {"source_key": "iso/27001-2022", "adapter": "other", "license": "open"},
    {"source_key": "aicpa/soc2-tsc", "adapter": "other", "license": "open"},
    {"source_key": "pci/dss-v4", "adapter": "other", "license": "open"},
    {"source_key": "ifrs/standards", "adapter": "other", "license": "open"},
    {"source_key": "synthetic/unit-test", "adapter": "synthetic", "license": "restricted", "canonical_url": "https://example.invalid/synthetic"},
])
def test_required_for_protected_sources_even_if_labels_change(meta):
    assert p.required_for(meta)
    assert p.required_for(SimpleNamespace(**meta))


def test_open_source_does_not_need_protected_source_grant():
    assert not p.required_for({"source_key": "uk/act", "adapter": "uk_legislation", "license": "open"})


def test_connection_transaction_is_owned_by_caller(permission_engine):
    with permission_engine.connect() as conn:
        transaction = conn.begin()
        grant(conn)
        assert p.decision(conn, KEY, "parse", now=NOW)["allowed"]
        transaction.rollback()
    assert not decide(permission_engine)["allowed"]


def ingest_grant(engine, *, approved=True, **operations):
    return p.record_permission(
        engine, source_key="finra/test", permissions=operations,
        evidence_ref="test-only:original-ingestion-fixture", approved_by="test-reviewer",
        approved=approved,
    )


def test_ingestion_requires_all_three_permissions_before_fetch(engine, tmp_path, monkeypatch):
    from app.clhear.l1 import pipeline
    from app.clhear.l1.models import sources, source_versions
    from tests.test_l1_strict_gate import Adapter

    adapter = Adapter()
    monkeypatch.setattr(adapter, "fetch", lambda *_: pytest.fail("unauthorized fetch"))
    store = pipeline.LocalStore(tmp_path / "lake")
    first = pipeline.ingest(engine, adapter, store)
    assert first["status"] == "rights-blocked"
    assert first["blocked_operations"] == ["acquire", "store", "parse"]
    ingest_grant(engine, acquire=True, store=True)
    second = pipeline.ingest(engine, adapter, store)
    assert second["status"] == "rights-blocked" and second["blocked_operations"] == ["parse"]
    assert not (tmp_path / "lake").exists()
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(sources).where(sources.c.key == "finra/test")).scalar_one() == 0
        assert conn.execute(sa.select(sa.func.count()).select_from(source_versions)).scalar_one() == 0


def test_ingestion_keeps_text_private_and_suppresses_ungranted_ai(engine, tmp_path, monkeypatch):
    from app.clhear.l1 import pipeline
    from app.clhear.l1.models import clauses, doc_nodes, search_units
    from app.clhear.platform import embeddings
    from tests.test_l1_strict_gate import Adapter

    ingest_grant(engine, acquire=True, store=True, parse=True)
    monkeypatch.setattr(embeddings, "rebuild_index", lambda *a, **k: pytest.fail("ungranted embedding"))
    observed = {}
    original = pipeline._persist

    def capture(*args, **kwargs):
        observed.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_persist", capture)
    result = pipeline.ingest(engine, Adapter(), pipeline.LocalStore(tmp_path / "lake"), gateway=object())
    assert result["status"] == "added", result
    assert observed["llm_router"] is None and observed["index_embeddings"] is False
    assert result["embeddings"]["reason"] == "embed permission not granted"
    assert "/restricted/finra/test/" in result["artifacts"][0]
    with engine.connect() as conn:
        assert conn.execute(sa.select(clauses.c.text)).scalar_one() == "First duty. Second duty."
        assert [text for text in conn.execute(sa.select(doc_nodes.c.raw_text).order_by(doc_nodes.c.seq)).scalars() if text] == ["First duty. Second duty."]
        assert not conn.execute(sa.select(clauses.c.public_ok)).scalar_one()
        assert not any(conn.execute(sa.select(doc_nodes.c.public_ok)).scalars())
        assert conn.execute(sa.select(sa.func.count()).select_from(search_units)).scalar_one() == 0


def test_public_display_requires_its_own_grant_and_does_not_publish_original(engine, tmp_path):
    from app.clhear.l1 import pipeline
    from app.clhear.l1.models import clauses, source_versions, sources
    from tests.test_l1_strict_gate import Adapter

    ingest_grant(engine, acquire=True, store=True, parse=True, display_internal=True)
    adapter, store = Adapter(), pipeline.LocalStore(tmp_path / "lake")
    assert pipeline.ingest(engine, adapter, store)["status"] == "added"
    with engine.connect() as conn:
        assert not conn.execute(sa.select(clauses.c.public_ok)).scalar_one()
    ingest_grant(engine, acquire=True, store=True, parse=True, display_public=True)
    result = pipeline.ingest(engine, adapter, store)
    assert result["status"] == "amended", result
    assert "/restricted/finra/test/" in result["artifacts"][0]
    with engine.connect() as conn:
        assert conn.execute(sa.select(clauses.c.public_ok).join(source_versions, clauses.c.source_version_id == source_versions.c.id)
                            .where(source_versions.c.status == "in_force")).scalar_one()
        assert conn.execute(sa.select(sources.c.rights_basis).where(sources.c.key == "finra/test")).scalar_one() == "derived_only"


def test_revoked_ingestion_preserves_previous_text_and_does_not_fetch(engine, tmp_path, monkeypatch):
    from app.clhear.l1 import pipeline
    from app.clhear.l1.models import clauses, source_versions
    from tests.test_l1_strict_gate import Adapter

    ingest_grant(engine, acquire=True, store=True, parse=True)
    adapter, store = Adapter(), pipeline.LocalStore(tmp_path / "lake")
    assert pipeline.ingest(engine, adapter, store)["status"] == "added"
    with engine.connect() as conn:
        before = dict(conn.execute(sa.select(clauses)).mappings().one())
    ingest_grant(engine, approved=False)
    monkeypatch.setattr(adapter, "fetch", lambda *_: pytest.fail("fetch after revocation"))
    denied = pipeline.ingest(engine, adapter, store)
    assert denied["status"] == "rights-blocked" and denied["previous_version_preserved"]
    with engine.connect() as conn:
        assert dict(conn.execute(sa.select(clauses)).mappings().one()) == before
        assert conn.execute(sa.select(source_versions.c.status)).scalar_one() == "in_force"


def test_authorized_missing_artifact_creates_no_placeholder(engine, tmp_path, monkeypatch):
    from app.clhear.l1 import pipeline
    from app.clhear.l1.adapters import restricted_file
    from app.clhear.l1.models import doc_nodes, source_versions

    p.record_permission(engine, source_key="iso/27001-2022", permissions={"acquire": True, "store": True, "parse": True},
                        evidence_ref="test-only:permission-no-artifact", approved_by="test-reviewer", approved=True)
    monkeypatch.setattr(restricted_file, "_list_restricted_objects", lambda _: [])
    result = pipeline.ingest(engine, restricted_file.RestrictedFileAdapter("iso/27001-2022", "ISO 27001"),
                             pipeline.LocalStore(tmp_path / "lake"))
    assert result["status"] == "awaiting-artifact" and result["error_type"] == "FileNotFoundError"
    assert result["freshness"] == "not_checked" and result["previous_version_preserved"] is False
    assert not (tmp_path / "lake").exists()
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(source_versions)).scalar_one() == 0
        assert conn.execute(sa.select(sa.func.count()).select_from(doc_nodes)).scalar_one() == 0


def test_restricted_adapter_has_content_identity_and_no_invented_edition_date(monkeypatch):
    from app.clhear.l1.adapters import restricted_file

    body = b"Original unit-test artifact; no publisher content."
    monkeypatch.setattr(restricted_file, "_list_restricted_objects", lambda _: [("source.txt", body, "text/plain")])
    adapter = restricted_file.RestrictedFileAdapter("iso/27001-2022", "ISO 27001")
    first, second = adapter.fetch(), adapter.fetch()
    assert first.version_label == second.version_label
    assert first.version_label.startswith("edition:acquired-sha256-")
    assert first.as_of_date is None and first.effective_date is None
    assert first.artifacts[0].content == body
    monkeypatch.setattr(restricted_file, "_list_restricted_objects", lambda _: [("a", body, "text/plain"), ("b", body, "text/plain")])
    with pytest.raises(ValueError, match="Ambiguous"):
        adapter.fetch()


def test_restricted_store_listing_ignores_archives_and_paginates(monkeypatch):
    import io
    import boto3
    from app.clhear.l1.adapters import restricted_file

    prefix = "restricted/iso/27001-2022/"
    reads = []

    class Client:
        def get_paginator(self, name):
            assert name == "list_objects_v2"
            return self

        def paginate(self, **kwargs):
            assert kwargs["Prefix"] == prefix
            yield {"Contents": [{"Key": prefix + "edition:old/source.pdf"}, {"Key": prefix + "edition:old/diff.json"}]}
            yield {"Contents": [{"Key": prefix + "original.pdf"}]}

        def get_object(self, **kwargs):
            reads.append(kwargs["Key"])
            return {"Body": io.BytesIO(b"%PDF-unit-test")}

    monkeypatch.setattr(boto3, "client", lambda *a, **k: Client())
    assert restricted_file._list_restricted_objects("iso/27001-2022") == [("original.pdf", b"%PDF-unit-test", "application/pdf")]
    assert reads == [prefix + "original.pdf"]


def test_restricted_store_access_failure_is_not_reported_as_missing(monkeypatch):
    import boto3
    from app.clhear.l1.adapters import restricted_file

    def fail(*args, **kwargs):
        raise RuntimeError("unit-test denied access")

    monkeypatch.setattr(boto3, "client", fail)
    with pytest.raises(RuntimeError, match="object-store access failed"):
        restricted_file._list_restricted_objects("iso/27001-2022")


@pytest.mark.parametrize("body", [b"not-utf8-\xff", b"PK\x03\x04binary-archive"])
def test_restricted_adapter_does_not_replace_invalid_bytes_with_invented_text(monkeypatch, body):
    from app.clhear.l1.adapters import restricted_file

    monkeypatch.setattr(restricted_file, "_list_restricted_objects",
                        lambda _: [("source.bin", body, "application/octet-stream")])
    with pytest.raises(ValueError, match="Unsupported authorized artifact format"):
        restricted_file.RestrictedFileAdapter("iso/27001-2022", "ISO 27001").fetch()
