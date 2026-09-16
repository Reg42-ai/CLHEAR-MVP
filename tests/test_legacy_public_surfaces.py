"""Private original prose cannot escape through legacy public projections.

The marker below is authored test material. A real exception ledger and private
worker snapshot are used; public APIs must remain references-only independently
of the current private control or a graph cached before its revocation.
"""
import json

import pytest
import sqlalchemy as sa

from app.clhear import db, layer_service
from app.clhear.l1 import operator_access, permissions, viewer_snapshot
from app.clhear.l1.models import clauses, citations, clause_annotations, sources
from app.clhear.platform import graph
from tests.test_l1_operator_access import URI, REGION, initialized
from tests.test_l1_viewer_snapshot import KEY, MARKER, corpus, open_snapshot
from tests.test_review_access import restricted_settings, restricted_client, _session  # noqa: F401


def private_rows(engine):
    corpus(engine)
    with engine.begin() as conn:
        clause_id = conn.execute(sa.select(clauses.c.id)).scalar_one()
        conn.execute(clauses.update().values(path=MARKER + " heading"))
        conn.execute(citations.insert().values(from_clause_id=clause_id,
                     raw_text=MARKER + " citation", reason=MARKER + " citation rationale"))
        conn.execute(clause_annotations.insert().values(clause_id=clause_id, origin="heuristic",
                     category="test-category", summary=MARKER + " summary", topics=[MARKER + " topic"]))
    initialized(engine)
    return clause_id


def public_grant(engine, *, approved=True):
    return permissions.record_permission(engine, source_key=KEY, permissions={"display_public": True},
        approved=approved, approved_by="test-only-reviewer", evidence_ref="test-only-public-text-permission")


def assert_private_clause(response):
    assert response.status_code == 200, response.text
    assert MARKER not in response.text
    body = response.json()
    assert body["path"] is None and body["text"] is None
    assert body["annotations"] == []
    assert body["citations"][0]["raw"] is None and body["citations"][0]["reason"] is None
    assert body["ref"] == "2210(a)" and body["text_hash"]


def test_signed_legacy_clause_and_lineage_never_use_private_exception(engine, restricted_client, monkeypatch):
    clause_id = private_rows(engine)
    _session(restricted_client)
    monkeypatch.setattr(operator_access, "verify_current_access", lambda *a, **kw: pytest.fail("Public route must not use private authority"))
    assert_private_clause(restricted_client.get(f"/l1/clauses/{clause_id}"))
    resolved = layer_service.resolve_clause(engine, KEY, "2210(a)")
    assert resolved["resolved"] and resolved["locked"]
    assert resolved["path"] is None and resolved["text"] is None
    assert MARKER not in json.dumps(resolved)
    # Stale/incorrect public_ok and open labels cannot override the protected
    # source namespace, which still needs strict public permission.
    with engine.begin() as conn:
        conn.execute(sources.update().values(license="open", rights_basis="public_domain"))
    assert_private_clause(restricted_client.get(f"/l1/clauses/{clause_id}"))
    assert layer_service.resolve_clause(engine, KEY, "2210(a)")["text"] is None


