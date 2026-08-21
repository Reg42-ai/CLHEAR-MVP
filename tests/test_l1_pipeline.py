"""P1 done-test (HLD §9) + pipeline/diff/restricted-discipline tests.

Everything runs OFFLINE: the uk_legislation tests replay recorded fixtures
(tests/fixtures/http), per working rule 7.
"""
import json

import sqlalchemy as sa

from app.clhear.l1 import families, pipeline
from app.clhear.l1.adapters.base import Artifact, ClauseNode, FetchResult, SourceMeta
from app.clhear.l1.adapters.uk_legislation import UkLegislationAdapter
from app.clhear.l1.models import change_events, clauses, family_members, source_versions, sources
from app.clhear.models import events


class _StubAdapter:
    """Deterministic in-memory adapter for pipeline unit tests."""

    key = "stub"

    def __init__(self, version: str, tree: list[ClauseNode], license: str = "open"):
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
            clause_tree=self.tree,
        )


def _tree(*items: tuple[str, str]) -> list[ClauseNode]:
    return [ClauseNode(ref=ref, path="Part 1", ordering=i, text=text) for i, (ref, text) in enumerate(items)]


def test_pipeline_diff_and_events(engine, tmp_path):
    store = pipeline.LocalStore(tmp_path / "lake")

    v1 = _StubAdapter("v1", _tree(("r1", "alpha"), ("r2", "bravo"), ("r3", "charlie")))
    s1 = pipeline.ingest(engine, v1, store)
    assert s1["status"] == "added" and s1["clauses"] == 3

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

        # previous version superseded, new one in force
        statuses = dict(
            conn.execute(sa.select(source_versions.c.version_label, source_versions.c.status)).all()
        )
        assert statuses == {"v1": "superseded", "v2": "in_force"}


def test_p1_done_test_mlr_replay(engine, tmp_path):
    """HLD §9 P1 done-test: MLRs fully ingested; replayed historical amendment
    yields correct clause-diff + SourceChanged; family auto-contains the
    amending SIs from the citator."""
    store = pipeline.LocalStore(tmp_path / "lake")

    # 1. Historical point-in-time text (before SI 2019/1511 came into force).
    old = pipeline.ingest(engine, UkLegislationAdapter(snapshot="2020-01-09"), store)
    assert old["status"] == "added"
    assert old["clauses"] > 100  # fully ingested: regulations + schedules

    # 2. Replay: the current consolidated text through the same diff engine.
    current = UkLegislationAdapter()
    new = pipeline.ingest(engine, current, store)
    assert new["status"] == "amended"
    # SI 2019/1511 (in force 2020-01-10) amended reg. 3 among many others.
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

    # 3. Citator sync: family auto-contains the amending SIs.
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
    assert by_key["uksi/2017/692"].added_via == "manual"  # the root

    # Citator sync is idempotent.
    assert families.sync_citator(engine, current)["new_members"] == []


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
    assert all(m["latest_version"] is None for m in citator_members)  # reference-level

    detail = client.get("/api/clhear/sources/uksi/2017/692").json()
    assert detail["license"] == "open" and detail["versions"][0]["status"] == "in_force"

    payload = client.get("/api/clhear/sources/uksi/2017/692/clauses?limit=50").json()
    assert payload["total"] > 100 and len(payload["clauses"]) == 50
    assert payload["locked"] is False
    assert payload["clauses"][0]["text"]  # verbatim text present for open source

    # P2 headline query already works at LIKE level: reg 27-28 for CDD.
    hits = client.get("/api/clhear/search?q=customer due diligence").json()
    refs = {h["ref"] for h in hits}
    assert {"regulation-27", "regulation-28"} <= refs


def test_restricted_discipline(engine, client, tmp_path):
    """Working rule 4: restricted clause text never leaves via API or search."""
    store = pipeline.LocalStore(tmp_path / "lake")
    secret_text = "SECRET-ISO-CLAUSE control objective text"
    adapter = _StubAdapter("v1", _tree(("c1", secret_text)), license="restricted")
    pipeline.ingest(engine, adapter, store)

    with engine.connect() as conn:
        row = conn.execute(sa.select(clauses)).one()
        assert row.public_ok is False  # restricted rows are never public_ok

    payload = client.get("/api/clhear/sources/stub/source-restricted/clauses").json()
    assert payload["locked"] is True
    assert payload["total"] == 1
    clause = payload["clauses"][0]
    assert clause["text"] is None  # refs + hashes only
    assert clause["text_hash"]
    assert "SECRET-ISO-CLAUSE" not in json.dumps(payload)

    hits = client.get("/api/clhear/search?q=SECRET-ISO-CLAUSE").json()
    assert hits == []  # excluded from search by construction

    # Artifacts land under restricted/, not public-ok/.
    assert (tmp_path / "lake" / "restricted" / "stub" / "source-restricted").exists()
