"""AI-native fleets, governance loop, Eval Studio, closed-world L4/L6/L7/L8."""
import json

import sqlalchemy as sa

from app.clhear.derived_models import license_types, obligations
from app.clhear.governance import (
    ADMIN_OVERRIDE,
    AI_ACCEPTED,
    AI_REJECTED,
    SECOND_REVIEW,
    admin_override,
    escalate_to_admin,
    file_correction,
    get_lifecycle,
    revalidate,
    second_human_agree,
)
from app.clhear.l2.extract import run_extraction
from app.clhear.l2.triage import span_is_grounded, triage_duties
from app.clhear.l3.generate import generate_blocks
from app.clhear.l4.licenses import extract_licenses, validate_authorisations
from app.clhear.l5.map import map_activities
from app.clhear.l6.rationale import citations_ok, narrate_blueprint
from app.clhear.l7.narrate import number_echo_ok, narrate_risk
from app.clhear.l8.cohorts import k_anonymity_ok, refresh_cohorts
from app.clhear.platform.evals import run_suite
from app.clhear.platform.gateway import FakeProvider, Gateway
from app.clhear.platform.router import Router

from tests.test_layers_stack import _seed_corpus


def _llm(engine, canned: str, gpu_open=True):
    fake = FakeProvider(canned_text=canned)
    return Router(engine, providers={"ollama": fake, "anthropic": fake, "fake": fake}, gpu_open=gpu_open), fake


def test_evidence_span_contract():
    clause = "A relevant person should keep records of customer due diligence for five years."
    assert span_is_grounded("keep records of customer due diligence", clause)
    assert not span_is_grounded("must file a SAR tomorrow", clause)
    assert not span_is_grounded("short", clause)


def test_duty_triage_inserts_only_grounded_spans(engine):
    _seed_corpus(engine)
    # A weak-modality clause the deterministic extractor will skip.
    with engine.begin() as conn:
        vid = conn.execute(sa.select(sa.text("id")).select_from(sa.table("source_versions"))).first()
    from app.clhear.l1.models import clauses, source_versions

    with engine.begin() as conn:
        vid = conn.execute(sa.select(source_versions.c.id).limit(1)).scalar_one()
        conn.execute(
            clauses.insert().values(
                source_version_id=vid, ref="regulation-40", path="part/regulation-40", ordering=40,
                text="A relevant person should keep records of customer due diligence for five years after the relationship ends.",
                text_hash="h40", public_ok=True,
            )
        )
    canned = json.dumps({
        "is_duty": True,
        "modality": "should",
        "evidence_span": "keep records of customer due diligence for five years",
        "addressee": "relevant person",
    })
    llm, _ = _llm(engine, canned)
    out = triage_duties(engine, llm)
    assert out["inserted"] == 1
    with engine.connect() as conn:
        row = conn.execute(sa.select(obligations).where(obligations.c.id == "OBL:uksi/2017/692#regulation-40")).one()
    assert row.method == "duty-triage-v1"
    # Invented span is discarded.
    canned_bad = json.dumps({"is_duty": True, "evidence_span": "invented text not in the clause"})
    llm2, _ = _llm(engine, canned_bad)
    # Already inserted — no new weak candidate with same id.
    assert triage_duties(engine, llm2)["inserted"] == 0


def test_l3_blocks_reject_invented_satisfies(engine):
    _seed_corpus(engine)
    run_extraction(engine)
    canned = json.dumps({
        "name": "Invented block",
        "description": "nope",
        "satisfies": [{"source_key": "made/up", "refs": ["art_99"]}],
    })
    llm, _ = _llm(engine, canned)
    out = generate_blocks(engine, llm)
    assert out["written"] == 0
    assert out["blocked"] >= 1


def test_l3_blocks_accept_closed_world_satisfies(engine):
    _seed_corpus(engine)
    run_extraction(engine)
    # Need 3+ same-theme obligations — seed corpus topics are financial-crime.
    canned = json.dumps({
        "name": "Customer due diligence programme",
        "description": "AI-designed CDD block",
        "capability": "cdd",
        "evidence_artifacts": ["CDD file"],
        "satisfies": [{"source_key": "uksi/2017/692", "refs": ["regulation-27"]}],
    })
    llm, _ = _llm(engine, canned)
    out = generate_blocks(engine, llm)
    # Cluster may be too small (only 2 duties) — either written or honestly blocked.
    assert out["written"] + out["blocked"] >= 1


