"""HLD v2 §4.1 done-test: a synthetic v1 -> v2 amendment yields a change event
with clause ids + an extracted effective date, a `clhear.l1.changed` bus
event, byte-exact spans into the canonical text, normative flags and a
rights-ledger entry — with no model call spent on undated text."""
import json
import xml.etree.ElementTree as ET

import sqlalchemy as sa

from app.clhear.l1 import pipeline, rights, spans
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta
from app.clhear.l1.models import change_events, clauses, source_versions, sources
from app.clhear.models import events
from app.clhear.platform.gateway import FakeProvider, Gateway

V1 = {
    "1": "A firm must conduct its business with integrity.",
    "2": "A firm must pay due regard to the interests of its customers.",
    "3": "This chapter applies to every firm.",
}
V2 = {
    "1": "A firm must conduct its business with integrity.",
    "2": "A firm must pay due regard to the interests of its customers and treat them fairly. "
         "This amendment comes into force on 1 January 2027.",
    "3": "This chapter applies to every firm.",
    "4": "A firm should consider the guidance in this section.",
}


class SyntheticAdapter:
    key = "synthetic"

    def __init__(self, provisions: dict[str, str], version: str, *, adapter: str = "synthetic",
                 rights_basis: str = "", source_key: str = "synthetic/prin"):
        self.provisions = provisions
        self.version = version
        # The fixture is explicit CLML, independently checked by minidom.
        # Rights tests control rights_basis; an unrelated publisher adapter
        # must not be claimed for a fabricated plain-text document.
        self.adapter = "uk_legislation"
        self.rights_basis = rights_basis or rights.rights_for(adapter).basis
        self.source_key = source_key

    def meta(self) -> SourceMeta:
        return SourceMeta(
            family_key="synthetic-family",
            family_name="Synthetic family",
            source_key=self.source_key,
            name="Synthetic Principles",
            kind="regulation",
            issuer="Synthetic Regulator",
            jurisdiction="XX",
            license="open",
            canonical_url="https://example.invalid/synthetic",
            adapter=self.adapter,
            short_name="SYN PRIN",
            instrument="Synthetic Principles",
            rights_basis=self.rights_basis,
        )

    def fetch(self, since_version=None):
        from app.clhear.l1.adapters.xml_document import parse
        root = ET.Element("Legislation")
        body = ET.SubElement(ET.SubElement(root, "Secondary"), "Body")
        section = ET.SubElement(body, "P1", id="s1")
        ET.SubElement(section, "Title").text = "Section 1"
        for ref, text in self.provisions.items():
            provision = ET.SubElement(section, "P1", id=ref)
            ET.SubElement(provision, "Text").text = text
        content = ET.tostring(root, encoding="utf-8")
        return FetchResult(
            version_label=f"consolidated:{self.version}",
            artifacts=[Artifact(name="doc.xml", content=content, content_type="application/xml")],
            tree=parse(content, self.source_key, self.adapter),
        )

    def expected_text(self, artifacts):
        from app.clhear.l1.adapters.xml_document import original_records
        return [row[7] for row in original_records(artifacts[0].content, self.source_key, self.adapter) if row[7]]


def _payload(row):
    return row.payload if isinstance(row.payload, dict) else json.loads(row.payload)


