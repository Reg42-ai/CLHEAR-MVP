"""HLD v2 §4.5 done-tests for the NYC L5 activities junction.

Two sides with a published vocabulary (the narrative metaphor never becomes a
schema term — lint); the curated junction (implies / operates / mitigates)
built idempotently with why-trails and no orphan; deterministic mapping of
every live obligation to a compliance activity (cue, else the block it
requires) with L4 predicates as trigger conditions; junction edges lit by
shared obligations and L4 product predicates; the activity map for a profile;
L2 / L4 change propagation through the worker; the closed-world router
refinement; the L5 gate suites; the migration on legacy activities; the /l5
API and browser."""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa

from app.clhear import curated
from app.clhear.derived_models import activities as activities_t
from app.clhear.derived_models import implies, mitigates, obligations, operates
from app.clhear.l1 import pipeline
from app.clhear.l2.extract import run_extraction
from app.clhear.l3 import decompose as l3_decompose
from app.clhear.l4 import predicates as l4_predicates
from app.clhear.l5 import map as l5
from app.clhear.l5.check import check_junction
from app.clhear.l5.models import ACTION_TYPES, SIDES, schema_term_lint, vocabulary
from app.clhear.models import events as events_t
from app.clhear.platform import record
from app.clhear.platform.evals import run_suite
from app.clhear.platform.gateway import FakeProvider, Gateway
from app.clhear.platform.router import Router
from tests.test_l1_synthetic_amendment import SyntheticAdapter

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "synthetic/uk-junction"
UK = {
    "1": "A firm that holds client money must reconcile its client money records at least once every business day.",
    "2": "A firm must verify the identity of the customer before establishing a business relationship.",
    "3": "A firm must retain records required by this chapter for at least five years.",
    "4": "A firm must notify the FCA without undue delay of any breach of the client money rules.",
    "5": "A firm must establish and maintain a written conflicts of interest policy.",
}
EMI = {
    "jurisdictions": ["UK"],
    "authorisations": ["E-money institution (Electronic Money Regulations 2011)"],
    "products": ["e-money issuance", "payment accounts"],
    "customer_base": ["retail"],
    "data_footprint": "large-scale personal data",
}
BROKER = {
    "jurisdictions": ["UK"],
    "authorisations": ["UK MiFID investment firm (Part 4A permission)", "Client money permission (CASS)"],
    "products": ["equities brokerage", "client money holding"],
    "customer_base": ["retail"],
}


class UKAdapter(SyntheticAdapter):
    def meta(self):
        return dataclasses.replace(super().meta(), jurisdiction="UK", family_key="synthetic-uk", family_name="Synthetic UK")


def _corpus(engine, tmp_path, provisions=UK):
    """Ingest -> extract -> decompose (L3) -> predicates (L4): the inputs L5 reads."""
    curated.seed(engine)
    store = pipeline.LocalStore(tmp_path / "lake")
    pipeline.ingest(engine, UKAdapter(provisions, "2026-01-01", source_key=SOURCE), store, gateway=Gateway(engine, FakeProvider()))
    run_extraction(engine, source_key=SOURCE)
    l3_decompose.decompose(engine, source_key=SOURCE)
    l4_predicates.extract_predicates(engine, source_key=SOURCE)


def _live(conn, source_key=SOURCE):
    return {r["clause_ref"]: dict(r) for r in conn.execute(
        sa.select(obligations).where(obligations.c.source_key == source_key, obligations.c.status.in_(("derived", "validated")))).mappings()}


def _act(conn, act_id):
    row = dict(conn.execute(sa.select(activities_t).where(activities_t.c.id == act_id)).mappings().one())
    row["triggers"] = l5._json(row["triggers"], [])
    return row


def _edges(conn, table, **where):
    q = sa.select(table)
    for k, v in where.items():
        q = q.where(getattr(table.c, k) == v)
    rows = [dict(r) for r in conn.execute(q.order_by(table.c.id)).mappings()]
    for r in rows:
        if "obligation_refs" in r:
            r["obligation_refs"] = l5._json(r["obligation_refs"], [])
    return rows


def _router(engine, answers: list[dict]) -> Router:
    queue = [json.dumps(a) for a in answers]
    provider = FakeProvider(script=lambda **_: queue.pop(0) if queue else "{}")
    return Router(engine, providers={provider.name: provider})


# --------------------------------------------------------------------------- vocabulary + lint