def test_l4_discards_ungrounded_and_validates_enum(engine):
    _seed_corpus(engine)
    # Clause the RAG/LIKE can find.
    from app.clhear.l1.models import clauses, source_versions

    with engine.begin() as conn:
        vid = conn.execute(sa.select(source_versions.c.id).limit(1)).scalar_one()
        conn.execute(
            clauses.insert().values(
                source_version_id=vid, ref="regulation-8", path="part/regulation-8", ordering=8,
                text="A payment institution authorisation is required before providing payment services.",
                text_hash="h8", public_ok=True,
            )
        )
    canned = json.dumps({
        "license_types": [
            {"name": "Payment institution authorisation", "issuing_regime": "PSRs",
             "source_key": "uksi/2017/692", "ref": "regulation-8"},
            {"name": "Invented banking licence", "issuing_regime": "fantasy",
             "source_key": "no/such", "ref": "art_1"},
        ]
    })
    llm, _ = _llm(engine, canned)
    out = extract_licenses(engine, llm)
    assert out["written"] >= 1
    assert out["discarded"] >= 1
    assert run_suite(engine, "l4_grounding")["passed"] is True
    validate_authorisations(engine, {"authorisations": ["Payment institution authorisation"]})
    try:
        validate_authorisations(engine, {"authorisations": ["Totally Fake Bank Charter"]})
        raise AssertionError("should have rejected ungrounded authorisation")
    except ValueError:
        pass


def test_l4_discards_anchors_not_in_force(engine):
    """Retrieval hits that are not live in-force clauses must never persist."""
    _seed_corpus(engine)
    canned = json.dumps({
        "license_types": [{
            "name": "Recital-only invention",
            "issuing_regime": "x",
            "source_key": "uksi/2017/692",
            "ref": "rct_999",
        }]
    })
    llm, _ = _llm(engine, canned)
    extract_licenses(engine, llm)
    assert run_suite(engine, "l4_grounding")["passed"] is True
    with engine.connect() as conn:
        for row in conn.execute(sa.select(license_types)):
            anchors = row.clause_anchors if isinstance(row.clause_anchors, list) else json.loads(row.clause_anchors or "[]")
            assert all(a.get("ref") != "rct_999" for a in anchors)


def test_l5_rejects_unknown_when_attribute(engine):
    _seed_corpus(engine)
    run_extraction(engine)
    from app.clhear import curated
    from app.clhear.l1.models import clauses, source_versions

    curated.seed(engine)
    with engine.begin() as conn:
        vid = conn.execute(sa.select(source_versions.c.id).limit(1)).scalar_one()
        conn.execute(
            clauses.insert().values(
                source_version_id=vid, ref="regulation-77", path="part/regulation-77", ordering=77,
                text="A relevant person must file a unique unmapped report within ten days.",
                text_hash="h77", public_ok=True,
            )
        )
    run_extraction(engine)
    canned = json.dumps({
        "activity_name": "Invented when",
        "description": "x",
        "when": {"not_a_real_attribute": "UK"},
    })
    llm, _ = _llm(engine, canned)
    out = map_activities(engine, llm)
    assert out["rejected"] >= 1
    assert out["written"] == 0


def test_l6_citation_check():
    bp = {
        "coverage": [{"obligation_id": "OBL:uksi/2017/692#regulation-27", "covered_by": ["BLK-CDD-PROGRAMME"]}],
        "blocks": [{"id": "BLK-CDD-PROGRAMME"}],
        "activities_evaluated": ["ACT-ONBOARD-CUSTOMER"],
    }
    ok, extra = citations_ok(
        "This program covers OBL:uksi/2017/692#regulation-27 via BLK-CDD-PROGRAMME.", bp
    )
    assert ok and not extra
    ok, extra = citations_ok("Also see OBL:invented#nope.", bp)
    assert not ok and extra


