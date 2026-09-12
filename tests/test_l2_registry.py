"""HLD v2 §4.2 done-tests for the NYC L2 obligation registry.

Schema + stable ids + asserts spans; synthetic v1 -> v2 amendment propagating
from `clhear.l1.changed` into L2 change events with effective dates (via the
worker handler); supersession linking; expiry revocation; dedupe + equivalence;
second-model review with proposal escalation; grounded structured refinement;
the four L2 gate suites; migration backfill; the /l2 API and browser."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone

import sqlalchemy as sa

from app.clhear.derived_models import (
    asserts,
    concept_members,
    concepts,
    equivalences,
    l2_change_events,
    obligation_reviews,
    obligations,
    supersessions,
)
from app.clhear.l1 import pipeline
from app.clhear.l1.models import clauses
from app.clhear.l2 import change as l2_change
from app.clhear.l2 import dedupe as l2_dedupe
from app.clhear.l2 import registry
from app.clhear.l2 import review as l2_review
from app.clhear.l2 import structured as l2_structured
from app.clhear.l2.extract import run_extraction
from app.clhear.models import events, proposals
from app.clhear.platform import record
from app.clhear.platform.evals import run_suite
from app.clhear.platform.gateway import FakeProvider, Gateway
from app.clhear.platform.router import Router
from tests.test_l1_synthetic_amendment import V1, V2, SyntheticAdapter

STABLE = re.compile(r"^OBL-\d{6}$")


def _payload(row):
    return row.payload if isinstance(row.payload, dict) else json.loads(row.payload)


def _ingest(engine, tmp_path, provisions, version, **kw):
    store = pipeline.LocalStore(tmp_path / "lake")
    return pipeline.ingest(engine, SyntheticAdapter(provisions, version, **kw), store, gateway=Gateway(engine, FakeProvider()))


def _live(conn, source_key="synthetic/prin"):
    return {
        r["clause_ref"]: dict(r)
        for r in conn.execute(
            sa.select(obligations).where(obligations.c.source_key == source_key, obligations.c.status.in_(("derived", "validated")))
        ).mappings()
    }


# --------------------------------------------------------------------------- schema / extraction


def test_extraction_gives_stable_ids_asserts_spans_and_why_trails(engine, tmp_path):
    _ingest(engine, tmp_path, V1, "2026-01-01")
    out = run_extraction(engine, source_key="synthetic/prin")
    assert out["inserted"] == 2  # provisions 1 and 2 carry "must"; 3 is scope

    with engine.connect() as conn:
        live = _live(conn)
        assert set(live) == {"1", "2"}
        for ob in live.values():
            assert STABLE.match(ob["stable_id"]), ob["stable_id"]
            assert ob["id"].startswith("OBL:synthetic/prin#")  # derivation key kept
            assert ob["why_trail_id"], "I3: every write carries a why-trail"
            assert ob["jurisdictions"] == ["XX"]
            assert ob["regulator"] == "Synthetic Regulator"
            assert "firm" in ob["subject"].lower()
            assert ob["modality"] == "must"
            assert ob["obligation_type"] in ("conduct", "consumer_protection", "other", "governance")
            assert ob["determination"]
            edges = conn.execute(
                sa.select(asserts).where(asserts.c.obligation_id == ob["id"], asserts.c.valid_to.is_(None))
            ).mappings().all()
            assert len(edges) == 1
            edge = edges[0]
            assert edge["id"].startswith("AST-") and edge["strength"] == "explicit"
            clause = conn.execute(sa.select(clauses).where(clauses.c.id == edge["clause_id"])).mappings().one()
            assert clause["ref"] == ob["clause_ref"] and clause["text_hash"] == edge["text_hash"]
            span = clause["text"][edge["span_start"]:edge["span_end"]]
            assert "must" in span  # the asserted span is the duty sentence, byte-exact
        # two stable ids, strictly increasing, distinct
        sids = sorted(o["stable_id"] for o in live.values())
        assert len(set(sids)) == 2
        added = conn.execute(sa.select(l2_change_events).where(l2_change_events.c.kind == "added")).mappings().all()
        assert len(added) == 2
        assert all(c["id"].startswith("CHG-") and c["why_trail_id"] for c in added)
        bus = conn.execute(sa.select(events).where(events.c.kind == "clhear.l2.changed")).all()
        assert len(bus) == 2

    # Idempotent: a second pass touches nothing and mints no new ids.
    again = run_extraction(engine, source_key="synthetic/prin")
    assert again["inserted"] == 0 and again["re_derived"] == 0
    with engine.connect() as conn:
        assert sorted(o["stable_id"] for o in _live(conn).values()) == sids
        assert conn.execute(sa.select(sa.func.count()).select_from(l2_change_events)).scalar_one() == 2


def test_resolve_accepts_stable_id_and_derivation_key(engine, tmp_path):
    _ingest(engine, tmp_path, V1, "2026-01-01")
    run_extraction(engine, source_key="synthetic/prin")
    with engine.connect() as conn:
        ob = next(iter(_live(conn).values()))
        assert registry.resolve_obligation_id(conn, ob["stable_id"]) == ob["id"]
        assert registry.resolve_obligation_id(conn, ob["id"]) == ob["id"]
        assert registry.resolve_obligation_id(conn, "OBL-999999") is None


# --------------------------------------------------------------------------- change propagation


def test_l1_amendment_propagates_into_l2_change_events_with_effective_dates(engine, tmp_path):
    _ingest(engine, tmp_path, V1, "2026-01-01")
    run_extraction(engine, source_key="synthetic/prin")
    with engine.connect() as conn:
        before = _live(conn)
        old_hash_2 = before["2"]["text_hash"]

    second = _ingest(engine, tmp_path, V2, "2026-06-01")
    assert second["status"] == "amended"
    with engine.connect() as conn:
        bus = conn.execute(sa.select(events).where(events.c.kind == "clhear.l1.changed").order_by(events.c.id.desc())).first()
    payload = _payload(bus)

    summary = l2_change.on_l1_changed(engine, payload)
    assert summary["l1_change_event_id"] == payload["change_event_id"]
    assert summary["obligations"] == {"updated": 1}  # provision 2 changed; 4 is guidance ("should")

    with engine.connect() as conn:
        after = _live(conn)
        assert set(after) == {"1", "2"}
        assert after["2"]["stable_id"] == before["2"]["stable_id"]  # I11: id survives the amendment
        assert after["2"]["version"] == before["2"]["version"] + 1
        assert after["2"]["text_hash"] != old_hash_2
        assert after["2"]["effective_from"] == date(2027, 1, 1)
        assert after["1"]["version"] == before["1"]["version"]  # untouched clause: untouched row
        chg = conn.execute(
            sa.select(l2_change_events).where(l2_change_events.c.kind == "updated")
        ).mappings().one()
        assert chg["obligation_id"] == after["2"]["id"]
        assert chg["cause_l1_change_event_id"] == payload["change_event_id"]
        assert chg["effective_date"] == date(2027, 1, 1) and chg["effective_date_basis"] == "text"
        assert chg["old_text_hash"] == old_hash_2 and chg["new_text_hash"] == after["2"]["text_hash"]
        assert chg["cause_clause_ids"] and set(chg["cause_clause_ids"]) <= set(payload["clause_ids"])
        # Old asserts edge invalidated, new one live (I2: never delete).
        edges = conn.execute(
            sa.select(asserts).where(asserts.c.obligation_id == after["2"]["id"]).order_by(asserts.c.id)
        ).mappings().all()
        assert len(edges) == 2 and edges[0]["valid_to"] is not None and edges[1]["valid_to"] is None
        assert edges[1]["text_hash"] == after["2"]["text_hash"]
        l2_bus = conn.execute(sa.select(events).where(events.c.kind == "clhear.l2.changed").order_by(events.c.id.desc())).first()
        assert _payload(l2_bus)["change"] == "updated"

    # Same L1 event twice -> no duplicate L2 change (idempotent on the cause id).
    again = l2_change.on_l1_changed(engine, payload)
    assert again.get("skipped") == "already processed"
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(l2_change_events).where(l2_change_events.c.kind == "updated")).scalar_one() == 1


def test_worker_handles_l1_changed_envelope(engine, tmp_path):
    from app.clhear import workers
    from app.clhear.platform.events import Envelope

    _ingest(engine, tmp_path, V1, "2026-01-01")
    run_extraction(engine, source_key="synthetic/prin")
    _ingest(engine, tmp_path, V2, "2026-06-01")
    with engine.connect() as conn:
        bus = conn.execute(sa.select(events).where(events.c.kind == "clhear.l1.changed").order_by(events.c.id.desc())).mappings().first()
    envelope = Envelope(
        event_id=bus["event_id"], layer="L1", kind="clhear.l1.changed", subject_ref="synthetic/prin",
        payload=_payload(bus), producer="l1.pipeline", ts=datetime.now(timezone.utc).isoformat(),
    )
    assert "clhear.l1.changed" in workers.HANDLERS
    out = workers.handle_envelope(engine, Gateway(engine, FakeProvider()), envelope.model_dump_json())
    assert out["obligations"] == {"updated": 1}
    assert "duration_ms" in out


def test_revocation_and_supersession(engine, tmp_path):
    _ingest(engine, tmp_path, V1, "2026-01-01")
    run_extraction(engine, source_key="synthetic/prin")
    with engine.connect() as conn:
        before = _live(conn)

    # v3: provision 2's duty is withdrawn and re-enacted as provision 5 with new wording.
    v3 = {
        "1": V1["1"],
        "2": "Provision 2 was revoked with effect from 1 March 2027.",
        "3": V1["3"],
        "5": "A firm must pay due regard to the interests of its customers, communicate with them clearly and treat them fairly. "
             "This provision comes into force on 1 March 2027.",
    }
    _ingest(engine, tmp_path, v3, "2027-03-01")
    with engine.connect() as conn:
        bus = conn.execute(sa.select(events).where(events.c.kind == "clhear.l1.changed").order_by(events.c.id.desc())).first()
    summary = l2_change.on_l1_changed(engine, _payload(bus))
    assert summary["obligations"] == {"added": 1, "revoked": 1}
    assert summary["supersessions"] == 1

    with engine.connect() as conn:
        old = conn.execute(sa.select(obligations).where(obligations.c.id == before["2"]["id"])).mappings().one()
        assert old["status"] == "stale"  # I2: invalidated, never deleted
        assert old["stable_id"] == before["2"]["stable_id"]
        assert old["effective_to"] == date(2027, 3, 1)
        new = _live(conn)["5"]
        assert new["stable_id"] != old["stable_id"]
        assert new["effective_from"] == date(2027, 3, 1)
        sup = conn.execute(sa.select(supersessions)).mappings().one()
        assert sup["id"].startswith("SUP-")
        assert sup["old_obligation_id"] == old["id"] and sup["new_obligation_id"] == new["id"]
        assert sup["effective_date"] == date(2027, 3, 1)
        assert new["canonical_id"] == old["stable_id"]  # continuity: the successor inherits the canonical thread
        revoked = conn.execute(sa.select(l2_change_events).where(l2_change_events.c.kind == "revoked")).mappings().one()
        assert revoked["obligation_id"] == old["id"] and revoked["effective_date"] == date(2027, 3, 1)


def test_revoke_expired_uses_effective_to(engine, tmp_path):
    _ingest(engine, tmp_path, V1, "2026-01-01")
    run_extraction(engine, source_key="synthetic/prin")
    with engine.begin() as conn:
        ob = next(iter(_live(conn).values()))
        conn.execute(obligations.update().where(obligations.c.id == ob["id"]).values(effective_to=date(2026, 12, 31)))
    assert l2_change.revoke_expired(engine, today=date(2026, 12, 30)) == 0
    assert l2_change.revoke_expired(engine, today=date(2027, 1, 1)) == 1
    with engine.connect() as conn:
        row = conn.execute(sa.select(obligations).where(obligations.c.id == ob["id"])).mappings().one()
        assert row["status"] == "stale"
        chg = conn.execute(sa.select(l2_change_events).where(l2_change_events.c.kind == "revoked")).mappings().one()
        assert chg["effective_date_basis"] == "expiry" and chg["effective_date"] == date(2026, 12, 31)


def test_nightly_change_pass_picks_up_unprocessed_l1_changes(engine, tmp_path):
    _ingest(engine, tmp_path, V1, "2026-01-01")
    run_extraction(engine, source_key="synthetic/prin")
    _ingest(engine, tmp_path, V2, "2026-06-01")
    out = l2_change.nightly_change_pass(engine)
    assert out["processed"] >= 1
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(l2_change_events).where(l2_change_events.c.kind == "updated")).scalar_one() == 1
    again = l2_change.nightly_change_pass(engine)
    assert again["processed"] == 0


def test_infer_clause_change_kinds():
    duty = "A firm must notify the regulator within 10 business days of any change in control."
    assert l2_change.infer_clause_change(None, duty).kind == "added"
    assert l2_change.infer_clause_change(duty, None).kind == "revoked"
    assert l2_change.infer_clause_change(duty, duty.replace("10", "5")).kind == "updated"
    editorial = l2_change.infer_clause_change(duty, duty.replace("any change", "any such change"))
    assert editorial.kind == "none" and editorial.materiality == "editorial"
    assert l2_change.infer_clause_change("This chapter applies to firms.", "This chapter applies to all firms.").kind == "none"


# --------------------------------------------------------------------------- dedupe / equivalence


def test_dedupe_merges_near_duplicates_within_a_jurisdiction(engine, tmp_path):
    _ingest(engine, tmp_path, V1, "2026-01-01")
    _ingest(engine, tmp_path, {"1": V1["1"] + " ", "2": V1["2"], "3": V1["3"]}, "2026-01-01", source_key="synthetic/prin-copy")
    run_extraction(engine)
    with engine.connect() as conn:
        assert len(_live(conn)) == 2 and len(_live(conn, "synthetic/prin-copy")) == 2
    rate_before = l2_dedupe.duplicate_rate(engine)
    assert rate_before["unmerged_duplicates"] == 2 and rate_before["rate"] > 0.01
    out = l2_dedupe.dedupe(engine)
    assert out["merged"] == 2
    with engine.connect() as conn:
        a, b = _live(conn), _live(conn, "synthetic/prin-copy")
        for ref in ("1", "2"):
            first, second = sorted((a[ref], b[ref]), key=lambda r: r["stable_id"])
            assert first["canonical_id"] is None  # the canonical row
            assert second["canonical_id"] == first["stable_id"]
            assert second["why_trail_id"] != first["why_trail_id"]
    rate_after = l2_dedupe.duplicate_rate(engine)
    assert rate_after["unmerged_duplicates"] == 0 and rate_after["rate"] == 0.0
    assert l2_dedupe.dedupe(engine)["merged"] == 0  # idempotent


def test_equivalences_across_jurisdictions_by_concept_and_lexical(engine, tmp_path):
    _ingest(engine, tmp_path, V1, "2026-01-01")
    run_extraction(engine)

    class _EU(SyntheticAdapter):
        def meta(self):
            m = super().meta()
            return m.__class__(**{**m.__dict__, "jurisdiction": "EU", "issuer": "EU Regulator"})

    store = pipeline.LocalStore(tmp_path / "lake")
    pipeline.ingest(engine, _EU({"1": "An investment firm must act honestly, fairly and with integrity in the best interests of its clients.",
                                 "2": "An investment firm must pay due regard to the interests of its customers when providing services."},
                                "2026-01-01", source_key="eu/prin"), store)
    run_extraction(engine, source_key="eu/prin")
    with engine.begin() as conn:
        xx = _live(conn)
        eu = _live(conn, "eu/prin")
        conn.execute(concepts.insert().values(id="CON:integrity", name="Act with integrity", canonical_statement="Firms act with integrity.", themes=[]))
        for ob in (xx["1"], eu["1"]):
            conn.execute(concept_members.insert().values(concept_id="CON:integrity", obligation_id=ob["id"], jurisdiction=ob["jurisdiction"]))
    out = l2_dedupe.detect_equivalences(engine)
    assert out["written"] >= 2
    with engine.connect() as conn:
        rows = conn.execute(sa.select(equivalences).where(equivalences.c.valid_to.is_(None))).mappings().all()
        bases = {r["basis"] for r in rows}
        assert {"concept", "lexical"} <= bases
        concept_edge = next(r for r in rows if r["basis"] == "concept")
        assert {concept_edge["obligation_a"], concept_edge["obligation_b"]} == {xx["1"]["id"], eu["1"]["id"]}
        lexical_edge = next(r for r in rows if r["basis"] == "lexical")
        assert {lexical_edge["obligation_a"], lexical_edge["obligation_b"]} == {xx["2"]["id"], eu["2"]["id"]}
        assert float(lexical_edge["similarity"]) >= 0.6
        assert all(r["id"].startswith("EQV-") and r["why_trail_id"] for r in rows)
    # Same-jurisdiction duplicates are dedupe's business, not equivalence.
    assert all(
        r["obligation_a"].split("#")[0] != r["obligation_b"].split("#")[0] for r in rows
    )
    assert l2_dedupe.detect_equivalences(engine)["written"] == 0


# --------------------------------------------------------------------------- review / precision


def _scripted(answers: list[dict]) -> FakeProvider:
    queue = [json.dumps(a) for a in answers]
    return FakeProvider(script=lambda **_: queue.pop(0) if queue else "{}")


def _review_router(engine, verdicts: list[dict]) -> Router:
    provider = _scripted(verdicts)
    return Router(engine, providers={provider.name: provider})


def test_second_model_review_records_precision_and_escalates(engine, tmp_path):
    _ingest(engine, tmp_path, V1, "2026-01-01")
    run_extraction(engine, source_key="synthetic/prin")
    router = _review_router(engine, [
        {"verdict": "correct", "confidence": 0.97, "reason": "clause imposes the duty verbatim"},
        {"verdict": "incorrect", "confidence": 0.6, "reason": "subject misread"},
    ])
    out = l2_review.review_obligations(engine, router)
    assert out["reviewed"] == 2
    with engine.connect() as conn:
        reviews = conn.execute(sa.select(obligation_reviews).order_by(obligation_reviews.c.id)).mappings().all()
        assert [r["verdict"] for r in reviews] == ["correct", "incorrect"]
        assert all(r["reviewer_kind"] == "model" for r in reviews)
        props = conn.execute(sa.select(proposals).where(proposals.c.kind == "l2_review")).mappings().all()
        assert len(props) == 1 and props[0]["status"] == "proposed" and props[0]["layer"] == "L2"
        assert props[0]["draft"]["verdict"] == "incorrect"
        live = _live(conn)
        assert {float(o["review_confidence"]) for o in live.values()} == {0.97, 0.6}
    stats = l2_review.precision(engine)
    assert stats == {**stats, "reviewed": 2, "judged": 2, "correct": 1, "incorrect": 1, "precision": 0.5}

    # Expert verdict overrides the model's.
    with engine.connect() as conn:
        wrong = next(o for o in _live(conn).values() if float(o["review_confidence"]) == 0.6)
    res = l2_review.record_expert_review(engine, wrong["stable_id"], reviewer="panel:q3", verdict="correct", notes="re-read: fine")
    assert res["obligation_id"] == wrong["stable_id"]
    assert l2_review.precision(engine)["precision"] == 1.0

    # Nothing left unreviewed at the current hash: no model call spent.
    assert l2_review.review_obligations(engine, router)["reviewed"] == 0


def test_review_low_confidence_escalates_to_human(engine, tmp_path):
    _ingest(engine, tmp_path, V1, "2026-01-01")
    run_extraction(engine, source_key="synthetic/prin")
    router = _review_router(engine, [
        {"verdict": "correct", "confidence": 0.7, "reason": "probably"},
        {"verdict": "correct", "confidence": 0.99, "reason": "clear"},
    ])
    l2_review.review_obligations(engine, router)
    with engine.connect() as conn:
        props = conn.execute(sa.select(proposals).where(proposals.c.kind == "l2_review")).mappings().all()
    assert len(props) == 1 and props[0]["confidence"] is not None and float(props[0]["confidence"]) == 0.7
    assert record.needs_human("L2", 0.7) and not record.needs_human("L2", 0.99)


# --------------------------------------------------------------------------- structured refinement


def test_structured_refinement_is_grounded_in_clause_text(engine, tmp_path):
    _ingest(engine, tmp_path, V1, "2026-01-01")
    run_extraction(engine, source_key="synthetic/prin")
    with engine.begin() as conn:
        conn.execute(obligations.update().values(subject="", action="", condition="", object=""))
    grounded = {"subject": "A firm", "action": "conduct its business with integrity", "condition": "", "object": "its business", "obligation_type": "conduct"}
    hallucinated = {"subject": "Every payment institution", "action": "file a quarterly capital return", "condition": "", "object": "capital", "obligation_type": "reporting"}
    provider = _scripted([grounded, hallucinated])
    router = Router(engine, providers={provider.name: provider})
    out = l2_structured.refine_structured(engine, router)
    assert out["refined"] == 1 and out["rejected"] == 1
    with engine.connect() as conn:
        live = _live(conn)
        assert live["1"]["subject"] == "A firm" and live["1"]["action"] == "conduct its business with integrity"
        assert live["1"]["model_manifest"]
        assert live["2"]["subject"] == ""  # ungrounded answer rejected, deterministic fields untouched
    assert l2_structured.grounded("pay due regard", "A firm must pay due regard to the interests of its customers.")
    assert not l2_structured.grounded("file a quarterly return", "A firm must pay due regard to the interests of its customers.")


# --------------------------------------------------------------------------- gates


def test_l2_gate_suites(engine, tmp_path):
    # Empty corpus: coverage, precision and dedupe fail honestly; change inference is corpus-independent.
    assert run_suite(engine, "l2_coverage")["passed"] is False
    assert run_suite(engine, "l2_precision")["passed"] is False
    assert run_suite(engine, "l2_dedupe")["passed"] is False
    ci = run_suite(engine, "l2_change_inference")
    assert ci["passed"] is True and ci["scores"]["cases"] >= 15 and ci["scores"]["accuracy"] >= 0.95

    _ingest(engine, tmp_path, V1, "2026-01-01")
    run_extraction(engine, source_key="synthetic/prin")
    cov = run_suite(engine, "l2_coverage")
    assert cov["passed"] is True and cov["scores"]["coverage"] == 1.0 and cov["scores"]["normative_clauses"] == 2
    assert run_suite(engine, "l2_dedupe")["passed"] is True
    assert run_suite(engine, "l2_basis_integrity")["passed"] is True
    l2_review.review_obligations(engine, _review_router(engine, [
        {"verdict": "correct", "confidence": 0.98, "reason": "ok"}, {"verdict": "correct", "confidence": 0.96, "reason": "ok"}]))
    prec = run_suite(engine, "l2_precision")
    assert prec["passed"] is True and prec["scores"]["precision"] == 1.0

    from app.clhear.platform.gates import LAYER_GATES, gate_status

    assert set(LAYER_GATES["L2"]) == {"l2_coverage", "l2_precision", "l2_dedupe", "l2_change_inference", "l2_basis_integrity"}
    assert gate_status(engine, "L2")["passed"] is True

    # Coverage is honest: a normative clause with no obligation drops it below gate.
    with engine.begin() as conn:
        ob = next(iter(_live(conn).values()))
        conn.execute(obligations.update().where(obligations.c.id == ob["id"]).values(status="stale"))
    assert run_suite(engine, "l2_coverage")["passed"] is False


# --------------------------------------------------------------------------- migration backfill


def test_backfill_gives_legacy_rows_stable_ids_and_asserts(engine, tmp_path):
    _ingest(engine, tmp_path, V1, "2026-01-01")
    run_extraction(engine, source_key="synthetic/prin")
    with engine.begin() as conn:
        ob = next(iter(_live(conn).values()))
        conn.execute(obligations.update().where(obligations.c.id == ob["id"]).values(stable_id=None, determination="", subject=""))
        record.invalidate(conn, asserts, asserts.c.obligation_id == ob["id"], why=ob["why_trail_id"], reason="test: simulate legacy row")
    with engine.begin() as conn:
        out = registry.backfill_stable_ids_and_asserts(conn)
    assert out["stable_ids"] == 1 and out["asserts"] == 1
    with engine.connect() as conn:
        row = conn.execute(sa.select(obligations).where(obligations.c.id == ob["id"])).mappings().one()
        assert STABLE.match(row["stable_id"]) and row["determination"] and row["subject"]
        live_edges = conn.execute(sa.select(asserts).where(asserts.c.obligation_id == ob["id"], asserts.c.valid_to.is_(None))).all()
        assert len(live_edges) == 1


# --------------------------------------------------------------------------- /l2 API + UI


def _seed_api(engine, tmp_path):
    _ingest(engine, tmp_path, V1, "2026-01-01")
    run_extraction(engine, source_key="synthetic/prin")
    _ingest(engine, tmp_path, V2, "2026-06-01")
    with engine.connect() as conn:
        bus = conn.execute(sa.select(events).where(events.c.kind == "clhear.l1.changed").order_by(events.c.id.desc())).first()
    l2_change.on_l1_changed(engine, _payload(bus))


def test_l2_api_browse_detail_history_changes(client, engine, tmp_path):
    _seed_api(engine, tmp_path)
    listing = client.get("/l2/obligations", params={"jurisdiction": "xx"}).json()
    assert listing["total"] == 2 and "types" in listing["facets"] and "XX" in listing["facets"]["jurisdictions"]
    item = next(i for i in listing["items"] if i["clause_ref"] == "2")
    assert STABLE.match(item["id"]) and item["effective_from"] == "2027-01-01"

    detail = client.get(f"/l2/obligations/{item['id']}").json()
    assert detail["id"] == item["id"] and detail["derivation_key"] == item["derivation_key"]
    assert detail["subject"] and detail["determination"]
    src = [s for s in detail["sources"] if s["live"]]
    assert len(src) == 1 and src[0]["strength"] == "explicit" and src[0]["basis_current"] is True
    assert src[0]["text"] and src[0]["span"]["text"] in src[0]["text"]  # open licence: text + highlighted span
    from app.clhear.l1.rights import republishable
    assert republishable(src[0]["rights_basis"])
    assert [c["kind"] for c in detail["history"]["changes"]] == ["added", "updated"]
    assert detail["history"]["changes"][1]["effective_date"] == "2027-01-01"
    assert detail["why"] and all(w["layer"] == "L2" for w in detail["why"])
    assert detail["why"][0]["agent_id"]

    # Derivation key works as well as the stable id (with a slash and a hash in it).
    from urllib.parse import quote

    by_key = client.get(f"/l2/obligations/{quote(item['derivation_key'], safe='')}").json()
    assert by_key["id"] == item["id"]
    hist = client.get(f"/l2/obligations/{item['id']}/history").json()
    assert hist["version"] == 2 and len(hist["changes"]) == 2
    why = client.get(f"/l2/obligations/{item['id']}/why").json()
    assert len(why["why"]) >= 2
    assert client.get("/l2/obligations/OBL-999999").status_code == 404

    feed = client.get("/l2/changes", params={"since": "2026-06-01"}).json()
    assert feed["count"] >= 1 and feed["items"][0]["kind"] == "updated"
    assert feed["items"][0]["effective_date"] == "2027-01-01" and feed["items"][0]["cause_l1_change_event_id"]
    assert client.get("/l2/changes", params={"kind": "revoked"}).json()["count"] == 0

    card = client.get("/l2/scorecard").json()
    assert card["gate"]["layer"] == "L2" and set(card["thresholds"]) == {"coverage", "precision", "duplicate_rate", "change_inference"}
    assert card["obligations"]["live"] == 2 and card["changes"] == {"added": 2, "updated": 1}


def test_l2_api_modification_request_and_expert_review(client, engine, tmp_path):
    _seed_api(engine, tmp_path)
    item = client.get("/l2/obligations").json()["items"][0]
    bad = client.post(f"/l2/obligations/{item['id']}/modification-requests",
                      json={"field": "title", "proposed_value": "x", "rationale": "long enough rationale"})
    assert bad.status_code == 422
    ok = client.post(f"/l2/obligations/{item['id']}/modification-requests",
                     json={"field": "determination", "proposed_value": "A firm must conduct its business with integrity at all times.",
                           "rationale": "The clause text says 'at all times' — the determination dropped it.", "requester": "member:acme"})
    assert ok.status_code == 201
    body = ok.json()
    assert body["status"] == "proposed" and body["obligation_id"] == item["id"]
    with engine.connect() as conn:
        prop = conn.execute(sa.select(proposals).where(proposals.c.id == body["proposal_id"])).mappings().one()
        assert prop["kind"] == "l2_modification" and prop["layer"] == "L2" and prop["subject_ref"] == item["id"]
        assert prop["draft"]["field"] == "determination" and prop["draft"]["requester"] == "member:acme"
        # The registry row is untouched: proposals are decided in the console, never applied here.
        row = conn.execute(sa.select(obligations.c.determination).where(obligations.c.stable_id == item["id"])).scalar_one()
        assert row == item["determination"]

    rev = client.post(f"/l2/obligations/{item['id']}/reviews", json={"verdict": "correct", "reviewer": "panel:q3", "notes": "fine"})
    assert rev.status_code == 201 and rev.json()["verdict"] == "correct"
    assert client.post("/l2/obligations/OBL-999999/reviews", json={"verdict": "correct", "reviewer": "x"}).status_code == 404
    assert client.post(f"/l2/obligations/{item['id']}/reviews", json={"verdict": "meh", "reviewer": "x"}).status_code == 422
    detail = client.get(f"/l2/obligations/{item['id']}").json()
    assert detail["history"]["reviews"][0]["reviewer_kind"] == "expert"
    assert client.get("/l2/scorecard").json()["precision"]["precision"] == 1.0


def test_l2_api_equivalences_and_compare(client, engine, tmp_path):
    _ingest(engine, tmp_path, V1, "2026-01-01")
    run_extraction(engine)

    class _EU(SyntheticAdapter):
        def meta(self):
            m = super().meta()
            return m.__class__(**{**m.__dict__, "jurisdiction": "EU", "issuer": "EU Regulator"})

    pipeline.ingest(engine, _EU({"2": "An investment firm must pay due regard to the interests of its customers when providing services."},
                                "2026-01-01", source_key="eu/prin"), pipeline.LocalStore(tmp_path / "lake"))
    run_extraction(engine, source_key="eu/prin")
    l2_dedupe.detect_equivalences(engine)
    eq = client.get("/l2/equivalences").json()
    assert eq["count"] == 1 and eq["items"][0]["basis"] == "lexical"
    assert {eq["items"][0]["a"]["jurisdiction"], eq["items"][0]["b"]["jurisdiction"]} == {"XX", "EU"}
    assert client.get("/l2/equivalences", params={"jurisdiction": "eu"}).json()["count"] == 1
    assert client.get("/l2/equivalences", params={"jurisdiction": "il"}).json()["count"] == 0

    cmp_ = client.get("/l2/compare", params={"jurisdictions": "xx,eu,il"}).json()
    assert cmp_["count"] == 1
    group = cmp_["groups"][0]
    assert set(group["members"]) == {"xx", "eu"} and group["gaps"] == ["il"]
    assert client.get("/l2/compare", params={"jurisdictions": "xx"}).status_code == 422

    detail = client.get(f"/l2/obligations/{group['members']['xx'][0]['id']}").json()
    assert len(detail["equivalents"]) == 1 and detail["equivalents"][0]["obligation"]["jurisdiction"] == "EU"


def test_l2_browser_page_is_served(client):
    page = client.get("/l2")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "L2 · obligation registry" in page.text and "/l2/obligations" in page.text and "Request a modification" in page.text
    assert client.get("/openapi.json").json()["paths"].get("/l2/compare")