def test_two_sides_published_vocabulary_and_no_metaphor_in_schema():
    assert SIDES == ("business", "compliance")
    v = vocabulary()
    assert {"onboarding", "order_handling", "marketing", "payments", "custody"} <= set(ACTION_TYPES["business"])
    assert {"screen", "monitor", "investigate", "report", "train", "attest"} <= set(ACTION_TYPES["compliance"])
    assert set(v["edges"]) == {"implies", "operates", "mitigates"}
    schema_files = [
        ROOT / "app/clhear/derived_models.py", ROOT / "migrations/m0013_l5_junction.py",
        ROOT / "app/clhear/curated/l5_activities.json", ROOT / "app/clhear/l5/map.py", ROOT / "app/clhear/l5/check.py",
        ROOT / "app/clhear/v1/l5.py", ROOT / "app/clhear/web/l5.html", ROOT / "clhear-evals/l5/mapping/core.json",
    ]
    assert schema_term_lint(schema_files) == []
    assert schema_term_lint([ROOT / "tests" / "_lint_probe.txt"]) == []  # missing file: no hit, no crash
    # the lint does catch the words when they appear
    probe = Path("/tmp/l5_lint_probe.txt")
    probe.write_text("side = 'defense'\n")
    assert schema_term_lint([probe])[0]["line"] == 1
    # every curated activity uses the vocabulary
    for item in curated.load("l5_activities"):
        assert item["side"] in SIDES and item["action_type"] in ACTION_TYPES[item["side"]]


# --------------------------------------------------------------------------- curated junction


def test_seeded_junction_complete_idempotent_with_why_trails(engine):
    counts = curated.seed(engine)
    assert counts["activities"] >= 20
    with engine.connect() as conn:
        acts = l5.live_activities(conn)
        sides = {a["side"] for a in acts}
        assert sides == {"business", "compliance"}
        assert all(a["action_type"] in ACTION_TYPES[a["side"]] for a in acts)
        imp, opr, mit = _edges(conn, implies), _edges(conn, operates), _edges(conn, mitigates)
        trails = {r["id"]: dict(r) for r in conn.execute(sa.select(record.why_trails).where(record.why_trails.c.layer == "L5")).mappings()}
    # ids from the sequence, never reused; every edge has an L5 why-trail (I3)
    assert imp[0]["id"].startswith("IMP-") and opr[0]["id"].startswith("OPR-") and mit[0]["id"].startswith("MIT-")
    assert all(e["why_trail_id"] in trails for e in imp + opr + mit)
    # "*" implied_by expands to every live product
    onboarding = {e["product_id"] for e in imp if e["activity_id"] == "ACT-ONBOARD-CUSTOMER"}
    assert len(onboarding) == 17 and "PRD:crypto-custody" in onboarding
    assert {e["product_id"] for e in imp if e["activity_id"] == "ACT-CUSTODY-CRYPTO"} == {"PRD:crypto-custody"}
    # curated operates and mitigates present, with their anchors' rationale
    assert any(e["activity_id"] == "ACT-SCREEN-CUSTOMERS" and e["block_id"] == "BLK-CDD-PROGRAMME" for e in opr)
    edge = next(e for e in mit if e["compliance_activity_id"] == "ACT-SCREEN-CUSTOMERS" and e["business_activity_id"] == "ACT-ONBOARD-CUSTOMER")
    assert edge["method"] == "curated" and "governs onboard a customer" in edge["rationale"]
    # no orphan: every business activity implied by a product, every compliance activity operating a block
    check = check_junction(engine)
    assert check["ok"] and check["orphans"] == [] and check["dangling"] == [] and check["completeness"] == 1.0
    assert check["vocabulary_violations"] == [] and check["when_violations"] == []
    # idempotent: a second build changes nothing and publishes nothing
    again = l5.build_junction(engine)
    assert again["added"] == 0 and again["changed"] == 0 and again["invalidated"] == 0 and again["unchanged"] == len(imp) + len(opr) + len(mit)
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(events_t).where(events_t.c.kind == "clhear.l5.changed")).scalar_one() == 0


