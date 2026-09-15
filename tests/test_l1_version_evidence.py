"""Version binding and ordered text evidence; fixtures are tests, never demo content."""
import sqlalchemy as sa

from app.clhear.l1 import pipeline
from app.clhear.l1.models import clauses
from app.clhear.models import eval_runs, runs
from app.clhear.platform import evals
from tests.test_l1_synthetic_amendment import SyntheticAdapter, V1, V2


def test_reader_pins_nodes_to_document_version(engine, client, tmp_path):
    store = pipeline.LocalStore(tmp_path / "lake")
    pipeline.ingest(engine, SyntheticAdapter(V1, "2026-01-01"), store, index_embeddings=False)
    old = client.get("/api/clhear/sources/synthetic/prin/document").json()
    pipeline.ingest(engine, SyntheticAdapter(V2, "2026-06-01"), store, index_embeddings=False)
    selected = client.get("/api/clhear/sources/synthetic/prin/document", params={"version_label": old["version"]}).json()
    assert selected["source_version_id"] == old["source_version_id"]
    node = next(n for n in selected["nodes"] if n["ref"] == "2")
    payload = client.get(f"/api/clhear/nodes/{node['id']}", params={"source_key": "synthetic/prin", "version_label": old["version"]}).json()
    assert payload["clauses"][0]["source_version_id"] == old["source_version_id"]
    assert "version=" in payload["permalink"]
    assert client.get(f"/api/clhear/nodes/{node['id']}", params={"version_label": "consolidated:2026-06-01"}).status_code == 404
    assert client.get("/api/clhear/sources/synthetic/prin/document?version_label=missing").status_code == 404


def test_evals_do_not_reuse_old_or_unbound_results(engine, tmp_path):
    store = pipeline.LocalStore(tmp_path / "lake")
    pipeline.ingest(engine, SyntheticAdapter(V1, "2026-01-01"), store, index_embeddings=False)
    first = evals.run_suite(engine, "e3_roundtrip", "synthetic/prin")
    assert first["passed"], first
    old_id = first["scores"]["source_version_id"]
    pipeline.ingest(engine, SyntheticAdapter(V2, "2026-06-01"), store, index_embeddings=False)
    with engine.begin() as conn:
        conn.execute(eval_runs.insert().values(suite="e3_roundtrip", source_key="synthetic/prin", scores={}, passed=True))
    assert not evals.latest_source_scorecard(engine, "synthetic/prin")["suites"]
    assert evals.latest_source_scorecard(engine, "synthetic/prin", old_id)["suites"]["e3_roundtrip"]["passed"]


def test_daily_unchanged_run_is_visible_for_its_stored_version(engine, client, tmp_path):
    store = pipeline.LocalStore(tmp_path / "lake")
    adapter = SyntheticAdapter(V1, "2026-01-01")
    pipeline.ingest(engine, adapter, store, index_embeddings=False)
    daily = pipeline.ingest(engine, adapter, store, index_embeddings=False)
    assert daily["status"] == "unchanged"
    card = client.get("/api/clhear/sources/synthetic/prin/evals").json()
    assert card["last_run"]["run_id"] == daily["run_id"]
    assert card["last_run"]["status"] == "unchanged"


def test_same_version_id_with_replaced_bytes_rejects_old_evidence(engine, client, tmp_path):
    """Legacy same-label repairs may preserve the ID while replacing its artifact."""
    from app.clhear.l1.models import source_versions

    key = "synthetic/prin"
    pipeline.ingest(engine, SyntheticAdapter(V1, "2026-01-01"), pipeline.LocalStore(tmp_path / "lake"),
                    index_embeddings=False)
    first = evals.run_suite(engine, "e3_roundtrip", key)
    version_id = first["scores"]["source_version_id"]
    assert first["passed"]
    assert client.get(f"/api/clhear/sources/{key}/evals").json()["last_run"] is not None
    with engine.begin() as conn:
        conn.execute(source_versions.update().where(source_versions.c.id == version_id)
                     .values(content_hash="replacement-artifact-hash"))

    for selected in (None, version_id):
        card = evals.latest_source_scorecard(engine, key, source_version_id=selected)
        assert card["source_version_id"] == version_id
        assert not card["suites"] and not card["green"]
        assert "e3_roundtrip" in card["missing_suites"]
    evidence = client.get(f"/api/clhear/sources/{key}/evals").json()
    assert evidence["source_version_id"] == version_id
    assert evidence["last_run"] is None
    completeness, passed = evals.e2_completeness(engine, key)
    assert completeness["last_coverage"] is None and not passed


