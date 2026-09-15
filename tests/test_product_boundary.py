"""Production evidence cannot contain fixtures or known development credentials."""
import base64
import hashlib
import hmac
import json
import time

import pytest
import sqlalchemy as sa
from fastapi import HTTPException

from app.clhear import accounts
from app.clhear.l1 import origin, viewer_snapshot
from app.clhear.l1.models import sources, source_versions, doc_nodes
from app.clhear.l1.public import nodes_internal_select
from app.clhear.settings import get_settings
from tests.test_l1_viewer_snapshot import corpus, open_snapshot, MARKER


def test_worker_origin_review_is_idempotent_and_preserves_original_rows(engine, tmp_path):
    version = corpus(engine, key="synthetic/prin")
    first = origin.reconcile_origins(engine)
    second = origin.reconcile_origins(engine)
    assert first["classified"] == 1 and second["classified"] == 0
    with engine.connect() as conn:
        assert conn.execute(sa.select(source_versions.c.id)).scalar_one() == version
        assert conn.execute(sa.select(doc_nodes.c.raw_text)).scalar_one() == MARKER
        assert conn.execute(sa.select(sa.func.count()).select_from(origin.origin_reviews)).scalar_one() == 1
    target = tmp_path / "viewer.db"
    manifest = viewer_snapshot.compile_viewer_snapshot(engine, target)
    assert manifest["excluded_test_sources"] == 1
    assert MARKER.encode() not in target.read_bytes()
    projection = open_snapshot(target)
    with projection.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(sources)).scalar_one() == 0
        assert conn.execute(sa.select(sa.func.count()).select_from(origin.origin_reviews)).scalar_one() == 1
    projection.dispose()


def test_company_names_are_never_classified_as_test_origin():
    for name in ("eToro", "Galaxy", "Democracy Commission", "FINRA"):
        assert not origin.is_test_source({"key": "finra/enforcement/real-case", "issuer": name})
    assert origin.is_test_source({"key": "TeSt/example", "issuer": "FINRA"})


def test_release_projection_excludes_test_bindings_and_metadata(engine, tmp_path):
    from app.clhear.l1 import release_snapshot
    corpus(engine, key="fixture/release")
    target = tmp_path / "release.db"
    result = release_snapshot.compile_snapshot(engine, target)
    assert result["bindings"] == [] and result["counts"]["sources"] == 0
    assert result["counts"]["source_families"] == 0 and result["counts"]["family_members"] == 0
    assert result["counts"]["clauses"] == 0 and MARKER.encode() not in target.read_bytes()
    from app.clhear.releases import corpus_counts
    assert corpus_counts(engine) == {"families": 0, "sources": 0, "clauses": 0, "change_events": 0}
    with engine.connect() as conn:
        assert conn.execute(sa.select(source_versions.c.id)).scalar_one()


def test_empty_signing_key_cannot_mint_or_accept_tokens(monkeypatch):
    monkeypatch.setenv("CLHEAR_SESSION_SECRET", "")
    monkeypatch.delenv("CLHEAR_APP_KEYS", raising=False)
    get_settings.cache_clear()
    assert get_settings().clhear_app_keys == ""
    body = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 60, "_p": "session"}).encode()).rstrip(b"=")
    forged = body.decode() + "." + hmac.new(b"", body, hashlib.sha256).hexdigest()[:32]
    try:
        assert accounts._verify(forged, "session") is None
        with pytest.raises(HTTPException) as error:
            accounts._sign({"uid": 1}, "session")
        assert error.value.status_code == 503
    finally:
        get_settings.cache_clear()


def test_old_readonly_snapshot_does_not_invent_source_locations(engine):
    corpus(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE doc_nodes DROP COLUMN source_locator")
    with engine.connect() as conn:
        row = conn.execute(nodes_internal_select(conn)).one()
        assert row.source_locator is None and row.raw_text == MARKER
        assert "source_locator" not in {c["name"] for c in sa.inspect(conn).get_columns("doc_nodes")}


def test_cycle_snapshot_metadata_preserves_exact_inventory_without_prompt_text():
    result = viewer_snapshot._metadata({"manifest_hash": "hash", "sources_by_adapter": {"finra": ["finra/rule/2210"]},
                                      "expected_source_keys": ["finra/rule/2210"], "prompt": MARKER,
                                      "command_event_id": "event-1"})
    assert result["sources_by_adapter"]["finra"] == ["finra/rule/2210"]
    assert result["expected_source_keys"] == ["finra/rule/2210"] and result["manifest_hash"] == "hash"
    assert MARKER not in json.dumps(result)


def test_test_journey_rejects_remote_targets_and_existing_databases(tmp_path):
    from tests.support.journey import Journey, seed_synthetic_corpus
    with pytest.raises(ValueError, match="local HTTP"):
        Journey("https://clhear.reg42.ai")
    db = tmp_path / "existing.db"
    db.write_bytes(b"retain")
    with pytest.raises(ValueError, match="new database"):
        seed_synthetic_corpus(db)
    assert db.read_bytes() == b"retain"


def test_historical_projection_compatibility_cannot_satisfy_new_cycle_contract(engine):
    from app.clhear.l1 import cycles, discovery
    additions = [cycles.children, cycles.cycles, discovery.pages, discovery.cycles, origin.origin_reviews]
    with engine.begin() as conn:
        for table in additions:
            table.drop(conn)
    with engine.connect() as conn:
        viewer_snapshot._required_tables(conn, historical_manifest={"table_allowlist": []})
        with pytest.raises(RuntimeError, match="requires migrated"):
            viewer_snapshot._required_tables(conn)
        with pytest.raises(RuntimeError, match="requires migrated"):
            viewer_snapshot._required_tables(conn, historical_manifest={"cycle_id": "cycle-1"})


def test_attachment_boundaries_cannot_overwrite_preserved_originals(engine, tmp_path):
    from app.clhear.l1 import pipeline, originals
    from app.clhear.l1.adapters.base import Artifact, FetchResult
    from app.clhear.l1.adapters.official_html import OfficialHtmlAdapter
    from app.clhear.l1.adapters.html_document import parse
    class Bundle(OfficialHtmlAdapter):
        def __init__(self, shift):
            super().__init__("fixture/bundle", "Bundle", "https://example.invalid/bundle")
            self.parts = [b"<h1>One</h1><p>First document.</p>" + b" " * shift,
                          b" " * (2 - shift) + b"<h1>Two</h1><p>Second document.</p>"]
        def fetch(self, since_version=None):
            trees = []
            for number, body in enumerate(self.parts, 1):
                tree = parse(body, "fixture/bundle", part=number)
                if number > 1:
                    tree[0].ref = ""
                trees.extend(tree)
            return FetchResult(version_label="edition:one", artifacts=[Artifact(f"part{n}.html", body, "text/html") for n, body in enumerate(self.parts, 1)], tree=trees)
        def expected_text(self, artifacts):
            return [originals.html_text(a.content) for a in artifacts]
    store = pipeline.LocalStore(tmp_path / "originals")
    a, b = Bundle(1), Bundle(2)
    assert b"".join(a.parts) == b"".join(b.parts)
    first = pipeline.ingest(engine, a, store, index_embeddings=False)
    second = pipeline.ingest(engine, b, store, index_embeddings=False)
    assert first["status"] == "added" and second["status"] == "amended"
    assert first["content_hash"] != second["content_hash"]
    assert first["source_version_id"] != second["source_version_id"]
    for item, body in zip(first["artifact_manifest"], a.parts):
        assert store.get(item["key"]) == body