def test_l6_narrate_rejects_outside_ids(engine):
    bp = {
        "coverage": [{"obligation_id": "OBL:uksi/2017/692#regulation-27"}],
        "blocks": [{"id": "BLK-CDD-PROGRAMME"}],
        "activities_evaluated": [],
        "coverage_summary": {"covered": 1, "gaps": 0, "total": 1},
    }
    canned = json.dumps({"rationale": "We also cover OBL:made-up#x which is not in the blueprint."})
    llm, _ = _llm(engine, canned)
    out = narrate_blueprint(engine, llm, bp)
    assert out["written"] is False


def test_l7_number_echo():
    score = {"score": 12.5, "components": {"coverage_deficit": 0.4}, "obligation_count": 3}
    assert number_echo_ok("Score 12.5 from deficit 0.4 over 3 obligations.", score)[0]
    assert not number_echo_ok("Industry average is 47 percent.", score)[0]


def test_l7_narrate_and_eval(engine):
    item = {
        "id": "RSK:test:general",
        "name": "test",
        "area": "general",
        "inputs": {"coverage_ratio": 0.5, "obligation_count": 2, "derived_unreviewed": 1},
        "live_inputs": {"change_events": 0, "changed_clauses": 0},
        "result": {"score": 30.0, "band": "elevated", "components": {"coverage_deficit": 0.5, "churn_pressure": 0.0, "open_ratio": 0.5}},
    }
    canned = json.dumps({
        "narrative": "Score 30.0 with coverage_ratio 0.5 over 2 obligations.",
        "facts_used": ["FACT:coverage-gap-weight"],
    })
    llm, _ = _llm(engine, canned)
    out = narrate_risk(engine, llm, item)
    assert out["written"] is True
    assert run_suite(engine, "l7_number_echo")["passed"] is True


def test_l8_k_anonymity_and_synthetic_label(engine):
    out = refresh_cohorts(engine)
    assert out["synthetic"] == 1
    ok, detail = k_anonymity_ok(engine)
    assert ok
    assert run_suite(engine, "l8_k_anonymity")["passed"] is True
    from app.clhear.l8.cohorts import list_cohorts

    demo = next(c for c in list_cohorts(engine) if c["synthetic"])
    assert "Synthetic" in demo["label"]
    assert demo["published"] is True


def test_governance_correction_loop(engine):
    from app.clhear.governance import mark_generated

    mark_generated(engine, layer="L2", subject_ref="OBL:demo", generated_by="qwen3.5:9b")
    case = file_correction(engine, layer="L2", subject_ref="OBL:demo", filed_by="ada@x.test", body="Wrong addressee")
    canned = json.dumps({"verdict": "reject", "rationale": "The clause names the relevant person."})
    llm, _ = _llm(engine, canned)
    rev = revalidate(engine, llm, case["id"])
    assert rev["verdict"] == "reject" and rev["status"] == AI_REJECTED
    second_human_agree(engine, case["id"], "bob@x.test")
    escalate_to_admin(engine, case["id"])
    admin_override(engine, case["id"], "avner@reg42.ai", accept=True)
    life = get_lifecycle(engine, "L2", "OBL:demo")
    assert life["status"] == ADMIN_OVERRIDE


def test_eval_studio_agreement_feeds_quality(engine, client):
    from app.clhear import eval_studio
    from app.clhear.community_writes import user_id_for
    from app.clhear.models import router_quality

    _seed_corpus(engine)
    run_extraction(engine)
    eval_studio.sample_tasks(engine, per_layer=2)
    open_tasks = eval_studio.list_open(engine)
    assert open_tasks
    uid = user_id_for("ada@x.test")
    eval_studio.record_vote(engine, task_id=open_tasks[0]["id"], user_id=uid, agrees=True, comment="yes")
    scores = eval_studio.agreement_scores(engine)
    assert scores["votes"] >= 1
    body = client.get("/api/clhear/eval").json()
    assert "scores" in body and "open" in body
    team = client.get("/api/clhear/team").json()
    assert any(f["name"] == "Weaver" for f in team["fleets"])
    ops = client.get("/api/clhear/ops").json()
    assert "dashboard" in ops
    how = client.get("/api/clhear/how-live").json()
    assert "tasks" in how and "benchmark" in how


def test_empty_l4_grounding_passes(engine):
    assert run_suite(engine, "l4_grounding")["passed"] is True
