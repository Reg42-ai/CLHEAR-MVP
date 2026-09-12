"""OJ TITLE/CHAPTER spine + inspector search-context / inherited tags."""
from pathlib import Path

import sqlalchemy as sa
from bs4 import BeautifulSoup

from app.clhear.l1 import pipeline, retrieval
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta, flatten
from app.clhear.l1.adapters.eur_lex import EurLexAdapter
from app.clhear.l1.models import clauses, doc_nodes

FIXTURE = Path(__file__).parent / "fixtures" / "oj_spine.html"


class _SpineAdapter:
    key = "eur_lex"

    def meta(self) -> SourceMeta:
        return SourceMeta(
            family_key="eu-mica",
            family_name="EU MiCA",
            source_key="celex/32023R1114-fixture",
            name="MiCA fixture",
            short_name="MiCA",
            kind="regulation",
            issuer="EU",
            jurisdiction="EU",
            license="open",
            license_ref="EUR-Lex",
            canonical_url="https://eur-lex.europa.eu",
            adapter="eur_lex",
            about="fixture",
            topics=["crypto", "conduct", "eu"],
            version_policy="as_published",
        )

    def fetch(self, since_version=None):
        content = FIXTURE.read_bytes()
        soup = BeautifulSoup(content, "html.parser")
        tree = EurLexAdapter(celex="32023R1114", celex_version="32023R1114")._parse_oj(soup)
        return FetchResult(
            version_label="as-published:fixture",
            artifacts=[Artifact(name="spine.xhtml", content=content, content_type="application/xhtml+xml")],
            tree=tree,
            version_kind="as_published",
        )

    def expected_text(self, artifacts):
        soup = BeautifulSoup(artifacts[0].content, "html.parser")
        return [str(s) for s in soup.strings if str(s).strip()]


_ARABIC_AND_PART = """
<html><body><div id="enc_1">
  <div id="cpt_1">
    <p class="oj-ti-section-1">CHAPTER 1</p>
    <div class="eli-title" id="cpt_1.tit_1"><p class="oj-ti-section-2">General provisions</p></div>
    <div class="eli-subdivision" id="art_1">
      <p class="oj-ti-art">Article 1</p>
      <div id="001.001"><p class="oj-normal">1. Scope.</p></div>
    </div>
  </div>
  <div id="prt_I">
    <p class="oj-ti-section-1">PART I</p>
    <div class="eli-title" id="prt_I.tit_1"><p class="oj-ti-section-2">OWN FUNDS</p></div>
    <div id="prt_I.tis_II">
      <p class="oj-ti-section-1">TITLE II</p>
      <div class="eli-title" id="prt_I.tis_II.tit_1"><p class="oj-ti-section-2">Capital</p></div>
      <div id="prt_I.tis_II.cpt_1">
        <p class="oj-ti-section-1">CHAPTER 1</p>
        <div class="eli-title" id="prt_I.tis_II.cpt_1.tit_1"><p class="oj-ti-section-2">Composition</p></div>
        <div class="eli-subdivision" id="art_5">
          <p class="oj-ti-art">Article 5</p>
          <div id="005.001"><p class="oj-normal">1. Own funds.</p></div>
        </div>
      </div>
    </div>
  </div>
</div></body></html>
"""


def test_oj_arabic_chapters_and_part_wrappers():
    soup = BeautifulSoup(_ARABIC_AND_PART, "html.parser")
    tree = EurLexAdapter(celex="32014R0596", celex_version="32014R0596")._parse_oj(soup)
    chapters = [n for n in flatten(tree) if n.node_type == "chapter"]
    assert any(c.ref == "cpt_1" and c.heading == "General provisions" for c in chapters)
    assert any(c.ref == "art_1" for c in flatten(tree) if c.ref == "art_1")
    parts = [n for n in flatten(tree) if n.node_type == "part"]
    assert any(p.ref == "prt_I" and p.label == "PART I" for p in parts)
    title = next(p for p in parts if p.ref == "prt_I.tis_II")
    assert title.heading == "Capital"
    nested = next(c for c in chapters if c.ref == "prt_I.tis_II.cpt_1")
    assert nested.heading == "Composition"
    assert any(c.ref == "art_5" for c in nested.children)


def test_oj_roman_titles_become_parts_and_nest_articles():
    soup = BeautifulSoup(FIXTURE.read_bytes(), "html.parser")
    tree = EurLexAdapter(celex="32023R1114", celex_version="32023R1114")._parse_oj(soup)
    parts = [n for n in flatten(tree) if n.node_type == "part"]
    assert [p.ref for p in parts] == ["tis_I", "tis_VI"]
    assert parts[0].label == "TITLE I"
    assert parts[0].heading == "SUBJECT MATTER"
    assert any(c.ref == "art_1" for c in parts[0].children)
    chapters = [n for n in flatten(tree) if n.node_type == "chapter"]
    assert chapters[0].ref == "tis_VI.cpt_1"
    assert chapters[0].label == "CHAPTER 1"
    assert chapters[0].heading == "Inside information"
    assert any(c.ref == "art_91" for c in chapters[0].children)
    art91 = next(n for n in flatten(tree) if n.ref == "art_91")
    assert art91.heading == "Prohibition of market manipulation"
    assert any(c.node_type == "heading" and c.heading == "Quoted chapter heading" for c in flatten([art91]))
    annex = next(n for n in tree if n.node_type == "schedule")
    assert annex.label == "ANNEX I"


def test_ingest_sets_clause_path_and_search_context(engine, client, tmp_path):
    pipeline.ingest(engine, _SpineAdapter(), pipeline.LocalStore(tmp_path / "lake"))
    with engine.connect() as conn:
        path = conn.execute(sa.select(clauses.c.path).where(clauses.c.ref == "art_91")).scalar()
        assert "TITLE VI — PREVENTION AND PROHIBITION OF MARKET ABUSE" in (path or "")
        assert "CHAPTER 1 — Inside information" in (path or "")
        point = conn.execute(
            sa.select(doc_nodes.c.id).where(doc_nodes.c.label == "(b)")
        ).scalar()
        assert point
    info = client.get(f"/api/clhear/nodes/{point}").json()
    assert info["indexed_as"]
    assert "Prohibition of market manipulation" in info["indexed_as"]
    labels = [f"{a['label']} — {a['heading']}" if a.get("heading") else a.get("label") for a in info["ancestors"]]
    assert any("Article 91" in (x or "") and "Prohibition of market manipulation" in (x or "") for x in labels)
    doc = client.get("/api/clhear/sources/celex/32023R1114-fixture/document").json()
    types = {n["node_type"] for n in doc["nodes"]}
    assert {"part", "chapter", "article", "schedule"} <= types
    toc_parts = [n for n in doc["nodes"] if n["node_type"] == "part"]
    assert [n["label"] for n in toc_parts] == ["TITLE I", "TITLE VI"]

    hits = retrieval.search(engine, "fictitious device")
    mica = [h for h in hits if h.get("doc_node_id") == point or "91" in (h.get("ref") or "")]
    assert mica, f"expected a hit on Art. 91 / point (b), got {[h.get('ref') for h in hits]}"
    assert any(
        "Prohibition of market manipulation" in (h.get("context") or "") for h in mica
    ), f"expected article heading in paragraph-grain context, got {[h.get('context') for h in mica]}"