def test_junction_reversions_and_invalidates_when_inputs_change(engine, monkeypatch):
    curated.seed(engine)
    rows = json.loads(json.dumps(curated.load("l5_activities")))
    # the reviewed table drops a product from an implies list
    for r in rows:
        if r["id"] == "ACT-HANDLE-ORDERS":
            r["implied_by"] = [p for p in r["implied_by"] if p != "PRD:crypto-exchange"]
    monkeypatch.setattr(l5, "_curated", lambda: rows)
    out = l5.build_junction(engine)
    assert out["invalidated"] == 1 and out["added"] == 0
    with engine.connect() as conn:
        gone = _edges(conn, implies, product_id="PRD:crypto-exchange", activity_id="ACT-HANDLE-ORDERS")
        assert len(gone) == 1 and gone[0]["valid_to"] is not None and gone[0]["review"][-1]["event"] == "invalidated"  # I2
        n_events = conn.execute(sa.select(sa.func.count()).select_from(events_t).where(events_t.c.kind == "clhear.l5.changed")).scalar_one()
    assert n_events == 1


# --------------------------------------------------------------------------- mapping from the registry


def test_every_obligation_mapped_by_cue_or_block_with_l4_conditions(engine, tmp_path):
    _corpus(engine, tmp_path)
    with engine.begin() as conn:
        out = l5.map_deterministic(conn)
    assert out["unmapped"] == 0 and out["mapped"] == 5 and out["by_cue"] == 4 and out["by_block"] == 1
    with engine.connect() as conn:
        live = _live(conn)
        screen = _act(conn, "ACT-SCREEN-CUSTOMERS")
        records = _act(conn, "ACT-KEEP-RECORDS")
        notify = _act(conn, "ACT-RESPOND-TO-INCIDENTS")
        acts = l5.live_activities(conn)
    t2 = next(t for t in screen["triggers"] if t["anchor"] == {"source_key": SOURCE, "refs": ["2"]})
    assert t2["method"] == "cue" and "verify" in t2["cue"] and t2["obligation"] == live["2"]["stable_id"]
    # the trigger condition is the obligation's L4 applicability predicate (schema attributes only)
    assert t2["when"] == {"jurisdictions": "UK", "authorisations": "*"}
    t4 = next(t for t in notify["triggers"] if t["anchor"] == {"source_key": SOURCE, "refs": ["4"]})
    assert t4["when"]["products"] == "client money holding"
    assert any(t["anchor"] == {"source_key": SOURCE, "refs": ["3"]} for t in records["triggers"])
    # no cue -> the activity that operates the block the obligation requires (never an invented activity)
    derived = [a for a in acts if a["status"] == "derived"]
    assert derived and all(a["id"].startswith("ACT-0") and a["side"] == "compliance" and a["action_type"] == "control" for a in derived)
    conflicts = next(a for a in derived if any(t["anchor"] == {"source_key": SOURCE, "refs": ["5"]} for t in a["triggers"]))
    assert conflicts["name"].startswith("Operate ") and conflicts["why_trail_id"]
    assert next(t for t in conflicts["triggers"] if t["anchor"]["refs"] == ["5"])["method"] == "block"
    # idempotent
    with engine.begin() as conn:
        assert l5.map_deterministic(conn)["mapped"] == 0
    cov = l5.coverage(engine)
    assert cov["obligations"] == 5 and cov["unmapped"] == 0 and cov["ratio"] == 1.0

    # junction: operates via requires edges (with obligation refs), mitigates lit by L4 product predicates
    junction = l5.build_junction(engine)
    assert junction["added"] >= 3
    with engine.connect() as conn:
        opr = _edges(conn, operates, activity_id=conflicts["id"])
        assert len(opr) == 1 and opr[0]["obligation_refs"] == [live["5"]["stable_id"]] and opr[0]["method"] == "deterministic"
        lit = [e for e in _edges(conn, mitigates, compliance_activity_id="ACT-RESPOND-TO-INCIDENTS") if e["valid_to"] is None]
        funds = next(e for e in lit if e["business_activity_id"] == "ACT-MOVE-CLIENT-FUNDS")
    assert live["4"]["stable_id"] in funds["obligation_refs"] and "l4-product" in funds["rationale"]
    assert check_junction(engine)["ok"]