def test_roundtrip_rejects_reordered_text_even_when_words_match(engine, tmp_path):
    pipeline.ingest(engine, SyntheticAdapter(V1, "2026-01-01"), pipeline.LocalStore(tmp_path / "lake"), index_embeddings=False)
    with engine.begin() as conn:
        clause = conn.execute(sa.select(clauses).order_by(clauses.c.id)).first()
        conn.execute(clauses.update().where(clauses.c.id == clause.id).values(text=" ".join(reversed(clause.text.split()))))
    scores, passed = evals.e3_roundtrip(engine, "synthetic/prin")
    assert not passed
    assert clause.id in scores["mismatch_ids"]


def test_roundtrip_preserves_verified_heading_projection_locations(engine, tmp_path):
    from dataclasses import replace
    from app.clhear.l1.adapters.base import Artifact, FetchResult
    from app.clhear.l1.adapters.dom_document import parse, original_records
    from app.clhear.l1.models import doc_nodes

    class HtmlFixture(SyntheticAdapter):
        BODY = (b'<html><body><div id="art_1"><p class="oj-ti-art">Article 1</p>'
                b'<p class="oj-sti-art">Risk duties</p><p>A firm must keep records.</p></div></body></html>')

        def meta(self):
            return replace(super().meta(), adapter="eur_lex")

        def fetch(self, since_version=None):
            return FetchResult(version_label="edition:fixture", artifacts=[Artifact("doc.html", self.BODY, "text/html")],
                               tree=parse(self.BODY, self.source_key))

        def expected_text(self, artifacts):
            return [row[7] for row in original_records(self.BODY, self.source_key) if row[7]]

    key = "synthetic/legal-html"
    result = pipeline.ingest(engine, HtmlFixture({}, "fixture", source_key=key),
                             pipeline.LocalStore(tmp_path / "lake"), index_embeddings=False)
    assert result["status"] == "added" and result["original_verification"]["verified"]
    with engine.connect() as conn:
        article = conn.execute(sa.select(doc_nodes).where(doc_nodes.c.ref == "art_1")).one()
        assert article.heading == "Risk duties" and article.source_locator["presentation_fields"]
    scores, passed = evals.e3_roundtrip(engine, key)
    assert passed and scores["mismatch_ids"] == []
    with engine.begin() as conn:
        conn.execute(doc_nodes.update().where(doc_nodes.c.raw_text == "A firm must keep records.")
                     .values(raw_text="Different source wording."))
    assert evals.e3_roundtrip(engine, key)[1] is False


def test_team_does_not_invent_success_or_match_unrelated_runs(engine):
    from app.clhear.team import _last_run
    with engine.begin() as conn:
        conn.execute(runs.insert().values(fleet="unrelated.annotate", trigger="test", inputs={}, outputs={}))
    assert _last_run(engine, "l1.annotate") is None
    with engine.begin() as conn:
        conn.execute(runs.insert().values(fleet="l1.annotate", trigger="test", inputs={}, outputs={}))
    assert _last_run(engine, "l1.annotate")["status"] == "unknown"


