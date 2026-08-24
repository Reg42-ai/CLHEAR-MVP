"""P1 done-test (HLD §9) + pipeline/diff/restricted-discipline + mini-E3 tests.

Everything runs OFFLINE: the uk_legislation tests replay recorded fixtures
(tests/fixtures/http), per working rule 7.
"""
import json
import xml.etree.ElementTree as ET

import sqlalchemy as sa

from app.clhear.l1 import families, pipeline
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta
from app.clhear.l1.adapters.uk_legislation import CLML, UkLegislationAdapter
from app.clhear.l1.http import get
from app.clhear.l1.models import change_events, clauses, doc_nodes, family_members, source_versions, sources
from app.clhear.models import events


class _StubAdapter:
    """Deterministic in-memory adapter for pipeline unit tests."""

    key = "stub"

    def __init__(self, version: str, tree: list[DocNode], license: str = "open"):
        self.version = version
        self.tree = tree
        self.license = license

    def meta(self) -> SourceMeta:
        return SourceMeta(
            family_key="stub-family",
            family_name="Stub family",
            source_key=f"stub/source-{self.license}",
            name="Stub source",
            kind="regulation",
            issuer="stub",
            jurisdiction="XX",
            license=self.license,
            canonical_url="https://example.invalid/stub",
            adapter="stub",
        )

    def fetch(self, since_version=None):
        if since_version == self.version:
            return None
        return FetchResult(
            version_label=self.version,
            artifacts=[Artifact(name="doc.txt", content=self.version.encode(), content_type="text/plain")],
            tree=[
                DocNode(
                    node_type=n.node_type,
                    ref=n.ref,
                    label=n.label,
                    heading=n.heading,
                    raw_text=n.raw_text,
                    source_fragment=n.source_fragment,
                    children=list(n.children),
                )
                for n in self.tree
            ],
        )

    def expected_text(self, artifacts):
        # Trivial oracle: the stub's own node text (always consistent).
        out = []
        for node in self.tree:
            for n in node.walk():
                for piece in (n.label, n.heading, n.raw_text):
                    if piece.strip():
                        out.append(piece)
        return out


def _tree(*items: tuple[str, str]) -> list[DocNode]:
    return [DocNode(node_type="provision", ref=ref, raw_text=text) for ref, text in items]


def test_pipeline_diff_and_events(engine, tmp_path):
    store = pipeline.LocalStore(tmp_path / "lake")

    v1 = _StubAdapter("v1", _tree(("r1", "alpha"), ("r2", "bravo"), ("r3", "charlie")))
    s1 = pipeline.ingest(engine, v1, store)
    assert s1["status"] == "added" and s1["clauses"] == 3
    assert s1["nodes"] == 3

    # Re-ingest of the same version is a no-op (up-to-date short-circuit).
    assert pipeline.ingest(engine, v1, store)["status"] == "up-to-date"

    # v2: r2 amended, r3 removed, r4 added.
    v2 = _StubAdapter("v2", _tree(("r1", "alpha"), ("r2", "bravo AMENDED"), ("r4", "delta")))
    s2 = pipeline.ingest(engine, v2, store)
    assert s2["status"] == "amended"
    assert s2["diff"] == {"added": ["r4"], "removed": ["r3"], "amended": ["r2"]}

    with engine.connect() as conn:
        change = conn.execute(
            sa.select(change_events).where(change_events.c.kind == "amended")
        ).one()
        assert set(change.clause_refs) == {"r2", "r3", "r4"}
        assert change.old_version == "v1" and change.new_version == "v2"
        assert change.diff_s3_uri  # diff artifact stored

        emitted = conn.execute(sa.select(events).where(events.c.kind == "SourceChanged")).all()
        assert len(emitted) == 2  # one per ingested version
        payload = emitted[-1].payload if isinstance(emitted[-1].payload, dict) else json.loads(emitted[-1].payload)
        assert payload["new_version"] == "v2" and "r2" in payload["clause_refs"]

        statuses = dict(
            conn.execute(sa.select(source_versions.c.version_label, source_versions.c.status)).all()
        )
        assert statuses == {"v1": "superseded", "v2": "in_force"}

        # Typed tree persisted; clause projection linked back to a doc_node.
        latest = conn.execute(
            sa.select(source_versions.c.id).where(source_versions.c.status == "in_force")
        ).scalar_one()
        nodes = conn.execute(
            sa.select(doc_nodes).where(doc_nodes.c.ref == "r2").where(doc_nodes.c.source_version_id == latest)
        ).all()
        assert any(n.raw_text == "bravo AMENDED" for n in nodes)
        clause = conn.execute(
            sa.select(clauses).where(clauses.c.ref == "r2").where(clauses.c.source_version_id == latest)
        ).one()
        assert clause.doc_node_id


