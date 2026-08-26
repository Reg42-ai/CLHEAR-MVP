"""Hybrid retrieval tests (Cerebras-lesson machinery): ref router, RRF fusion,
FTS5/LIKE retrievers, bursting grain, scopes, restricted discipline. Offline
against recorded fixtures."""
import sqlalchemy as sa

from app.clhear.l1 import pipeline, retrieval
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta
from app.clhear.l1.adapters.eur_lex import EurLexAdapter
from app.clhear.l1.adapters.uk_legislation import UkLegislationAdapter
from app.clhear.l1.models import search_units


def test_detect_refs_router():
    assert "regulation-27" in retrieval.detect_refs("what does reg 27 say")
    assert "regulation-18A" in retrieval.detect_refs("Regulation 18A risk assessments")
    assert "art_6" in retrieval.detect_refs("article 6 lawful basis")
    assert "rct_47" in retrieval.detect_refs("recital 47")
    assert "sec1471" in retrieval.detect_refs("§1471 withholding")
    assert "1.1471-5(b)" in retrieval.detect_refs("see 1.1471-5(b) for FFI definition")
    assert "ac-2" in retrieval.detect_refs("what is AC-2 about")
    assert "GV.OC-01" in retrieval.detect_refs("explain GV.OC-01")
    assert retrieval.detect_refs("customer due diligence") == []


def test_rrf_fusion_consensus_beats_single_vote():
    fused = retrieval.rrf({"fts": [1, 2, 3], "like": [2, 3, 1]})
    order = [uid for uid, _ in fused]
    # 1 is first in fts but third in like; 2 is second and first: consensus math
    scores = dict(fused)
    assert scores[2] > scores[3]
    assert set(order) == {1, 2, 3}
    # ref retriever outweighs the others at equal rank
    fused2 = retrieval.rrf({"ref": [9], "fts": [8], "like": [8]})
    assert dict(fused2)[9] > 0 and fused2[0][0] == 9


def test_hybrid_search_end_to_end(engine, client, tmp_path):
    store = pipeline.LocalStore(tmp_path / "lake")
    pipeline.ingest(engine, UkLegislationAdapter(), store)
    pipeline.ingest(engine, EurLexAdapter(), store)

    with engine.connect() as conn:
        grains = dict(
            conn.execute(
                sa.select(search_units.c.grain, sa.func.count()).group_by(search_units.c.grain)
            ).all()
        )
    assert grains["clause"] > 200 and grains["paragraph"] > 1000

    # Citation query: the ref router puts the cited clause first.
    hits = client.get("/api/clhear/search?q=reg 27").json()
    assert hits[0]["ref"].startswith("regulation-27")

    # Phrase query: CDD still surfaces regs 27/28 among the top hits.
    hits = client.get("/api/clhear/search?q=customer due diligence").json()
    top_refs = " ".join(h["ref"] for h in hits[:10])
    assert "regulation-27" in top_refs and "regulation-28" in top_refs

    # Bursting: a paragraph-grain win — Art. 6(1)(f) found by its own vocabulary.
    hits = client.get("/api/clhear/search?q=legitimate interests").json()
    assert hits[0]["ref"].startswith("art_6")
    assert hits[0]["grain"] == "paragraph"
    assert hits[0]["context"]  # restored context present

    # Scope (the "projects" lesson): family scope excludes other sources.
    hits = client.get("/api/clhear/search?q=personal data&scope=uk-mlr").json()
    assert hits and all(h["source_key"] == "uksi/2017/692" for h in hits)
    hits = client.get("/api/clhear/search?q=personal data&scope=eu-gdpr").json()
    assert hits and all(h["source_key"] == "celex/32016R0679" for h in hits)


def test_fts_fallback_to_like(engine, client, tmp_path, monkeypatch):
    pipeline.ingest(engine, UkLegislationAdapter(), pipeline.LocalStore(tmp_path / "lake"))
    monkeypatch.setattr(retrieval, "_fts_ok", lambda conn: False)
    hits = retrieval.search(engine, "customer due diligence")
    refs = " ".join(h["ref"] for h in hits)
    assert "regulation-27" in refs or "regulation-28" in refs


class _RestrictedAdapter:
    key = "locked"

    def meta(self) -> SourceMeta:
        return SourceMeta(
            family_key="locked-family",
            family_name="Locked family",
            source_key="locked/one",
            name="Locked source",
            kind="standard",
            issuer="stub",
            jurisdiction="XX",
            license="restricted",
            canonical_url="https://example.invalid",
            adapter="locked",
            short_name="Locked",
        )

    def fetch(self, since_version=None):
        text = "SECRET restricted control text that must never be searchable " * 4
        return FetchResult(
            version_label="edition:1",
            artifacts=[Artifact(name="doc.txt", content=text.encode())],
            tree=[DocNode(node_type="provision", ref="c1", raw_text=text)],
            version_kind="edition",
        )

    def expected_text(self, artifacts):
        return [artifacts[0].content.decode()]


def test_restricted_sources_are_never_indexed(engine, client, tmp_path):
    pipeline.ingest(engine, _RestrictedAdapter(), pipeline.LocalStore(tmp_path / "lake"))
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(search_units)).scalar_one() == 0
    assert client.get("/api/clhear/search?q=SECRET restricted control").json() == []