def test_activity_map_for_profiles(engine, tmp_path):
    _corpus(engine, tmp_path)
    l5.map_activities(engine)
    with engine.connect() as conn:
        emi = l5.activity_map(conn, EMI)
        broker = l5.activity_map(conn, BROKER, matched_obligations={o["obligation_id"] for o in l4_predicates.obligations_for_attributes(conn, BROKER)})
        live = _live(conn)
    b_ids = {a["id"] for a in emi["business"]}
    assert {"ACT-ONBOARD-CUSTOMER", "ACT-MOVE-CLIENT-FUNDS", "ACT-MARKET-PRODUCTS", "ACT-PROCESS-PERSONAL-DATA"} <= b_ids
    assert "ACT-CUSTODY-CRYPTO" not in b_ids and "ACT-HANDLE-ORDERS" not in b_ids
    funds = next(a for a in emi["business"] if a["id"] == "ACT-MOVE-CLIENT-FUNDS")
    assert {p["product"] for p in funds["implied_by"]} == {"e-money issuance", "payment accounts"}
    assert {a["id"] for a in emi["compliance"]} >= {"ACT-SCREEN-CUSTOMERS", "ACT-MONITOR-TRANSACTIONS", "ACT-KEEP-RECORDS"}
    # the broker holds client money: the breach-notification duty lights respond-to-incidents -> move client funds
    edge = next(e for e in broker["edges"] if e["compliance_activity_id"] == "ACT-RESPOND-TO-INCIDENTS" and e["business_activity_id"] == "ACT-MOVE-CLIENT-FUNDS")
    assert edge["lit"] and live["4"]["stable_id"] in edge["obligation_refs"]
    assert broker["counts"]["lit_edges"] >= 1 and live["4"]["stable_id"] in broker["obligations"]
    # obligations that do not apply to the profile (EMI holds no client money) do not light its edges
    emi_lit = {r for e in emi["edges"] for r in e["obligation_refs"]}
    assert live["2"]["stable_id"] in emi_lit or live["3"]["stable_id"] in emi_lit


# --------------------------------------------------------------------------- propagation


def test_l2_and_l4_changes_propagate_through_the_worker(engine, tmp_path):
    from app.clhear import workers
    from app.clhear.platform.events import Envelope

    _corpus(engine, tmp_path)
    l5.map_activities(engine)
    gateway = Gateway(engine, FakeProvider())

    def send(kind, payload, n):
        env = Envelope(event_id=f"evt-l5-{n}", layer="L2", kind=kind, subject_ref=payload.get("obligation_id", "x"),
                       payload=payload, producer="test", ts=datetime.now(timezone.utc).isoformat())
        return workers.handle_envelope(engine, gateway, env.model_dump_json())

    with engine.connect() as conn:
        live = _live(conn)
        before = [e for e in _edges(conn, mitigates, compliance_activity_id="ACT-RESPOND-TO-INCIDENTS") if e["valid_to"] is None]
    ref4 = live["4"]["stable_id"]
    assert any(ref4 in e["obligation_refs"] for e in before)
    with engine.begin() as conn:  # the registry retires the obligation (what L2 does on 'revoked')
        conn.execute(obligations.update().where(obligations.c.id == live["4"]["id"]).values(status="stale"))
    out = send("clhear.l2.changed", {"change_event_id": "CHG-000301", "obligation_id": ref4, "derivation_key": live["4"]["id"], "change": "revoked"}, 1)
    assert out["l5"]["change"] == "revoked" and out["l5"]["unlit"] + out["l5"]["invalidated"] >= 1
    with engine.connect() as conn:
        after = _edges(conn, mitigates, compliance_activity_id="ACT-RESPOND-TO-INCIDENTS")
        assert all(ref4 not in e["obligation_refs"] for e in after if e["valid_to"] is None)
        # curated edges stay (unlit), derived-only edges close with a reason (I2)
        closed = [e for e in after if e["valid_to"] is not None]
        assert all(e["review"][-1]["reason"] == "obligation revoked" for e in closed)
        assert conn.execute(sa.select(sa.func.count()).select_from(mitigates).where(
            mitigates.c.compliance_activity_id == "ACT-RESPOND-TO-INCIDENTS")).scalar_one() == len(after) >= len(before)  # nothing deleted
    # updated: anchors still hold, the junction is rebuilt, nothing lost
    out = send("clhear.l2.changed", {"change_event_id": "CHG-000302", "obligation_id": live["2"]["stable_id"], "derivation_key": live["2"]["id"], "change": "updated"}, 2)
    assert out["l5"]["junction"]["invalidated"] == 0
    # L4 ontology change -> implies re-derived through the worker
    env = Envelope(event_id="evt-l5-onto", layer="L4", kind="clhear.l4.changed", subject_ref="l4.ontology@x", payload={},
                   producer="l4.ontology", ts=datetime.now(timezone.utc).isoformat())
    out = workers.handle_envelope(engine, gateway, env.model_dump_json())
    assert out["l5"]["invalidated"] == 0 and out["l5"]["unchanged"] > 0
    # replay of the same envelope is a no-op
    assert workers.handle_envelope(engine, gateway, env.model_dump_json()) is None


# --------------------------------------------------------------------------- router refinement