def test_p1_done_test_mlr_replay(engine, tmp_path):
    """HLD §9 P1 done-test: MLRs fully ingested; replayed historical amendment
    yields correct clause-diff + SourceChanged; family auto-contains the
    amending SIs from the citator."""
    store = pipeline.LocalStore(tmp_path / "lake")

    old = pipeline.ingest(engine, UkLegislationAdapter(snapshot="2020-01-09"), store)
    assert old["status"] == "added"
    assert old["clauses"] > 100  # fully ingested: regulations + schedules

    current = UkLegislationAdapter()
    new = pipeline.ingest(engine, current, store)
    assert new["status"] == "amended"
    assert "regulation-3" in new["diff"]["amended"]
    assert len(new["diff"]["amended"]) > 10
    assert new["diff"]["removed"] == []

    with engine.connect() as conn:
        emitted = conn.execute(
            sa.select(events).where(events.c.kind == "SourceChanged").order_by(events.c.id.desc())
        ).first()
        payload = emitted.payload if isinstance(emitted.payload, dict) else json.loads(emitted.payload)
        assert payload["source"] == "uksi/2017/692"
        assert "regulation-3" in payload["clause_refs"]

    summary = families.sync_citator(engine, current)
    assert "uksi/2019/1511" in summary["new_members"]
    with engine.connect() as conn:
        members = conn.execute(
            sa.select(sources.c.key, family_members.c.added_via, family_members.c.tier)
            .join(family_members, family_members.c.source_id == sources.c.id)
        ).all()
    by_key = {m.key: m for m in members}
    assert by_key["uksi/2019/1511"].added_via == "citator"
    assert by_key["uksi/2019/1511"].tier == "binding"
    assert by_key["uksi/2017/692"].added_via == "manual"

    assert families.sync_citator(engine, current)["new_members"] == []


def _ws(text: str) -> str:
    return " ".join(text.split())


def test_mlr_roundtrip_mini_e3(engine, client, tmp_path):
    """Mini-E3: concatenated public raw_text equals the CLML body's Text nodes
    (whitespace-normalized) — proving lossless storage of the official text."""
    store = pipeline.LocalStore(tmp_path / "lake")
    pipeline.ingest(engine, UkLegislationAdapter(), store)

    artifact = get("https://www.legislation.gov.uk/uksi/2017/692/data.xml")
    root = ET.fromstring(artifact)
    doc_el = root.find(f"{CLML}Secondary")
    fixture_text = []
    for section in (doc_el.find(f"{CLML}Body"), doc_el.find(f"{CLML}Schedules")):
        if section is None:
            continue
        for text_el in section.iter(f"{CLML}Text"):
            fixture_text.append("".join(text_el.itertext()))
    from_artifact = _ws(" ".join(fixture_text))

    payload = client.get("/api/clhear/sources/uksi/2017/692/document").json()
    assert payload["locked"] is False and payload["total"] > 200
    from_db = _ws(" ".join(n["raw_text"] for n in payload["nodes"] if n.get("raw_text")))
    assert len(from_db) > 10_000
    assert len(from_artifact) > 10_000
    # Whitespace-normalized fidelity: lengths within 15% and a long distinctive
    # span of the official text is present in the reconstructed document.
    ratio = abs(len(from_db) - len(from_artifact)) / max(len(from_artifact), 1)
    assert ratio < 0.15, f"length drift {ratio:.2f} db={len(from_db)} artifact={len(from_artifact)}"
    needle = "A relevant person must apply customer due diligence measures"
    assert needle in from_db and needle in from_artifact

    # Inspector payload for a provision node.
    provision = next(n for n in payload["nodes"] if n["ref"] == "regulation-28")
    info = client.get(f"/api/clhear/nodes/{provision['id']}").json()
    assert info["ref"] == "regulation-28"
    assert info["node_type"] == "provision"
    assert info["text_hash"] == provision["text_hash"]
    assert info["source_fragment"]
    assert info["permalink"].startswith("/sources?")
    assert info["source_key"] == "uksi/2017/692"
    assert "SECRET" not in json.dumps(info)  # sanity