def test_private_text_requires_both_reviewer_and_display_grant(engine, client, tmp_path, monkeypatch):
    from app.clhear.accounts import SESSION_COOKIE, session_token
    from app.clhear.l1.adapters.base import DocNode
    from app.clhear.l1.permissions import record_permission
    from tests.test_l1_pipeline import _StubAdapter
    from app.clhear.settings import get_settings
    monkeypatch.setenv("CLHEAR_SESSION_SECRET", "test-private-display-secret-at-least-32-characters")
    monkeypatch.setenv("CLHEAR_AUTH_DEBUG", "false")
    get_settings.cache_clear()

    adapter = _StubAdapter("v1", [DocNode(node_type="provision", ref="c1", raw_text="PRIVATE TEST TEXT")], license="restricted")
    key = adapter.meta().source_key
    flags = {"acquire": True, "store": True, "parse": True}
    record_permission(engine, source_key=key, permissions=flags, evidence_ref="test:original-fixture", approved_by="test", approved=True)
    pipeline.ingest(engine, adapter, pipeline.LocalStore(tmp_path / "lake"), index_embeddings=False)
    url = f"/api/clhear/sources/{key}/document"
    assert "PRIVATE TEST TEXT" not in client.get(url).text
    client.cookies.set(SESSION_COOKIE, session_token({"id": "reviewer", "email": "maintainer@reg42.ai", "display_name": "Reviewer"}))
    assert "PRIVATE TEST TEXT" not in client.get(url).text  # sign-in is insufficient
    record_permission(engine, source_key=key, permissions={**flags, "display_internal": True}, evidence_ref="test:display-grant", approved_by="test", approved=True)
    response = client.get(url)
    assert response.headers["cache-control"] == "private, no-store"
    document = response.json()
    text_node = next(n for n in document["nodes"] if n["raw_text"] == "PRIVATE TEST TEXT")
    node = client.get(f"/api/clhear/nodes/{text_node['id']}").json()
    assert node["raw_text"] == "PRIVATE TEST TEXT" and node["public_ok"] is False
    client.cookies.clear()
    assert "PRIVATE TEST TEXT" not in client.get(url).text  # grant is insufficient


def test_l1_hold_does_not_acknowledge_downstream_event(engine, monkeypatch):
    import json
    import pytest
    from app.clhear.settings import get_settings
    from app.clhear.workers import handle_envelope
    monkeypatch.setenv("CLHEAR_L1_ONLY", "true")
    get_settings.cache_clear()
    body = json.dumps({"event_id": "held-event", "layer": "L1", "kind": "clhear.l1.changed",
                       "subject_ref": "test/source", "producer": "test", "ts": "2026-09-15T00:00:00Z"})
    with pytest.raises(RuntimeError, match="L1 acceptance hold"):
        handle_envelope(engine, None, body)
    with engine.connect() as conn:
        assert not conn.execute(sa.select(runs.c.id).where(runs.c.fleet == "worker")).first()


def test_global_embedding_rebuild_obeys_protected_source_permission(engine, tmp_path):
    from app.clhear.l1.models import sources
    from app.clhear.platform import embeddings
    pipeline.ingest(engine, SyntheticAdapter(V1, "2026-01-01"), pipeline.LocalStore(tmp_path / "lake"), index_embeddings=False)
    with engine.begin() as conn:
        # A legacy public flag must not bypass the new source permission check.
        conn.execute(sources.update().where(sources.c.key == "synthetic/prin").values(key="iso/test-standard"))
    class NeverEmbed:
        name = "forbidden-test-provider"
        def embed(self, texts):
            raise AssertionError("Protected text reached an embedder without permission")
    result = embeddings.rebuild_index(engine, NeverEmbed(), force=True)
    assert result["indexable"] == 0 and result["embedded"] == 0 and not result["error"]


def test_search_rechecks_display_permission_after_revocation(engine, client, tmp_path):
    from app.clhear.l1.models import sources
    from app.clhear.l1.permissions import record_permission
    from app.clhear.l1 import retrieval
    pipeline.ingest(engine, SyntheticAdapter(V1, "2026-01-01"), pipeline.LocalStore(tmp_path / "lake"), index_embeddings=False)
    key = "iso/test-standard"
    with engine.begin() as conn:
        conn.execute(sources.update().where(sources.c.key == "synthetic/prin").values(key=key))
    record_permission(engine, source_key=key, permissions={"display_public": True},
                      evidence_ref="test:original-fixture-public-display", approved_by="test", approved=True)
    assert retrieval.search(engine, "integrity")
    record_permission(engine, source_key=key, permissions={}, evidence_ref="test:revocation", approved_by="test", approved=False)
    assert not retrieval.search(engine, "integrity")
    assert client.get("/api/clhear/search?q=integrity").json() == []