def test_private_snapshot_legacy_routes_stay_redacted_before_and_after_live_revocation(engine, restricted_client, tmp_path):
    clause_id = private_rows(engine)
    _, s3, _ = initialized(engine)
    path = tmp_path / "private-candidate.db"
    viewer_snapshot.compile_viewer_snapshot(engine, path)
    original_bytes = path.read_bytes()
    assert MARKER.encode() in original_bytes  # Actual private originals exist.
    snapshot = open_snapshot(path)
    db.set_engine(snapshot)
    _session(restricted_client)
    try:
        cached = graph.get_graph(snapshot, ensure=False)
        cached.rebuild(graph.project(snapshot))  # Warm only the disposable in-memory cache.
        # Model a graph produced by the legacy projection before this fix.
        # Response-time strict checks must defeat its cached text too.
        cached.snapshot.nodes[f"CLS-{clause_id}"]["path"] = MARKER + " stale cached heading"
        cached.snapshot.nodes[f"CLS-{clause_id}"]["raw_text"] = MARKER + " stale cached body"
        for revoked in (False, True):
            if revoked:
                operator_access.invalidate_control(URI, REGION, s3_client=s3, mutation_id="fixture-revocation")
            assert_private_clause(restricted_client.get(f"/l1/clauses/{clause_id}"))
            for route in (f"/graph/nodes/CLS-{clause_id}", f"/graph/clauses/{clause_id}/derived"):
                response = restricted_client.get(route)
                assert response.status_code == 200, response.text
                assert MARKER not in response.text
            assert MARKER not in json.dumps(layer_service.resolve_clause(snapshot, KEY, "2210(a)"))
        assert cached.snapshot.nodes[f"CLS-{clause_id}"]["path"].startswith(MARKER)
        # Redaction is a read response; the immutable snapshot and historical
        # cached evidence are never rewritten to hide the original material.
        assert path.read_bytes() == original_bytes
    finally:
        graph.invalidate(snapshot)
        db.set_engine(engine)
        snapshot.dispose()


def test_new_graph_projection_excludes_exception_paths_even_with_legacy_public_flag(engine):
    clause_id = private_rows(engine)
    snapshot = graph.project(engine)
    clause = snapshot.nodes[f"CLS-{clause_id}"]
    assert clause["path"] is None and clause["public_ok"] is False
    assert MARKER not in json.dumps(clause)


def test_genuine_public_grant_preserves_public_fields_and_revocation_sanitizes_warm_graph(engine, restricted_client):
    clause_id = private_rows(engine)
    public_grant(engine)
    _session(restricted_client)
    response = restricted_client.get(f"/l1/clauses/{clause_id}")
    assert response.status_code == 200 and MARKER in response.json()["text"]
    assert MARKER in response.json()["path"] and MARKER in response.json()["citations"][0]["raw"]
    assert MARKER in response.json()["annotations"][0]["summary"]
    assert not layer_service.resolve_clause(engine, KEY, "2210(a)")["locked"]
    cached = graph.get_graph(engine)
    assert any(MARKER in (node.get("path") or "")
               for node in cached.neighbourhood(f"CLS-{clause_id}")["nodes"])
    public_grant(engine, approved=False)
    assert_private_clause(restricted_client.get(f"/l1/clauses/{clause_id}"))
    assert layer_service.resolve_clause(engine, KEY, "2210(a)")["path"] is None
    assert MARKER not in json.dumps(cached.neighbourhood(f"CLS-{clause_id}"))
    assert MARKER not in json.dumps(cached.derived_from(str(clause_id)))
    graph.invalidate(engine)


def test_neo4j_cached_clause_properties_are_redacted_against_current_public_permission(engine):
    from types import SimpleNamespace
    clause_id = private_rows(engine)
    cached_clause = {"id": f"CLS-{clause_id}", "kind": "clause", "ref": "2210(a)",
                     "path": MARKER + " old heading", "raw_text": MARKER + " old text", "public_ok": True}
    record = {"o": {"id": "OBL-fixture", "kind": "obligation"},
              "clauses": [{"clause": cached_clause, "source": {"id": KEY, "kind": "source"},
                           "strength": "explicit", "span": [0, 10]}]}
    class Session:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def run(self, query, **params):
            return SimpleNamespace(single=lambda: record)
    neo = graph.Neo4jGraph("bolt://offline-fixture:7687", "fixture", "fixture", driver=SimpleNamespace(session=lambda **kw: Session()))
    neo.reader_engine = engine
    assert MARKER not in json.dumps(neo.evidence_for("OBL-fixture"))
    public_grant(engine)
    result = neo.evidence_for("OBL-fixture")
    assert MARKER in result["clauses"][0]["clause"]["path"]
    assert "raw_text" not in result["clauses"][0]["clause"]
    public_grant(engine, approved=False)
    assert MARKER not in json.dumps(neo.evidence_for("OBL-fixture"))
    assert MARKER in cached_clause["raw_text"]  # Response filtering never edits the graph cache.