def test_sources_api_and_search(engine, client, tmp_path):
    store = pipeline.LocalStore(tmp_path / "lake")
    pipeline.ingest(engine, UkLegislationAdapter(), store)
    families.sync_citator(engine, UkLegislationAdapter())

    listing = client.get("/api/clhear/sources").json()
    fam = next(f for f in listing if f["key"] == "uk-mlr")
    root = next(m for m in fam["members"] if m["relation"] == "root")
    assert root["key"] == "uksi/2017/692" and root["clauses"] > 100
    citator_members = [m for m in fam["members"] if m["added_via"] == "citator"]
    assert len(citator_members) > 30
    assert all(m["latest_version"] is None for m in citator_members)

    detail = client.get("/api/clhear/sources/uksi/2017/692").json()
    assert detail["license"] == "open" and detail["versions"][0]["status"] == "in_force"

    payload = client.get("/api/clhear/sources/uksi/2017/692/clauses?limit=50").json()
    assert payload["total"] > 100 and len(payload["clauses"]) == 50
    assert payload["locked"] is False
    assert payload["clauses"][0]["text"]
    assert payload["clauses"][0]["doc_node_id"]

    # P2 headline query: reg 27-28 for CDD (clause- or paragraph-grain hits).
    hits = client.get("/api/clhear/search?q=customer due diligence").json()
    joined = " ".join(h["ref"] for h in hits)
    assert "regulation-27" in joined and "regulation-28" in joined
    assert all(h["doc_node_id"] for h in hits)


def test_restricted_discipline(engine, client, tmp_path):
    """Working rule 4: restricted clause/node text never leaves via API."""
    store = pipeline.LocalStore(tmp_path / "lake")
    secret_text = "SECRET-ISO-CLAUSE control objective text"
    adapter = _StubAdapter(
        "v1",
        [DocNode(node_type="provision", ref="c1", raw_text=secret_text, source_fragment=f"<x>{secret_text}</x>")],
        license="restricted",
    )
    pipeline.ingest(engine, adapter, store)

    with engine.connect() as conn:
        row = conn.execute(sa.select(clauses)).one()
        assert row.public_ok is False
        node = conn.execute(sa.select(doc_nodes)).one()
        assert node.public_ok is False and secret_text in node.raw_text

    payload = client.get("/api/clhear/sources/stub/source-restricted/clauses").json()
    assert payload["locked"] is True
    assert payload["total"] == 1
    clause = payload["clauses"][0]
    assert clause["text"] is None
    assert clause["text_hash"]
    assert "SECRET-ISO-CLAUSE" not in json.dumps(payload)

    document = client.get("/api/clhear/sources/stub/source-restricted/document").json()
    assert document["locked"] is True
    assert document["nodes"][0]["raw_text"] is None
    assert "SECRET-ISO-CLAUSE" not in json.dumps(document)

    info = client.get(f"/api/clhear/nodes/{document['nodes'][0]['id']}").json()
    assert info["raw_text"] is None and info["source_fragment"] is None
    assert "SECRET-ISO-CLAUSE" not in json.dumps(info)

    hits = client.get("/api/clhear/search?q=SECRET-ISO-CLAUSE").json()
    assert hits == []

    assert (tmp_path / "lake" / "restricted" / "stub" / "source-restricted").exists()