def test_amendment_emits_change_event_with_effective_date_and_clause_ids(engine, tmp_path):
    store = pipeline.LocalStore(tmp_path / "lake")
    provider = FakeProvider()
    gateway = Gateway(engine, provider)

    first = pipeline.ingest(engine, SyntheticAdapter(V1, "2026-01-01"), store, gateway=gateway)
    assert first["status"] == "added"
    assert first["effective_date_basis"] == "none"

    second = pipeline.ingest(engine, SyntheticAdapter(V2, "2026-06-01"), store, gateway=gateway)
    assert second["status"] == "amended"
    assert second["effective_date"] == "2027-01-01"
    assert second["effective_date_basis"] == "text"
    assert provider.calls == 0  # deterministic pass found the date: no l1.change escalation

    with engine.connect() as conn:
        src = conn.execute(sa.select(sources).where(sources.c.key == "synthetic/prin")).mappings().one()
        ev = conn.execute(
            sa.select(change_events).where(change_events.c.kind == "amended").order_by(change_events.c.id.desc())
        ).mappings().first()
        assert ev is not None
        assert ev["effective_date"].isoformat() == "2027-01-01"
        assert ev["effective_date_basis"] == "text"
        # Leaf provisions 2 (amended) and 4 (added); the enclosing section s1
        # carries subtree text, so it is reported amended as well.
        assert set(ev["clause_refs"]) == {"2", "4", "s1"}
        latest_version_id = conn.execute(
            sa.select(source_versions.c.id).where(source_versions.c.source_id == src["id"]).order_by(source_versions.c.id.desc())
        ).scalar()
        changed = conn.execute(
            sa.select(clauses.c.id, clauses.c.ref).where(clauses.c.id.in_(ev["clause_ids"]))
        ).all()
        assert {r.ref for r in changed} == {"2", "4", "s1"}
        assert all(
            conn.execute(sa.select(clauses.c.source_version_id).where(clauses.c.id == r.id)).scalar() == latest_version_id
            for r in changed
        )

        # The bus event downstream fleets subscribe to (HLD v2 §3).
        bus = [
            r for r in conn.execute(sa.select(events).where(events.c.kind == "clhear.l1.changed").order_by(events.c.id))
        ]
        assert len(bus) == 2  # one per ingest (added, amended)
        payload = _payload(bus[-1])
        assert payload["change"] == "amended"
        assert payload["change_event_id"] == ev["id"]
        assert set(payload["clause_ids"]) == set(ev["clause_ids"])
        assert payload["effective_date"] == "2027-01-01"
        assert payload["effective_date_basis"] == "text"
        assert sorted(payload["added"]) == ["4"] and sorted(payload["amended"]) == ["2", "s1"]

        # Rights ledger: one determination recorded, sources.rights_basis in sync.
        ledger = rights.history(conn, src["id"])
        assert len(ledger) == 1
        assert ledger[0]["rights_basis"] == src["rights_basis"]
        assert rights.republishable(src["rights_basis"]) is True


def test_spans_index_the_canonical_text_and_normative_flags(engine, tmp_path):
    adapter = SyntheticAdapter(V2, "2026-06-01")
    pipeline.ingest(engine, adapter, pipeline.LocalStore(tmp_path / "lake"))
    tree = adapter.fetch(None).tree
    canonical = spans.canonical_text(tree)

    with engine.connect() as conn:
        rows = conn.execute(sa.select(clauses.c.ref, clauses.c.text, clauses.c.span_start, clauses.c.span_end, clauses.c.normative)).all()
    assert rows
    by_ref = {r.ref: r for r in rows}
    for row in rows:
        assert row.span_start is not None and row.span_end is not None
        assert canonical[row.span_start:row.span_end] == row.text
    assert by_ref["1"].normative is True   # "must"
    assert by_ref["4"].normative is False  # "should consider" — guidance
    # A container clause spans its children (subtree text) inside the canonical text.
    assert by_ref["s1"].span_start <= by_ref["1"].span_start and by_ref["s1"].span_end >= by_ref["4"].span_end


def test_llm_refinement_is_quote_bound(engine, tmp_path):
    """An `l1.change` answer must quote the clause; a hallucinated date is ignored."""
    from datetime import date

    from app.clhear.l1 import change_detect

    class _Router:
        def __init__(self, answer):
            self.answer = answer
            self.calls = 0

    calls = []

    def fake_complete(router, task_id, *, prompt, max_tokens=0, **kw):
        calls.append(task_id)
        return type("R", (), {"text": router.answer})()

    import app.clhear.platform.router as platform_router

    original = platform_router.complete
    platform_router.complete = fake_complete  # type: ignore[assignment]
    try:
        undated = change_detect.EffectiveDate(date(2026, 6, 1), "publisher", "as_of")
        text = ["The rule applies to all firms from 3 July 2026 onwards."]
        # No trigger phrase matched deterministically ("applies ... from" is
        # separated) — the router quotes a real substring: accepted.
        refined = change_detect.refine_with_router(_Router("applies to all firms from 3 July 2026"), "x", text, undated)
        assert refined.basis == "text" and refined.value == date(2026, 7, 3)
        # Hallucinated quote (not a substring): rejected, publisher basis kept.
        bad = change_detect.refine_with_router(_Router("comes into force on 9 September 2029"), "x", text, undated)
        assert bad == undated
        # Undated text: no call spent at all.
        n = len(calls)
        same = change_detect.refine_with_router(_Router("whatever"), "x", ["a new provision"], undated)
        assert same == undated and len(calls) == n
        assert all(t == "l1.change" for t in calls)
    finally:
        platform_router.complete = original  # type: ignore[assignment]