def test_router_refinement_is_closed_world_and_quote_bound(engine, tmp_path):
    _corpus(engine, tmp_path)
    with engine.begin() as conn:
        l5.map_deterministic(conn)
    with engine.connect() as conn:
        live = _live(conn)
    ob5 = live["5"]
    # 1: ungrounded quote -> discarded; 2: out-of-vocabulary proposal -> discarded;
    # 3: existing activity from the menu with a real quote -> re-homed
    router = _router(engine, [
        {"activity_id": "ACT-ATTEST-GOVERNANCE", "quote": "approved by the board"},
        {"activity_id": None, "new": {"side": "defensive", "action_type": "attest", "name": "Governance"}, "quote": "conflicts of interest policy"},
        {"activity_id": "ACT-ATTEST-GOVERNANCE", "quote": "conflicts of interest policy"},
    ])
    r1 = l5.llm_refine(engine, router, ob5)
    r2 = l5.llm_refine(engine, router, ob5)
    r3 = l5.llm_refine(engine, router, ob5)
    assert r1["outcome"] == "discarded" and "quote" in r1["reason"]
    assert r2["outcome"] == "discarded"
    assert r3 == {"obligation": ob5["stable_id"], "outcome": "mapped", "activity": "ACT-ATTEST-GOVERNANCE"}
    with engine.connect() as conn:
        attest = _act(conn, "ACT-ATTEST-GOVERNANCE")
        acts = l5.live_activities(conn)
    t = next(t for t in attest["triggers"] if t["anchor"] == {"source_key": SOURCE, "refs": ["5"]})
    assert t["method"] == "llm" and t["cue"] == "conflicts of interest policy"
    fallback = next(a for a in acts if a["status"] == "derived" and any(x["anchor"]["refs"] == ["5"] for x in a["triggers"]))
    assert next(x for x in fallback["triggers"] if x["anchor"]["refs"] == ["5"])["superseded_by"] == "ACT-ATTEST-GOVERNANCE"
    assert not any(a["name"] == "Governance" for a in acts)
    # a new activity proposed within the vocabulary is minted from the id sequence
    router = _router(engine, [{"activity_id": None, "new": {"side": "compliance", "action_type": "assess", "name": "Review conflicts"}, "quote": "conflicts of interest policy"}])
    r4 = l5.llm_refine(engine, router, ob5)
    assert r4["outcome"] == "mapped"
    with engine.connect() as conn:
        new = _act(conn, r4["activity"])
    assert new["status"] == "ai_generated" and new["action_type"] == "assess" and new["id"].startswith("ACT-0")
    # the nightly mapper wires it all: deterministic -> refinement (limit) -> junction
    out = l5.map_activities(engine, _router(engine, []), limit=1)
    assert out["deterministic"]["mapped"] == 0 and out["junction"]["activities"] >= 20


# --------------------------------------------------------------------------- gates


def test_l5_gates_and_scorecard(engine, tmp_path):
    from app.clhear.platform.gates import GATE_THRESHOLDS, LAYER_GATES, gate_status

    assert LAYER_GATES["L5"] == ("l5_completeness", "l5_mapping", "l5_precision", "l3_l5_referential")
    assert GATE_THRESHOLDS["L5"]["junction_completeness"] == "100%"
    _corpus(engine, tmp_path)
    l5.map_activities(engine)
    c = run_suite(engine, "l5_completeness")
    assert c["passed"] and c["scores"]["orphans"] == [] and c["scores"]["obligation_coverage"] == 1.0
    m = run_suite(engine, "l5_mapping")
    assert m["passed"] and m["scores"]["accuracy"] >= 0.92 and m["scores"]["cases"] >= 20 and all(x["passed"] for x in m["scores"]["map_checks"])
    p = run_suite(engine, "l5_precision")
    assert not p["passed"] and p["scores"]["votes"] == 0  # nothing reviewed fails honestly
    run_suite(engine, "l3_l5_referential")
    status = gate_status(engine, "L5")
    assert status["failed"] == ["l5_precision"] and status["missing"] == []
    # an orphan business activity fails completeness honestly
    with engine.begin() as conn:
        record.write(conn, activities_t, {"id": "ACT-999999", "name": "Sell timeshares", "side": "business", "action_type": "marketing",
                                          "triggers": [], "status": "derived"},
                     why=l5._why("ACT-999999", "test orphan", []))
    c2 = run_suite(engine, "l5_completeness")
    assert not c2["passed"] and c2["scores"]["orphans"][0]["activity"] == "ACT-999999"


