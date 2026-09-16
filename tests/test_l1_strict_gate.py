"""Source validation must gate storage even when substring coverage passes."""
import sqlalchemy as sa
import pytest

from app.clhear.l1 import pipeline
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta
from app.clhear.l1.models import clauses, doc_nodes, source_versions


@pytest.fixture(autouse=True)
def reviewed_test_permission(engine):
    from app.clhear.l1.permissions import record_permission

    record_permission(engine, source_key="finra/test", permissions={"acquire": True, "store": True, "parse": True},
                      evidence_ref="test-only:original-unit-fixture", approved_by="test-reviewer", approved=True)


class Adapter:
    key = "test"

    def __init__(self):
        self.violations = []

    def meta(self):
        return SourceMeta(
            family_key="test", family_name="Test", source_key="finra/test",
            name="Test rule", kind="regulation", issuer="FINRA", jurisdiction="US",
            license="open", canonical_url="https://www.finra.org/test", adapter="uk_legislation",
            rights_basis="derived_only",
        )

    BODY = b'<Legislation><Secondary><Body><P1 id="2210(a)"><Text>First duty. Second duty.</Text></P1></Body></Secondary></Legislation>'

    def fetch(self, since_version=None):
        from app.clhear.l1.adapters.xml_document import parse
        return FetchResult(version_label="consolidated:2026-09-14",
            artifacts=[Artifact("page.xml", self.BODY, "application/xml")],
            tree=parse(self.BODY, self.meta().source_key, "uk_legislation"))

    def expected_text(self, artifacts):
        return ["First duty.", "Second duty."]

    def validate_tree(self, tree, artifacts):
        return self.violations


def test_l1_only_keeps_verbatim_private_and_does_not_rebuild_embeddings(engine, tmp_path, monkeypatch):
    from app.clhear.platform import embeddings

    calls = []
    monkeypatch.setattr(embeddings, "rebuild_index", lambda *a, **k: calls.append(k))
    result = pipeline.ingest(engine, Adapter(), pipeline.LocalStore(tmp_path), index_embeddings=False)
    assert result["status"] == "added", result
    assert calls == []
    assert "/restricted/finra/test/" in result["artifacts"][0]
    artifact = result["artifact_manifest"][0]
    assert artifact["key"] == f"restricted/finra/test/sha256-{result['content_hash']}/{pipeline.sha256(Adapter.BODY)}/page.xml"
    assert (tmp_path / artifact["key"]).read_bytes() == Adapter.BODY
    assert artifact["sha256"] == pipeline.sha256(Adapter.BODY)
    with engine.connect() as conn:
        clause = conn.execute(sa.select(clauses)).one()
        assert clause.text == "First duty. Second duty."
        assert not clause.public_ok
        assert not any(conn.execute(sa.select(doc_nodes.c.public_ok)).scalars())


def test_exact_validator_blocks_storage_when_coverage_is_full(engine, tmp_path):
    adapter = Adapter()
    adapter.violations = ["FINRA ordered text differs from official body"]
    result = pipeline.ingest(engine, adapter, pipeline.LocalStore(tmp_path), index_embeddings=False)
    assert result["status"] == "not-fully-successful"
    assert result["coverage"] == 1.0
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(source_versions)).scalar_one() == 0
        assert conn.execute(sa.select(sa.func.count()).select_from(doc_nodes)).scalar_one() == 0
    assert not list(tmp_path.rglob("page.xml"))


def test_exact_validator_is_not_bypassed_by_unchanged_bytes(engine, tmp_path):
    adapter = Adapter()
    store = pipeline.LocalStore(tmp_path)
    first = pipeline.ingest(engine, adapter, store, index_embeddings=False)
    assert first["status"] == "added"
    adapter.violations = ["wrong clause reference"]
    second = pipeline.ingest(engine, adapter, store, index_embeddings=False)
    assert second["status"] == "not-fully-successful"
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(source_versions)).scalar_one() == 1


def test_clause_ordering_uses_its_node_position_before_continuation_paragraphs(engine, tmp_path):
    class ContinuationAdapter(Adapter):
        BODY = b'<Legislation><Secondary><Body><P1 id="2210(a)"><Text>First duty.</Text><P1para><Text>Second duty.</Text></P1para></P1></Body></Secondary></Legislation>'

    result = pipeline.ingest(engine, ContinuationAdapter(), pipeline.LocalStore(tmp_path), index_embeddings=False)
    assert result["status"] == "added", result
    with engine.connect() as conn:
        row = conn.execute(sa.select(clauses.c.ordering, doc_nodes.c.seq).join(doc_nodes, clauses.c.doc_node_id == doc_nodes.c.id)).one()
        assert row.ordering == row.seq
        assert row.seq >= 1


def test_unchanged_source_repairs_corrupt_projection_without_deleting_history(engine, tmp_path):
    adapter, store = Adapter(), pipeline.LocalStore(tmp_path)
    assert pipeline.ingest(engine, adapter, store, index_embeddings=False)["status"] == "added"
    with engine.begin() as conn:
        old_clause = conn.execute(sa.select(clauses)).one()
        old_node = conn.execute(sa.select(doc_nodes).where(doc_nodes.c.id == old_clause.doc_node_id)).one()
        conn.execute(doc_nodes.update().where(doc_nodes.c.id == old_node.id).values(raw_text="lost text"))
        conn.execute(clauses.update().where(clauses.c.id == old_clause.id).values(text="lost text"))
    repaired = pipeline.ingest(engine, adapter, store, index_embeddings=False)
    assert repaired["status"] == "amended", repaired
    assert any(stage["stage"] == "projection_repair" for stage in repaired["stages"])
    with engine.connect() as conn:
        assert conn.execute(sa.select(clauses.c.text).where(clauses.c.id == old_clause.id)).scalar_one() == "lost text"
        assert conn.execute(sa.select(doc_nodes.c.id).where(doc_nodes.c.id == old_node.id)).scalar_one() == old_node.id
        assert conn.execute(sa.select(sa.func.count()).select_from(source_versions)).scalar_one() == 2
    assert pipeline.ingest(engine, adapter, store, index_embeddings=False)["status"] == "unchanged"


def test_retry_hash_matches_bytes_actually_stored(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("CLHEAR_SALVAGE_CAP", "0")
    from app.clhear.settings import get_settings
    get_settings.cache_clear()

    class RetryAdapter(Adapter):
        attempts = 0

        def fetch(self, since_version=None):
            self.attempts += 1
            result = super().fetch(since_version)
            if self.attempts == 1:
                from app.clhear.l1.adapters.xml_document import parse
                body = b'<Legislation><Secondary><Body><P1 id="2210(a)"><Text>First duty.</Text></P1></Body></Secondary></Legislation>'
                result.tree = parse(body, self.meta().source_key, "uk_legislation")
                result.artifacts = [Artifact("page.xml", body, "application/xml")]
            return result

    result = pipeline.ingest(engine, RetryAdapter(), pipeline.LocalStore(tmp_path), index_embeddings=False)
    assert result["status"] == "added", result
    expected = pipeline.artifact_set_hash([Artifact("page.xml", Adapter.BODY, "application/xml")])
    assert result["content_hash"] == expected
    with engine.connect() as conn:
        assert conn.execute(sa.select(source_versions.c.content_hash)).scalar_one() == expected
