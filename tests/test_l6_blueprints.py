"""HLD v2 §4.6 — L6 blueprint composer.

The leanest complete program for a profile: hard constraints (every
applicable obligation satisfied; L3-required blocks mandatory), greedy
set-cover + pruning for the rest, a minimality proof per item, explanations
that pass a mechanical rubric, a diff engine that recomposes stored
blueprints when lower layers change (superseding, never deleting), OSCAL
export, gates and the /l6 API + browser.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone

import sqlalchemy as sa

from app.clhear import curated
from app.clhear.derived_models import activities as activities_t
from app.clhear.derived_models import blocks as blocks_t
from app.clhear.derived_models import blueprint_items, blueprints, minimality_proofs, obligations
from app.clhear.derived_models import requires as requires_t
from app.clhear.l1 import pipeline
from app.clhear.l2.extract import run_extraction
from app.clhear.l3 import decompose as l3_decompose
from app.clhear.l4 import predicates as l4_predicates
from app.clhear.l4 import validate as l4_validate
from app.clhear.l5 import map as l5
from app.clhear.l6 import composer, diff as l6_diff
from app.clhear.l6.check import check_blueprint, verify_minimality
from app.clhear.l6.explain import refine_explanations, rubric, score_blueprint
from app.clhear.l6.models import ENGINE_VERSION, fingerprint, vocabulary
from app.clhear.interop import oscal
from app.clhear.models import events as events_t
from app.clhear.platform.evals import run_suite
from app.clhear.platform.gateway import FakeProvider, Gateway
from app.clhear.platform.router import Router
from tests.test_l1_synthetic_amendment import SyntheticAdapter

SOURCE = "synthetic/uk-composer"
UK = {
    "1": "A firm that holds client money must reconcile its client money records at least once every business day.",
    "2": "A firm must verify the identity of the customer before establishing a business relationship.",
    "3": "A firm must retain records required by this chapter for at least five years.",
    "4": "A firm must notify the FCA without undue delay of any breach of the client money rules.",
    "5": "A firm must establish and maintain a written conflicts of interest policy.",
}
BROKER = {
    "jurisdictions": ["UK"],
    "authorisations": ["UK MiFID investment firm (Part 4A permission)", "Dealing in investments as agent", "Client money permission (CASS)"],
    "products": ["equities brokerage", "client money holding"],
    "customer_base": ["retail", "professional"],
}
EMI = {
    "jurisdictions": ["UK"],
    "authorisations": ["E-money institution (Electronic Money Regulations 2011)"],
    "products": ["e-money issuance", "payment accounts"],
    "customer_base": ["retail"],
}


class UKAdapter(SyntheticAdapter):
    def meta(self):
        return dataclasses.replace(super().meta(), jurisdiction="UK", family_key="synthetic-uk", family_name="Synthetic UK")


def _corpus(engine, tmp_path, provisions=UK):
    """Ingest -> extract -> decompose (L3) -> predicates (L4) -> activities + junction (L5): the inputs L6 reads."""
    curated.seed(engine)
    store = pipeline.LocalStore(tmp_path / "lake")
    pipeline.ingest(engine, UKAdapter(provisions, "2026-01-01", source_key=SOURCE), store, gateway=Gateway(engine, FakeProvider()))
    run_extraction(engine, source_key=SOURCE)
    l3_decompose.decompose(engine, source_key=SOURCE)
    l4_predicates.extract_predicates(engine, source_key=SOURCE)
    l5.map_activities(engine)


def _live(conn, source_key=SOURCE):
    return {r["clause_ref"]: dict(r) for r in conn.execute(
        sa.select(obligations).where(obligations.c.source_key == source_key, obligations.c.status.in_(("derived", "validated")))).mappings()}


def _router(engine, answers: list[dict]) -> Router:
    queue = [json.dumps(a) for a in answers]
    provider = FakeProvider(script=lambda **_: queue.pop(0) if queue else "{}")
    return Router(engine, providers={provider.name: provider})


def _add_block(conn, bid, name, kind, satisfies, status="curated"):
    conn.execute(blocks_t.insert().values(id=bid, name=name, description="", capability="", evidence_artifacts=[],
                                          satisfies=satisfies, implements_controls=[], status=status, kind=kind, purpose=name))


# --------------------------------------------------------------------------- vocabulary


def test_vocabulary_and_fingerprint():
    v = vocabulary()
    assert v["engine_version"] == ENGINE_VERSION == "composer-v2"
    assert v["item_bases"] == ["required", "selected"] and len(v["explanation_rubric"]) == 5
    # one attribute set is one fingerprint, whatever the order or casing
    a = fingerprint({"jurisdictions": ["UK", "EU"], "products": ["x"]}, None)
    b = fingerprint({"products": ["X"], "jurisdictions": ["eu", "uk"]}, None)
    assert a == b and a != fingerprint({"jurisdictions": ["UK"]}, None) and a != fingerprint({"jurisdictions": ["UK", "EU"], "products": ["x"]}, ["ACT-X"])


# --------------------------------------------------------------------------- composition


def test_leanest_complete_program_with_required_blocks_and_proof(engine, tmp_path):
    _corpus(engine, tmp_path)
    bp = composer.compose(engine, {"attributes": BROKER}, requested_by="test")
    with engine.connect() as conn:
        live = _live(conn)
        req = {oid: [r for r in rows] for oid, rows in composer._live_requires(conn).items()}
    # completeness: every applicable obligation from the synthetic source is satisfied by >= 1 item
    mine = [c for c in bp["coverage"] if c["source_key"] == SOURCE]
    assert len(mine) == 5 and all(c["state"] == "covered" and c["satisfied_by"] for c in mine)
    assert bp["coverage_summary"]["gaps"] == 0 and bp["coverage_summary"]["total"] == bp["obligations_triggered"]
    # hard constraint: every block an obligation requires (L3) is in the program as 'required'
    program = {i["block_id"]: i for i in bp["items"]}
    for oid in (live[r]["id"] for r in UK):
        for edge in req.get(oid, []):
            bid = edge["block_id"]
            assert bid in program and program[bid]["basis"] == "required" and oid in program[bid]["required_by"]
    # each item explains itself and knows which compliance activities operate it (L5) — the conflicts policy
    # is operated by the derived 'Operate ...' activity the cartographer minted
    conflicts_block = req[live["5"]["id"]][0]["block_id"]
    assert program[conflicts_block]["kind"] == "Document"
    assert any(a["activity_id"].startswith("ACT-0") for a in program[conflicts_block]["activities_operated"])
    # minimality: checked, minimal, independently verified; every item is load-bearing
    assert bp["minimality"]["checked"] and bp["minimality"]["minimal"] and not bp["minimality"]["redundant"]
    assert bp["minimality"]["load_bearing"] == len(bp["items"]) > 0
    verified = verify_minimality(bp)
    assert verified["minimal"] and verified["proof_agrees"]
    for p in bp["minimality"]["proof"]:
        assert p["removal_impact"]["gaps"] or p["basis"] == "required"
    check = check_blueprint(bp)
    assert check["ok"] and check["complete"] and not check["problems"]
    # program tree by kind, items ordered by kind then id
    assert set(bp["program"]) <= {"System", "Document", "Role", "Configuration", "Process", "Workflow", "Asset", "Body"}
    assert sum(len(v) for v in bp["program"].values()) == len(bp["items"])
    # determinism: same inputs => same composition (minus the stored id)
    again = composer.compose(engine, {"attributes": BROKER}, log_request=False)
    assert {k: v for k, v in bp.items() if k != "blueprint_id"} == again
    # stored under a BLU- id with ITM- items and proof rows, one L6 why-trail with L2..L5 inputs (I3)
    assert bp["blueprint_id"].startswith("BLU-")
    with engine.connect() as conn:
        stored = composer.get_blueprint(conn, bp["blueprint_id"])
        rows = conn.execute(sa.select(blueprint_items).where(blueprint_items.c.blueprint_id == bp["blueprint_id"])).mappings().all()
        proofs = conn.execute(sa.select(minimality_proofs).where(minimality_proofs.c.blueprint_id == bp["blueprint_id"])).mappings().all()
        from app.clhear.platform import record
        why = conn.execute(sa.select(record.why_trails).where(record.why_trails.c.id == stored["why_trail_id"])).mappings().one()
    assert stored["status"] == "current" and stored["composition"]["composition_hash"] == bp["composition_hash"]
    assert len(rows) == len(proofs) == len(bp["items"]) and all(r["id"].startswith("ITM-") and r["why_trail_id"] == stored["why_trail_id"] for r in rows)
    assert why["layer"] == "L6" and why["agent_id"] == "l6.compose" and "minimal=True" in why["reasoning_summary"]
    assert all(it["id"] for it in stored["composition"]["items"])
    # same profile + same composition => the current blueprint is reused, not duplicated
    assert composer.compose(engine, {"attributes": BROKER}, requested_by="test")["blueprint_id"] == bp["blueprint_id"]


def test_set_cover_picks_fewest_blocks_and_prunes_redundant_ones(engine, tmp_path):
    """Alternatives (curated selectors) are minimised; a block that ends up redundant is pruned;
    the proof names what each remaining item is load-bearing for."""
    _corpus(engine, tmp_path)
    with engine.begin() as conn:
        live = _live(conn)
        # three overlapping curated alternatives for the synthetic duties 1, 3, 4
        _add_block(conn, "BLK-TEST-WIDE", "Wide client money control", "Process", [{"source_key": SOURCE, "refs": ["1", "3", "4"]}])
        _add_block(conn, "BLK-TEST-NARROW-A", "Reconciliation only", "Process", [{"source_key": SOURCE, "refs": ["1"]}])
        _add_block(conn, "BLK-TEST-NARROW-B", "Breach notice only", "Workflow", [{"source_key": SOURCE, "refs": ["4"]}])
        # drop the L3 requires edges for 1/3/4 so the alternatives decide the cover
        from app.clhear.platform import record
        for ref in ("1", "3", "4"):
            record.invalidate(conn, requires_t, requires_t.c.obligation_id == live[ref]["id"], why=composer._why("test", "test", []), reason="test")
    bp = composer.compose(engine, {"attributes": BROKER}, log_request=False)
    ids = {i["block_id"] for i in bp["items"]}
    assert "BLK-TEST-WIDE" in ids and "BLK-TEST-NARROW-A" not in ids and "BLK-TEST-NARROW-B" not in ids
    wide = next(i for i in bp["items"] if i["block_id"] == "BLK-TEST-WIDE")
    assert wide["basis"] == "selected" and set(wide["load_bearing_for"]) >= {live["1"]["id"], live["4"]["id"]}
    for ref in ("1", "3", "4"):
        c = next(c for c in bp["coverage"] if c["obligation_id"] == live[ref]["id"])
        assert "BLK-TEST-WIDE" in c["covered_by"] and c["satisfied_by"] == ["BLK-TEST-WIDE"]
    assert bp["minimality"]["minimal"] and verify_minimality(bp)["minimal"]
    # a planted redundancy is detected by the independent verifier and by the checker
    planted = json.loads(json.dumps(bp))
    planted["items"].append({**wide, "block_id": "BLK-TEST-NARROW-A", "name": "Reconciliation only", "basis": "selected",
                             "obligations_satisfied": [live["1"]["id"]], "load_bearing_for": [], "required_by": []})
    for c in planted["coverage"]:
        if c["obligation_id"] == live["1"]["id"]:
            c["covered_by"].append("BLK-TEST-NARROW-A")
            c["satisfied_by"].append("BLK-TEST-NARROW-A")
    v = verify_minimality(planted)
    assert not v["minimal"] and v["redundant"] == ["BLK-TEST-NARROW-A"] and not v["proof_agrees"]
    assert not check_blueprint(planted)["ok"]


def test_gaps_are_surfaced_and_explanations_pass_the_rubric(engine, tmp_path):
    _corpus(engine, tmp_path)
    with engine.begin() as conn:
        live = _live(conn)
        from app.clhear.platform import record
        # obligation 5 loses its only block -> a gap the program must show, never hide
        record.invalidate(conn, requires_t, requires_t.c.obligation_id == live["5"]["id"], why=composer._why("test", "test", []), reason="test")
    bp = composer.compose(engine, {"attributes": BROKER}, log_request=False)
    gap = next(c for c in bp["coverage"] if c["obligation_id"] == live["5"]["id"])
    assert gap["state"] == "gap" and gap["satisfied_by"] == [] and bp["coverage_summary"]["gaps"] == 1
    assert not check_blueprint(bp)["complete"]
    # explanations: block named, obligations cited, trigger stated, role stated, nothing outside the blueprint
    s = score_blueprint(bp)
    assert s["items"] > 0 and s["passed"] == s["items"] and s["score"] == 1.0
    item = bp["items"][0]
    assert item["block_id"] in item["explanation"] and item["obligations_satisfied"][0] in item["explanation"]
    bad = rubric("This program is fine.", item, bp)
    assert not bad["passed"] and bad["checks"]["names_block"] is False
    leaked = rubric(item["explanation"] + " See also OBL:made-up#nope.", item, bp)
    assert leaked["checks"]["no_outside_ids"] is False
    # router rewrites are kept only when they still pass the rubric (closed world)
    good = item["explanation"].replace("is required by", "is a mandatory item required by")
    second_before = bp["items"][1]["explanation"]
    llm = _router(engine, [{"explanation": good}, {"explanation": "Everything is covered by BLK-INVENTED."}])
    out = refine_explanations(engine, llm, bp, limit=2)
    assert out == {"accepted": 1, "rejected": 1}
    assert bp["items"][0]["explanation"] == good and bp["items"][1]["explanation"] == second_before


# --------------------------------------------------------------------------- diff + propagation


def test_diff_engine_recomposes_on_lower_layer_change_and_supersedes(engine, tmp_path):
    from app.clhear import workers
    from app.clhear.platform.events import Envelope

    _corpus(engine, tmp_path)
    # the migration composed the sample profiles before this corpus existed: the
    # new obligations legitimately supersede those blueprints (I1: derive downward)
    settled = l6_diff.recompose(engine, cause="corpus ingested", publish=False)
    assert settled["checked"] >= 1 and 1 <= settled["changed"] <= settled["checked"]
    prof = l4_validate.create_profile(engine, BROKER, name="Broker", source="golden")
    bp = composer.compose_for_profile(engine, prof["id"], requested_by="test")
    assert bp["profile_id"] == prof["id"] and bp["blueprint_id"].startswith("BLU-")
    with engine.connect() as conn:
        live = _live(conn)
    # nothing changed -> recompose is a no-op, the diff says so
    assert l6_diff.recompose(engine, cause="test")["changed"] == 0
    since = l6_diff.what_changed_since(engine, bp["blueprint_id"])
    assert since["changed"] is False and since["summary"]["items_added"] == 0
    # L2 revokes obligation 5 -> the worker recomposes: the conflicts policy leaves the program
    with engine.begin() as conn:
        conn.execute(obligations.update().where(obligations.c.id == live["5"]["id"]).values(status="stale"))
    env = Envelope(event_id="evt-l6-1", layer="L2", kind="clhear.l2.changed", subject_ref=live["5"]["stable_id"],
                   payload={"change_event_id": "CHG-000601", "obligation_id": live["5"]["stable_id"], "derivation_key": live["5"]["id"], "change": "revoked"},
                   producer="test", ts=datetime.now(timezone.utc).isoformat())
    out = workers.handle_envelope(engine, Gateway(engine, FakeProvider()), env.model_dump_json())
    # every current blueprint that relied on obligation 5 is superseded — ours among them
    assert out["l6"]["changed"] >= 1
    ours = next(c for c in out["l6"]["changes"] if c["supersedes"] == bp["blueprint_id"])
    new_id = ours["blueprint_id"]
    with engine.connect() as conn:
        old = composer.get_blueprint(conn, bp["blueprint_id"])
        new = composer.get_blueprint(conn, new_id)
        d = l6_diff.diff_blueprints(conn, bp["blueprint_id"], new_id)
        hist = composer.history(conn, new_id)
        count = conn.execute(sa.select(sa.func.count()).select_from(blueprints).where(blueprints.c.profile_id == prof["id"])).scalar_one()
        kinds = [r[0] for r in conn.execute(sa.select(events_t.c.kind).where(events_t.c.kind == "clhear.l6.changed"))]
    assert old["status"] == "superseded" and old["valid_to"] is not None and old["review"][-1]["reason"].startswith(f"superseded by {new_id}")
    assert new["status"] == "current" and new["review"][-1]["event"] == "recomposed" and new["review"][-1]["supersedes"] == bp["blueprint_id"]
    assert count == 2  # I2: the old blueprint is kept
    assert d["changed"] and d["same_profile"] and live["5"]["id"] in {o["obligation_id"] for o in d["obligations"]["removed"]}
    assert d["summary"]["items_removed"] >= 1 and [h["blueprint_id"] for h in hist] == [bp["blueprint_id"], new_id]
    assert len(kinds) == out["l6"]["changed"] and set(kinds) == {"clhear.l6.changed"}
    # replay of the same envelope is a no-op; the L5 junction change event also routes to L6
    assert workers.handle_envelope(engine, Gateway(engine, FakeProvider()), env.model_dump_json()) is None
    env2 = Envelope(event_id="evt-l6-2", layer="L5", kind="clhear.l5.changed", subject_ref="l5.junction", payload={},
                    producer="test", ts=datetime.now(timezone.utc).isoformat())
    out2 = workers.handle_envelope(engine, Gateway(engine, FakeProvider()), env2.model_dump_json())
    assert out2["l6"]["checked"] >= 1 and out2["l6"]["changed"] == 0
    # compare two profiles: the EMI has no client-money duties
    a = composer.compose(engine, {"attributes": BROKER}, log_request=False)
    b = composer.compose(engine, {"attributes": EMI}, log_request=False)
    d2 = l6_diff.diff_compositions(a, b)
    assert live["1"]["id"] in {o["obligation_id"] for o in d2["obligations"]["removed"]}


# --------------------------------------------------------------------------- OSCAL


def test_oscal_export_round_trips(engine, tmp_path):
    _corpus(engine, tmp_path)
    bp = composer.compose(engine, {"attributes": BROKER}, requested_by="test")
    ssp = oscal.blueprint_ssp(bp, blueprint_id=bp["blueprint_id"])["system-security-plan"]
    assert ssp["metadata"]["oscal-version"] == "1.1.2"
    comps = ssp["system-implementation"]["components"]
    reqs = ssp["control-implementation"]["implemented-requirements"]
    assert len(comps) == len(bp["items"]) and len(reqs) == len(bp["coverage"])
    assert all(any(p["name"] == "clhear-id" for p in c["props"]) for c in comps)
    ok, problems = oscal.round_trip_ok(bp)
    assert ok, problems
    back = oscal.import_blueprint(oscal.blueprint_ssp(bp, blueprint_id=bp["blueprint_id"]))
    assert back["blueprint_id"] == bp["blueprint_id"] and back["minimal"] is True and back["composition_hash"] == bp["composition_hash"]
    # byte-identical on re-export (uuid5 of ids, fixed timestamp)
    assert json.dumps(oscal.blueprint_ssp(bp, blueprint_id=bp["blueprint_id"]), sort_keys=True) == json.dumps(
        oscal.blueprint_ssp(bp, blueprint_id=bp["blueprint_id"]), sort_keys=True)
    with engine.connect() as conn:
        cd = oscal.component_definition(conn, release="r1")["component-definition"]
    assert cd["metadata"]["version"] == "r1" and any(c["title"] for c in cd["components"])
    cdd = next(c for c in cd["components"] if any(p["value"] == "BLK-CDD-PROGRAMME" for p in c["props"]))
    assert any(ir["control-id"] == "OBL:uksi/2017/692#regulation-27" for ci in cdd["control-implementations"] for ir in ci["implemented-requirements"])


# --------------------------------------------------------------------------- gates


def test_l6_gates_and_scorecard(engine, tmp_path):
    from app.clhear.platform.gates import GATE_THRESHOLDS, LAYER_GATES, gate_status

    assert LAYER_GATES["L6"] == ("l6_completeness", "l6_minimality", "l6_reference", "l6_explanation", "l6_citation")
    assert GATE_THRESHOLDS["L6"]["completeness"] == "100%" and GATE_THRESHOLDS["L6"]["reference_agreement"] == ">=90%"
    # nothing composed with obligations yet -> honest failures
    assert run_suite(engine, "l6_completeness")["passed"] is False
    _corpus(engine, tmp_path)
    prof = l4_validate.create_profile(engine, BROKER, name="Broker", source="golden")
    composer.compose_for_profile(engine, prof["id"], requested_by="test")
    comp = run_suite(engine, "l6_completeness")
    assert comp["passed"] is True and comp["scores"]["completeness"] == 1.0 and comp["scores"]["applicable_obligations"] >= 5
    mini = run_suite(engine, "l6_minimality")
    assert mini["passed"] is True and mini["scores"]["minimal"] == mini["scores"]["checked"] == 1
    expl = run_suite(engine, "l6_explanation")
    assert expl["passed"] is True and expl["scores"]["score"] == 1.0
    # reference agreement: the golden programs whose instruments are in the store are evaluated
    from app.clhear.l1.models import clauses, family_members, source_families, source_versions, sources
    with engine.begin() as conn:
        fam = conn.execute(source_families.insert().values(key="uk-mlr", name="UK MLRs", scope_charter={})).inserted_primary_key[0]
        src = conn.execute(sources.insert().values(family_id=fam, key="uksi/2017/692", name="MLRs 2017", kind="regulation", license="open",
                                                   short_name="MLRs", jurisdiction="UK", topics=[])).inserted_primary_key[0]
        conn.execute(family_members.insert().values(family_id=fam, source_id=src, relation="root", tier="binding", status="active", added_via="manual"))
        vid = conn.execute(source_versions.insert().values(source_id=src, version_label="c", version_kind="consolidated", content_hash="h",
                                                           s3_uri="s3://x", status="in_force")).inserted_primary_key[0]
        for ref, text in (("regulation-27", "A relevant person must apply customer due diligence measures when establishing a business relationship."),
                          ("regulation-28", "A relevant person must conduct ongoing monitoring of a business relationship."),
                          ("regulation-33", "A relevant person must apply enhanced customer due diligence measures in any high-risk case.")):
            conn.execute(clauses.insert().values(source_version_id=vid, ref=ref, path=f"part/{ref}", ordering=int(ref.split("-")[1]),
                                                 text=text, text_hash="h" + ref, public_ok=True))
    run_extraction(engine, source_key="uksi/2017/692")
    ref = run_suite(engine, "l6_reference")
    assert ref["scores"]["evaluated"] >= 3 and ref["scores"]["skipped"] >= 1  # eu-casp needs instruments not in this store
    assert ref["passed"] is True and ref["scores"]["agreement"] >= 0.9
    by_case = {d["case"]: d for d in ref["scores"]["detail"]}
    assert by_case["uk-retail-broker"]["f1"] == 1.0 and "BLK-CDD-PROGRAMME" in by_case["uk-retail-broker"]["in_scope"]
    assert "BLK-EDD-HIGH-RISK" in by_case["uk-retail-broker"]["in_scope"] and not by_case["uk-retail-broker"]["missing"]
    assert by_case["uk-emi-onboarding-only"]["f1"] == 1.0
    # a planted gap fails completeness (never silently accepted)
    with engine.begin() as conn:
        live = _live(conn)
        from app.clhear.platform import record
        record.invalidate(conn, requires_t, requires_t.c.obligation_id == live["5"]["id"], why=composer._why("test", "test", []), reason="test")
    l6_diff.recompose(engine, cause="test")
    assert run_suite(engine, "l6_completeness")["passed"] is False
    status = gate_status(engine, "L6")
    assert status["passed"] is False and "l6_completeness" in status["failed"]


# --------------------------------------------------------------------------- migration


def test_migration_backfills_legacy_rows_and_composes_stored_profiles(engine):
    from migrations.m0014_l6_blueprints import upgrade

    curated.seed(engine)
    with engine.begin() as conn:
        # a pre-v2 request-log row
        conn.execute(blueprints.insert().values(requested_by="legacy", release="", profile={"attributes": {"jurisdictions": ["UK"]}, "activities": None},
                                                result={"coverage_summary": {"covered": 0, "gaps": 0, "total": 0}}, engine_version="composer-v1",
                                                stable_id=None, fingerprint="", status="current"))
        upgrade(conn)
    with engine.connect() as conn:
        legacy = conn.execute(sa.select(blueprints).where(blueprints.c.requested_by == "legacy")).mappings().one()
        current = composer.list_blueprints(conn, status="current")
        pids = {r[0] for r in conn.execute(sa.select(l4_validate.profiles.c.id).where(l4_validate.profiles.c.status == "valid"))}
    assert legacy["stable_id"].startswith("BLU-") and legacy["status"] == "superseded" and legacy["fingerprint"]
    assert {c["profile_id"] for c in current} == pids and pids
    with engine.begin() as conn:
        upgrade(conn)  # idempotent
    with engine.connect() as conn:
        assert len(composer.list_blueprints(conn, status="current")) == len(current)


# --------------------------------------------------------------------------- API + browser


def test_l6_api_and_browser(client, engine, tmp_path):
    _corpus(engine, tmp_path)
    prof = l4_validate.create_profile(engine, BROKER, name="Broker", source="golden")
    assert client.get("/l6/vocabulary").json()["engine_version"] == ENGINE_VERSION
    created = client.post("/l6/blueprints", json={"profile_id": prof["id"], "requested_by": "test"})
    assert created.status_code == 201, created.text
    bp = created.json()
    bid = bp["blueprint_id"]
    assert bp["check"]["ok"] and bp["profile_id"] == prof["id"] and bp["items"]
    assert client.post("/l6/blueprints", json={"profile_id": "PRF-999999"}).status_code == 404
    assert client.post("/l6/blueprints", json={}).status_code == 422
    assert client.post("/l6/blueprints", json={"attributes": {"jurisdictions": ["UK"], "authorisations": ["Not a licence"]}}).status_code == 422
    listed = client.get("/l6/blueprints", params={"profile_id": prof["id"]}).json()
    assert listed["count"] == 1 and listed["items"][0]["blueprint_id"] == bid
    one = client.get(f"/l6/blueprints/{bid}", params={"since": True}).json()
    assert one["status"] == "current" and one["history"][0]["blueprint_id"] == bid and one["why"][0]["layer"] == "L6"
    assert one["what_changed_since"]["changed"] is False and one["check"]["ok"]
    item = one["composition"]["items"][0]
    page = client.get(f"/l6/blueprints/{bid}/items/{item['id']}").json()
    assert page["item"]["block_id"] == item["block_id"] and page["obligations"] and page["rubric"]["passed"] and page["proof"]
    assert client.get(f"/l6/blueprints/{bid}/items/ITM-999999").status_code == 404
    mini = client.get(f"/l6/blueprints/{bid}/minimality").json()
    assert mini["verified"]["minimal"] and mini["verified"]["proof_agrees"] and mini["proof_rows"]
    assert client.get(f"/l6/blueprints/{bid}/diff", params={"against": "BLU-999999"}).status_code == 404
    assert client.get(f"/l6/blueprints/{bid}/diff", params={"against": bid}).json()["changed"] is False
    exp = client.get(f"/l6/blueprints/{bid}/export", params={"format": "oscal"}).json()
    assert "system-security-plan" in exp and oscal.import_blueprint(exp)["blueprint_id"] == bid
    assert client.get(f"/l6/blueprints/{bid}/export", params={"format": "xml"}).status_code == 422
    assert client.get("/l6/blueprints/BLU-999999").status_code == 404
    cmp = client.post("/l6/compare", json={"a": {"attributes": BROKER}, "b": {"attributes": EMI}}).json()
    assert cmp["a"]["items"] and cmp["summary"]["obligations_removed"] >= 1
    assert client.get(f"/l6/profiles/{prof['id']}/blueprint").json()["blueprint_id"] == bid
    assert client.get("/l6/profiles/PRF-999999/blueprint").status_code == 404
    cd = client.get("/l6/export/oscal/components").json()
    assert cd["component-definition"]["components"]
    card = client.get("/l6/scorecard").json()
    assert card["blueprints"]["current"] >= 1 and card["completeness"]["ratio"] == 1.0 and card["minimality"]["minimal"] == card["minimality"]["checked"]
    html = client.get("/l6").text
    assert "L6 · blueprint" in html and "/l6/blueprints" in html and 'href="/l5"' in html
    # layer catalogue + counts
    layers = client.get("/api/clhear/layers").json()
    l6 = next(l for l in layers["layers"] if l["layer"] == "L6")
    assert l6["status"] == "computed" and "L2" in l6["derivation"]["inputs"] and "L5" in l6["derivation"]["inputs"]