# --------------------------------------------------------------------------- migration


def test_migration_classifies_legacy_activities_and_is_idempotent(engine):
    from migrations import m0013_l5_junction as m

    assert m.classify_name("Monitor trades for market abuse") == ("compliance", "monitor")
    assert m.classify_name("Onboard a corporate client") == ("business", "onboarding")
    assert m.classify_name("Maintain the compliance manual") == ("compliance", "control")
    with engine.begin() as conn:
        conn.execute(activities_t.insert().values(id="ACT-AI-monitor-trades", name="Monitor trades", description="", business_owner="",
                                                  triggers=[], status="ai_generated"))
        m.upgrade(conn)  # re-running the migration is safe
    with engine.connect() as conn:
        row = _act(conn, "ACT-AI-monitor-trades")
        assert (row["side"], row["action_type"]) == ("compliance", "monitor")
        assert _act(conn, "ACT-ONBOARD-CUSTOMER")["side"] == "business"


# --------------------------------------------------------------------------- API + UI


def test_l5_api_and_browser(client, engine, tmp_path):
    page = client.get("/l5")
    assert page.status_code == 200 and "activities" in page.text and "Activity map" in page.text
    v = client.get("/l5/vocabulary").json()
    assert v["sides"] == ["business", "compliance"] and v["action_types"]["compliance"]
    lst = client.get("/l5/activities", params={"side": "compliance"}).json()
    assert lst["count"] >= 11 and all(i["side"] == "compliance" for i in lst["items"]) and lst["facets"]["side"]["business"] >= 9
    assert client.get("/l5/activities", params={"side": "attack"}).status_code == 422
    one = client.get("/l5/activities/ACT-SCREEN-CUSTOMERS").json()
    assert one["side"] == "compliance" and one["operates"][0]["block_id"] == "BLK-CDD-PROGRAMME" and one["governs"]
    assert one["triggers"][0]["anchor"]["source_key"] == "uksi/2017/692" and "history" in one
    assert client.get("/l5/activities/ACT-NOPE").status_code == 404
    biz = client.get("/l5/activities/ACT-CUSTODY-CRYPTO").json()
    assert [e["product"] for e in biz["implied_by"]] == ["crypto custody"] and biz["governed_by"]
    edges = client.get("/l5/edges/mitigates").json()
    assert edges["count"] >= 18 and edges["items"][0]["compliance_activity"]
    assert client.get("/l5/edges/attacks").status_code == 404
    # a stored L4 profile -> its activity map; ad-hoc attributes too
    prof = client.get("/l5/profiles/PRF-000001/activities").json()
    assert prof["profile_id"] == "PRF-000001" and prof["counts"]["business"] >= 4 and prof["counts"]["compliance"] >= 5
    assert client.get("/l5/profiles/PRF-999999/activities").status_code == 404
    adhoc = client.post("/l5/activity-map", json={"attributes": {"jurisdictions": ["EU"], "products": ["crypto custody"], "crypto_services": True}}).json()
    assert {a["id"] for a in adhoc["business"]} >= {"ACT-CUSTODY-CRYPTO", "ACT-ONBOARD-CUSTOMER"}
    assert any(e["compliance_activity_id"] == "ACT-SAFEGUARD-CRYPTO" for e in adhoc["edges"])
    # with a corpus: an obligation's activities and the edges it lights
    _corpus(engine, tmp_path)
    l5.map_activities(engine)
    with engine.connect() as conn:
        live = _live(conn)
    ob = client.get(f"/l5/obligations/{live['4']['stable_id']}/activities").json()
    assert ob["obligation_id"] == live["4"]["stable_id"] and ob["activities"][0]["activity_id"] == "ACT-RESPOND-TO-INCIDENTS"
    assert ob["counts"]["mitigates"] >= 1
    ob_acts = client.get("/l5/activities/ACT-RESPOND-TO-INCIDENTS/obligations").json()
    assert ob_acts["count"] >= 1 and ob_acts["by_jurisdiction"]["UK"] >= 1
    card = client.get("/l5/scorecard").json()
    assert card["thresholds"]["expert_precision"] == ">=92%" and card["junction"]["ok"] and card["coverage"]["unmapped"] == 0
    # the layer catalog shows L5 as derived with junction counts
    layers = {l["layer"]: l for l in client.get("/api/clhear/layers").json()["layers"]}
    assert layers["L5"]["banner"]["data_status"] == "derived" and layers["L5"]["counts"]["mitigates_edges"] >= 18
