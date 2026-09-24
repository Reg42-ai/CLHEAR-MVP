"""HLD v2 §4.3 done-tests for the NYC L3 building-block catalogue.

Eight kinds with fixed characteristic schemas; deterministic decomposition of
live obligations into `requires` edges with rationale spans and why-trails;
curated anchors as explicit edges; harmonisation into canonical blocks with
edge migration; characteristics backed by spans or explicitly not specified
(+ grounded LLM fill); L2 change propagation via the worker; reuse ratio and
explosion check; the four L3 gate suites; migration backfill; L6 composer
coverage via requires edges; the /l3 API and browser."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import sqlalchemy as sa

from app.clhear.derived_models import activities, blocks, characteristics, l3_kinds, obligations, requires
from app.clhear.l1 import pipeline
from app.clhear.l2.extract import run_extraction
from app.clhear.l3 import characterize as l3_characterize
from app.clhear.l3 import decompose as l3_decompose
from app.clhear.l3 import harmonize as l3_harmonize
from app.clhear.l3.kinds import KINDS, NOT_SPECIFIED, infer_kind, kinds_catalog, required_fields
from app.clhear.models import proposals
from app.clhear.platform import record
from app.clhear.platform.evals import run_suite
from app.clhear.platform.gateway import FakeProvider, Gateway
from app.clhear.platform.ids import next_id
from app.clhear.platform.router import Router
from tests.test_l1_synthetic_amendment import SyntheticAdapter

SOURCE = "synthetic/aml"
AML = {
    "1": "A firm must appoint a money laundering reporting officer with sufficient seniority who reports directly to the board.",
    "2": "A firm must establish and maintain a written anti-money laundering policy approved by senior management and reviewed at least annually.",
    "3": "A firm must maintain an anti-money laundering policy.",
    "4": "A firm must operate a transaction monitoring system capable of detecting unusual patterns.",
    "5": "A firm must hold own funds of at least EUR 125,000 at all times.",
    "6": "This chapter applies to every firm.",
}
STABLE = re.compile(r"^BLK-\d{6}$")


def _ingest(engine, tmp_path, provisions=AML, version="2026-01-01", source_key=SOURCE):
    store = pipeline.LocalStore(tmp_path / "lake")
    return pipeline.ingest(engine, SyntheticAdapter(provisions, version, source_key=source_key), store,
                           gateway=Gateway(engine, FakeProvider()))


def _live(conn, source_key=SOURCE):
    return {
        r["clause_ref"]: dict(r)
        for r in conn.execute(
            sa.select(obligations).where(obligations.c.source_key == source_key, obligations.c.status.in_(("derived", "validated")))
        ).mappings()
    }


def _edges(conn, obligation_id=None, block_id=None, live=True):
    q = sa.select(requires)
    if obligation_id:
        q = q.where(requires.c.obligation_id == obligation_id)
    if block_id:
        q = q.where(requires.c.block_id == block_id)
    if live:
        q = q.where(requires.c.valid_to.is_(None))
    return [dict(r) for r in conn.execute(q.order_by(requires.c.id)).mappings()]


def _block(conn, block_id):
    return dict(conn.execute(sa.select(blocks).where(blocks.c.id == block_id)).mappings().one())


def _chars(conn, block_id, live=True):
    q = sa.select(characteristics).where(characteristics.c.block_id == block_id)
    if live:
        q = q.where(characteristics.c.valid_to.is_(None))
    return {r["key"]: dict(r) for r in conn.execute(q).mappings()}


def _scripted(answers: list[dict]) -> FakeProvider:
    queue = [json.dumps(a) for a in answers]
    return FakeProvider(script=lambda **_: queue.pop(0) if queue else "{}")


def _router(engine, answers: list[dict]) -> Router:
    provider = _scripted(answers)
    return Router(engine, providers={provider.name: provider})


def _seed(engine, tmp_path):
    _ingest(engine, tmp_path)
    run_extraction(engine, source_key=SOURCE)
    return l3_decompose.decompose(engine)


def _curated_block(conn, block_id, name, kind, refs, status="curated"):
    conn.execute(blocks.insert().values(
        id=block_id, name=name, description=f"curated {name}", capability="", evidence_artifacts=[],
        satisfies=[{"source_key": SOURCE, "refs": refs}], implements_controls=[], status=status, kind=kind, purpose=f"curated {name}",
    ))


# --------------------------------------------------------------------------- kinds


def test_kinds_registry_and_inference(engine):
    assert set(KINDS) == {"System", "Document", "Role", "Configuration", "Process", "Workflow", "Asset", "Body"}
    assert required_fields("Process") == ("trigger", "performing_role", "system_or_tool", "cadence", "output", "record")
    assert required_fields("Role") == ("seniority", "independence", "competence", "reporting_line")
    assert infer_kind("A firm must appoint a money laundering reporting officer.") == "Role"
    assert infer_kind("A firm must maintain a written anti-money laundering policy.") == "Document"
    assert infer_kind("A firm must operate a transaction monitoring system.") == "System"
    assert infer_kind("A firm must hold own funds of at least EUR 125,000.") == "Asset"
    assert infer_kind("The board must establish an audit committee.") == "Body"
    assert infer_kind("A firm must report suspicious transactions within 24 hours.") == "Process"
    catalog = kinds_catalog()
    assert [k["kind"] for k in catalog] == list(KINDS) and all(k["fields"] for k in catalog)
    with engine.connect() as conn:  # the migration seeds the schema registry
        rows = {r["kind"]: r for r in conn.execute(sa.select(l3_kinds)).mappings()}
    assert set(rows) == set(KINDS)
    assert [f["key"] for f in rows["Document"]["fields"]] == list(required_fields("Document"))


def test_propose_block_harmonises_names():
    p = {t: l3_decompose.propose_block({"determination": t}) for t in AML.values()}
    assert (p[AML["1"]]["kind"], p[AML["1"]]["name"]) == ("Role", "Money laundering reporting officer")
    assert p[AML["2"]]["name"] == p[AML["3"]]["name"] == "Anti-money laundering policy"  # duty verb + qualifier stripped
    assert (p[AML["4"]]["kind"], p[AML["4"]]["name"]) == ("System", "Transaction monitoring system")
    assert (p[AML["5"]]["kind"], p[AML["5"]]["name"]) == ("Asset", "Own funds")
    body = l3_decompose.propose_block({"determination": "The board must establish an audit committee composed of non-executive directors."})
    assert (body["kind"], body["name"]) == ("Body", "Audit committee")
    assert l3_decompose.name_similarity("Anti-money laundering policy", "Written anti-money laundering policy") >= 0.8
    assert p[AML["1"]]["purpose"].startswith("A firm must appoint")


# --------------------------------------------------------------------------- decomposition


def test_decompose_links_every_obligation_with_rationale_spans_and_why_trails(engine, tmp_path):
    out = _seed(engine, tmp_path)
    assert out["examined"] == 5 and out["linked_derived"] == 5 and out["linked_curated"] == 0
    assert out["blocks_created"] == 4  # provisions 2 and 3 share the AML policy document

    with engine.connect() as conn:
        live = _live(conn)
        assert set(live) == {"1", "2", "3", "4", "5"}
        kinds = {}
        for ref, ob in live.items():
            edges = _edges(conn, obligation_id=ob["id"])
            assert len(edges) == 1, ref
            e = edges[0]
            assert e["id"].startswith("REQ-") and e["method"] == "deterministic"
            assert e["obligation_text_hash"] == ob["text_hash"]
            text = ob["determination"] or ob["statement"]
            assert e["rationale"] == text[e["rationale_start"]:e["rationale_end"]] and "must" in e["rationale"]
            trail = conn.execute(sa.select(record.why_trails).where(record.why_trails.c.id == e["why_trail_id"])).mappings().one()
            assert trail["layer"] == "L3" and trail["agent_id"] == "l3.decompose" and ob["id"] in trail["evidence_refs"]
            b = _block(conn, e["block_id"])
            assert STABLE.match(b["id"]) and b["status"] == "derived" and b["why_trail_id"] and b["canonical_id"] is None
            kinds[ref] = (b["kind"], b["name"])
        assert kinds["1"] == ("Role", "Money laundering reporting officer")
        assert kinds["2"] == kinds["3"] == ("Document", "Anti-money laundering policy")
        assert kinds["4"] == ("System", "Transaction monitoring system")
        assert kinds["5"] == ("Asset", "Own funds")

    comp = l3_decompose.completeness(engine)
    assert comp == {"obligations": 5, "linked": 5, "missing": [], "missing_count": 0, "rate": 1.0}
    again = l3_decompose.decompose(engine)  # idempotent
    assert again == {"examined": 0, "linked_curated": 0, "linked_derived": 0, "blocks_created": 0}


def test_curated_anchor_becomes_requires_edge_and_wins_over_derivation(engine, tmp_path):
    _ingest(engine, tmp_path)
    run_extraction(engine, source_key=SOURCE)
    with engine.begin() as conn:
        _curated_block(conn, "BLK-AML-MLRO", "MLRO appointment", "Role", refs=["1"])
    out = l3_decompose.decompose(engine)
    assert out["linked_curated"] == 1 and out["linked_derived"] == 4 and out["blocks_created"] == 3
    with engine.connect() as conn:
        ob = _live(conn)["1"]
        edges = _edges(conn, obligation_id=ob["id"])
        assert [e["block_id"] for e in edges] == ["BLK-AML-MLRO"] and edges[0]["method"] == "curated-anchor"
        assert not conn.execute(sa.select(blocks.c.id).where(blocks.c.kind == "Role").where(blocks.c.id != "BLK-AML-MLRO")).all()


# --------------------------------------------------------------------------- harmonisation + reuse


def test_harmonize_merges_near_duplicates_and_moves_edges(engine, tmp_path):
    _seed(engine, tmp_path)
    with engine.begin() as conn:
        live = _live(conn)
        keep = next(b for b in conn.execute(sa.select(blocks).where(blocks.c.kind == "Document")).mappings())
        why = l3_decompose.why_for("test", method="test", confidence=1.0, summary="duplicate for harmonisation test")
        dup_id = next_id(conn, "BLK")
        record.write(conn, blocks, {"id": dup_id, "name": "Written anti-money laundering policy", "description": "", "capability": "",
                                    "evidence_artifacts": [], "satisfies": [], "implements_controls": [], "status": "derived",
                                    "kind": "Document", "purpose": "dup"}, why=why, valid_from=datetime.now(timezone.utc).date())
        # move provision 3's edge onto the duplicate and give the duplicate a characteristic
        edge3 = _edges(conn, obligation_id=live["3"]["id"])[0]
        record.invalidate(conn, requires, requires.c.id == edge3["id"], why=why, reason="test")
        l3_decompose.link(conn, obligation=live["3"], block_id=dup_id, method="deterministic", why=why)
    with engine.connect() as conn:
        dup_block = _block(conn, dup_id)
    l3_characterize.characterize_block(engine, dup_block)
    assert l3_harmonize.reuse_ratio(engine)["canonical_blocks"] == 5

    out = l3_harmonize.harmonize(engine)
    assert out == {"merged": 1, "edges_moved": 1}
    with engine.connect() as conn:
        dup = _block(conn, dup_id)
        assert dup["canonical_id"] == keep["id"] and dup["why_trail_id"]  # I2: dropped block stays, pointing at canonical
        assert _edges(conn, obligation_id=live["3"]["id"], block_id=dup_id) == []
        moved = _edges(conn, obligation_id=live["3"]["id"], block_id=keep["id"])
        assert len(moved) == 1 and moved[0]["method"] == "harmonized" and moved[0]["rationale"]
        assert _chars(conn, dup_id) == {} and _chars(conn, dup_id, live=False)  # invalidated, not deleted
        history = _edges(conn, obligation_id=live["3"]["id"], live=False)
        assert len(history) == 3 and sum(e["valid_to"] is None for e in history) == 1

    ratio = l3_harmonize.reuse_ratio(engine)
    assert ratio["canonical_blocks"] == 4 and ratio["live_edges"] == 5 and ratio["obligations_linked"] == 5
    assert ratio["reuse_ratio"] == 1.25 and ratio["explosion"] is False
    assert ratio["by_kind"] == {"Role": 1, "Document": 1, "System": 1, "Asset": 1}
    assert l3_harmonize.harmonize(engine) == {"merged": 0, "edges_moved": 0}


def test_explosion_check_flags_a_block_per_clause(engine, tmp_path):
    _seed(engine, tmp_path)
    with engine.begin() as conn:
        why = l3_decompose.why_for("test", method="test", confidence=1.0, summary="orphan blocks")
        for i in range(3):
            l3_decompose.find_or_create_block(conn, kind="Process", name=f"Unrelated process {i} alpha beta gamma", purpose="x", why=why)
    assert l3_harmonize.reuse_ratio(engine)["explosion"] is True
    assert run_suite(engine, "l3_reuse")["passed"] is False


# --------------------------------------------------------------------------- characteristics


def test_characterize_backs_values_with_spans_or_marks_not_specified(engine, tmp_path):
    _seed(engine, tmp_path)
    out = l3_characterize.characterize(engine)
    assert out["blocks"] == 4 and out["unbacked"] == 0 and out["backed"] >= 5
    with engine.connect() as conn:
        live = _live(conn)
        by_kind = {b["kind"]: dict(b) for b in conn.execute(sa.select(blocks)).mappings()}
        role = _chars(conn, by_kind["Role"]["id"])
        assert set(role) == set(required_fields("Role"))
        assert role["seniority"]["status"] == "backed" and "sufficient seniority" in role["seniority"]["value"]
        assert role["seniority"]["backing_obligation_id"] == live["1"]["id"] and role["seniority"]["value"] in role["seniority"]["backing_span"]
        assert role["reporting_line"]["status"] == "backed" and "board" in role["reporting_line"]["value"]
        assert role["independence"]["status"] == "not_specified" and role["independence"]["value"] == NOT_SPECIFIED
        assert all(r["why_trail_id"] for r in role.values())
        doc = _chars(conn, by_kind["Document"]["id"])
        assert doc["review_cadence"]["value"].lower() == "at least annually" and doc["review_cadence"]["status"] == "backed"
        assert "senior management" in doc["approver"]["value"] and doc["approver"]["backing_obligation_id"] == live["2"]["id"]
        asset = _chars(conn, by_kind["Asset"]["id"])
        assert asset["quantity_or_threshold"]["value"] == "at least EUR 125,000"
    comp = l3_characterize.completeness(engine)
    assert comp["blocks"] == 4 and comp["gap_count"] == 0 and comp["rate"] == 1.0
    assert comp["required"] == sum(len(required_fields(k)) for k in ("Role", "Document", "System", "Asset"))
    assert l3_characterize.characterize(engine) == {"blocks": 0, "backed": 0, "not_specified": 0, "unbacked": 0}  # idempotent


def test_characterize_llm_fill_is_grounded(engine, tmp_path):
    _seed(engine, tmp_path)
    router = _router(engine, [{
        "independence": "not specified by source",
        "competence": "sufficient seniority",  # literal span -> backed
    }])
    with engine.connect() as conn:
        role = dict(conn.execute(sa.select(blocks).where(blocks.c.kind == "Role")).mappings().one())
    counts = l3_characterize.characterize_block(engine, role, llm=router)
    assert counts == {"backed": 3, "not_specified": 1, "unbacked": 0}
    with engine.connect() as conn:
        chars = _chars(conn, role["id"])
        assert chars["competence"]["method"] == "l3.characterize" and chars["competence"]["status"] == "backed"
        assert chars["competence"]["backing_obligation_id"] and chars["competence"]["backing_span"]
        assert chars["independence"]["status"] == "not_specified" and chars["independence"]["method"] == "l3.characterize"
        trail = conn.execute(sa.select(record.why_trails).where(record.why_trails.c.id == chars["competence"]["why_trail_id"])).mappings().one()
        assert trail["model_manifest"]["task"] == "l3.characterize"

    # An invented value is kept but marked unbacked - never presented as fact.
    router = _router(engine, [{"capability": "detecting unusual patterns", "data_inputs": "not specified by source",
                               "retention": "seven years under MiFID II"}])
    with engine.connect() as conn:
        system = dict(conn.execute(sa.select(blocks).where(blocks.c.kind == "System")).mappings().one())
    counts = l3_characterize.characterize_block(engine, system, llm=router)
    assert counts["unbacked"] == 1
    with engine.connect() as conn:
        chars = _chars(conn, system["id"])
        assert chars["retention"]["status"] == "unbacked" and chars["retention"]["backing_obligation_id"] is None
        assert chars["capability"]["status"] == "backed"
    assert run_suite(engine, "l3_characteristics")["passed"] is False  # unbacked rows count against the gate


# --------------------------------------------------------------------------- L2 change propagation


def test_l2_change_propagates_through_worker_envelope(engine, tmp_path):
    from app.clhear import workers
    from app.clhear.platform.events import Envelope

    _seed(engine, tmp_path)
    l3_characterize.characterize(engine)
    assert "clhear.l2.changed" in workers.HANDLERS
    gateway = Gateway(engine, FakeProvider())

    def send(payload, n):
        env = Envelope(event_id=f"evt-l3-{n}", layer="L2", kind="clhear.l2.changed", subject_ref=payload["obligation_id"],
                       payload=payload, producer="l2.change", ts=datetime.now(timezone.utc).isoformat())
        return workers.handle_envelope(engine, gateway, env.model_dump_json())

    with engine.connect() as conn:
        live = _live(conn)
        role_edge = _edges(conn, obligation_id=live["1"]["id"])[0]
        backed_before = [c for c in _chars(conn, role_edge["block_id"]).values() if c["backing_obligation_id"] == live["1"]["id"]]
        assert backed_before

    # revoked -> requires edges closed, characteristics it backed reopened
    out = send({"change_event_id": "CHG-000101", "obligation_id": live["1"]["stable_id"], "derivation_key": live["1"]["id"],
                "change": "revoked", "source": SOURCE}, 1)
    assert out["edges_invalidated"] == 1 and out["characteristics_reopened"] == len(backed_before) and "duration_ms" in out
    with engine.connect() as conn:
        assert _edges(conn, obligation_id=live["1"]["id"]) == []
        closed = _edges(conn, obligation_id=live["1"]["id"], live=False)
        assert closed[0]["review"][-1]["reason"] == "obligation revoked" and closed[0]["review"][-1]["why_trail_id"]
        remaining = _chars(conn, role_edge["block_id"])
        assert all(c["backing_obligation_id"] != live["1"]["id"] for c in remaining.values())
        assert _block(conn, role_edge["block_id"])["valid_to"] is None  # the block itself survives (I2)

    # updated with a changed text hash -> edge re-stamped (old closed, new live with the new hash)
    with engine.begin() as conn:
        conn.execute(obligations.update().where(obligations.c.id == live["2"]["id"]).values(text_hash="sha256:changed"))
    out = send({"change_event_id": "CHG-000102", "obligation_id": live["2"]["stable_id"], "derivation_key": live["2"]["id"],
                "change": "updated", "source": SOURCE}, 2)
    assert out["edges_invalidated"] == 1 and out["edges_relinked"] == 1
    with engine.connect() as conn:
        edges = _edges(conn, obligation_id=live["2"]["id"], live=False)
        assert len(edges) == 2 and edges[0]["valid_to"] is not None and edges[1]["valid_to"] is None
        assert edges[1]["obligation_text_hash"] == "sha256:changed" and edges[1]["block_id"] == edges[0]["block_id"]

    # unknown obligation is ignored, not an error; same event twice is idempotent
    assert send({"change_event_id": "CHG-000103", "obligation_id": "OBL-999999", "change": "revoked"}, 3)["ignored"] is True
    assert send({"change_event_id": "CHG-000101", "obligation_id": live["1"]["stable_id"], "derivation_key": live["1"]["id"],
                 "change": "revoked"}, 1) is None


# --------------------------------------------------------------------------- gates


def test_l3_gate_suites(engine, tmp_path):
    from app.clhear.eval_studio import _insert_task, record_vote
    from app.clhear.platform.gates import LAYER_GATES, gate_status

    assert set(LAYER_GATES["L3"]) == {"l3_completeness", "l3_characteristics", "l3_reuse", "l3_precision", "l3_l5_referential"}
    for suite in ("l3_completeness", "l3_characteristics", "l3_reuse", "l3_precision"):
        assert run_suite(engine, suite)["passed"] is False, suite  # empty catalogue fails honestly

    _seed(engine, tmp_path)
    comp = run_suite(engine, "l3_completeness")
    assert comp["passed"] is True and comp["scores"]["rate"] == 1.0 and comp["scores"]["missing_count"] == 0
    assert run_suite(engine, "l3_characteristics")["passed"] is False  # nothing characterised yet
    l3_characterize.characterize(engine)
    ch = run_suite(engine, "l3_characteristics")
    assert ch["passed"] is True and ch["scores"]["rate"] >= 0.95
    reuse = run_suite(engine, "l3_reuse")
    assert reuse["passed"] is True and reuse["scores"]["explosion"] is False and reuse["scores"]["live_edges"] == 5
    assert run_suite(engine, "l3_precision")["passed"] is False  # no expert votes yet

    with engine.connect() as conn:
        ids = [r[0] for r in conn.execute(sa.select(blocks.c.id))]
    for i, bid in enumerate(ids):
        tid = _insert_task(engine, "L3", "l3.block", bid, "Is this block right?", {"block": bid}, "test-model", "l3.block_generate")
        record_vote(engine, task_id=tid, user_id="expert-1", agrees=True, comment="ok")
    prec = run_suite(engine, "l3_precision")
    assert prec["passed"] is True and prec["scores"]["votes"] == len(ids) and prec["scores"]["precision"] == 1.0
    assert run_suite(engine, "l3_l5_referential")["passed"] is True
    status = gate_status(engine, "L3")
    assert status["passed"] is True and status["missing"] == []

    # Completeness is honest: an obligation whose edge is closed drops the gate.
    with engine.begin() as conn:
        e = _edges(conn)[0]
        record.invalidate(conn, requires, requires.c.id == e["id"], why=e["why_trail_id"], reason="test")
    assert run_suite(engine, "l3_completeness")["passed"] is False


# --------------------------------------------------------------------------- migration backfill


def test_backfill_gives_legacy_blocks_kinds_and_requires_edges(engine, tmp_path):
    _ingest(engine, tmp_path)
    run_extraction(engine, source_key=SOURCE)
    with engine.begin() as conn:
        _curated_block(conn, "BLK-AML-POLICY", "AML policy", "Process", refs=["2", "3"])  # pre-v2 rows defaulted to Process
        conn.execute(blocks.update().where(blocks.c.id == "BLK-AML-POLICY").values(purpose=""))
        conn.execute(blocks.insert().values(
            id="BLK-AI-000009", name="Compliance officer appointment", description="Appoint a compliance officer with seniority",
            capability="role", evidence_artifacts=[], satisfies=[], implements_controls=[], status="derived", kind="Process", purpose="",
        ))
    with engine.begin() as conn:
        out = l3_decompose.backfill_kinds_and_requires(conn)
    assert out == {"kinds_set": 1, "requires": 2}
    with engine.connect() as conn:
        live = _live(conn)
        assert _block(conn, "BLK-AI-000009")["kind"] == "Role"
        assert _block(conn, "BLK-AML-POLICY")["purpose"] == "curated AML policy"
        for ref in ("2", "3"):
            e = _edges(conn, obligation_id=live[ref]["id"])
            assert len(e) == 1 and e[0]["block_id"] == "BLK-AML-POLICY" and e[0]["method"] == "curated-anchor" and e[0]["why_trail_id"]
    with engine.begin() as conn:
        assert l3_decompose.backfill_kinds_and_requires(conn) == {"kinds_set": 0, "requires": 0}  # idempotent


# --------------------------------------------------------------------------- L6 composer uses requires edges


def test_composer_covers_obligations_via_requires_edges(engine, tmp_path):
    from app.clhear.l6.composer import compose

    _seed(engine, tmp_path)
    with engine.begin() as conn:
        conn.execute(activities.insert().values(
            id="ACT-AML-TEST", name="AML programme", description="", business_owner="MLRO", status="curated",
            triggers=[{"anchor": {"source_key": SOURCE, "refs": ["1", "5"]}, "when": {"jurisdictions": "XX"}}],
        ))
    result = compose(engine, {"attributes": {"jurisdictions": ["XX"]}}, log_request=False)
    assert result["coverage_summary"] == {"covered": 2, "gaps": 0, "total": 2}
    by_ref = {c["clause_ref"]: c for c in result["coverage"]}
    with engine.connect() as conn:
        live = _live(conn)
        role_block = _edges(conn, obligation_id=live["1"]["id"])[0]["block_id"]
    assert by_ref["1"]["covered_by"] == [role_block] and by_ref["1"]["state"] == "covered"
    assert {b["id"] for b in result["blocks"]} == {by_ref["1"]["covered_by"][0], by_ref["5"]["covered_by"][0]}

    # A harmonised (merged) block resolves to its canonical block in the blueprint.
    with engine.begin() as conn:
        why = l3_decompose.why_for("test", method="test", confidence=1.0, summary="merge for composer test")
        conn.execute(blocks.update().where(blocks.c.id == role_block).values(canonical_id=by_ref["5"]["covered_by"][0], why_trail_id=why.write(conn)))
    result = compose(engine, {"attributes": {"jurisdictions": ["XX"]}}, log_request=False)
    assert {c["clause_ref"]: c["covered_by"] for c in result["coverage"]}["1"] == by_ref["5"]["covered_by"]


# --------------------------------------------------------------------------- /l3 API + UI


def test_l3_api_kinds_catalogue_detail_and_scorecard(client, engine, tmp_path):
    _seed(engine, tmp_path)
    l3_characterize.characterize(engine)

    # The app seeds the curated catalogue (14 blocks) at startup; 4 derived blocks join it.
    kinds = client.get("/l3/kinds").json()
    assert kinds["count"] == 8 and {k["kind"] for k in kinds["items"]} == set(KINDS)
    assert next(k for k in kinds["items"] if k["kind"] == "Role")["blocks"] == 2  # curated AML governance + derived MLRO

    listing = client.get("/l3/blocks").json()
    assert listing["total"] == 18 and set(listing["facets"]["kinds"]) == set(KINDS) and set(listing["facets"]["statuses"]) == {"curated", "derived"}
    derived = client.get("/l3/blocks", params={"status": "derived"}).json()
    assert derived["total"] == 4
    doc = next(i for i in derived["items"] if i["kind"] == "Document")
    assert doc["obligations"] == 2 and STABLE.match(doc["id"]) and doc["purpose"]
    assert client.get("/l3/blocks", params={"kind": "Role", "status": "derived"}).json()["total"] == 1
    assert client.get("/l3/blocks", params={"q": "transaction monitoring system"}).json()["total"] == 1
    assert client.get("/l3/blocks", params={"kind": "Nope"}).status_code == 422
    curated = client.get("/l3/blocks/BLK-CDD-PROGRAMME").json()
    assert curated["status"] == "curated" and curated["kind"] == "Process" and curated["backing_obligations"] == []
    assert all(c["status"] == "not_specified" for c in curated["characteristics"])

    detail = client.get(f"/l3/blocks/{doc['id']}").json()
    assert detail["id"] == doc["id"] and detail["kind"] == "Document"
    assert [c["key"] for c in detail["characteristics"]] == list(required_fields("Document"))
    review = next(c for c in detail["characteristics"] if c["key"] == "review_cadence")
    assert review["status"] == "backed" and review["backing_obligation_id"].startswith("OBL-") and review["backing_span"]
    assert detail["characteristics_summary"]["filled"] == detail["characteristics_summary"]["required"] == 4
    assert len(detail["backing_obligations"]) == 2
    bo = detail["backing_obligations"][0]
    assert bo["obligation_id"].startswith("OBL-") and bo["span"]["text"] == bo["determination"][bo["span"]["start"]:bo["span"]["end"]]
    assert bo["method"] == "deterministic" and bo["basis_current"] is True and bo["live"] is True
    assert detail["why"] and all(w["layer"] == "L3" for w in detail["why"])
    assert detail["needed_by"] == [] and detail["merged_duplicates"] == []
    assert client.get(f"/l3/blocks/{doc['id']}/history").json()["requires"]
    assert client.get(f"/l3/blocks/{doc['id']}/why").json()["why"]
    assert client.get("/l3/blocks/BLK-999999").status_code == 404

    with engine.connect() as conn:
        ob = _live(conn)["4"]
    for ref in (ob["stable_id"], ob["id"]):
        from urllib.parse import quote

        r = client.get(f"/l3/obligations/{quote(ref, safe='')}/blocks").json()
        assert r["obligation_id"] == ob["stable_id"] and r["count"] == 1 and r["items"][0]["block"]["kind"] == "System"
        assert r["items"][0]["span"] and r["items"][0]["rationale"]
    assert client.get("/l3/obligations/OBL-999999/blocks").status_code == 404

    card = client.get("/l3/scorecard").json()
    assert card["gate"]["layer"] == "L3" and set(card["thresholds"]) == {"obligation_to_block", "characteristic_completeness", "expert_precision"}
    assert card["blocks"]["canonical"] == 18 and card["blocks"]["by_status"] == {"curated": 14, "derived": 4}
    assert card["completeness"]["rate"] == 1.0 and card["characteristics"]["rate"] == 1.0
    assert card["reuse"]["live_edges"] == 5 and card["requires_by_method"] == {"deterministic": 5}


def test_l3_api_modification_request_files_a_proposal(client, engine, tmp_path):
    _seed(engine, tmp_path)
    bid = client.get("/l3/blocks", params={"kind": "Role", "status": "derived"}).json()["items"][0]["id"]
    r = client.post(f"/l3/blocks/{bid}/modification-requests", json={
        "field": "characteristic:reporting_line", "proposed_value": "the audit committee",
        "rationale": "Article 4 requires the MLRO to report to the audit committee, not the board.", "requester": "expert@example.org"})
    assert r.status_code == 201 and r.json()["status"] == "proposed"
    with engine.connect() as conn:
        p = conn.execute(sa.select(proposals).where(proposals.c.id == r.json()["proposal_id"])).mappings().one()
    assert p["layer"] == "L3" and p["kind"] == "l3_modification" and p["subject_ref"] == bid
    draft = p["draft"] if isinstance(p["draft"], dict) else json.loads(p["draft"])
    assert draft["field"] == "characteristic:reporting_line" and draft["proposed_value"] == "the audit committee"
    with engine.connect() as conn:  # the catalogue row is untouched (proposal only)
        assert _block(conn, bid)["kind"] == "Role"
    assert client.post(f"/l3/blocks/{bid}/modification-requests", json={
        "field": "kind", "proposed_value": "Widget", "rationale": "kind is wrong here"}).status_code == 422
    assert client.post(f"/l3/blocks/{bid}/modification-requests", json={
        "field": "characteristic:cadence", "proposed_value": "x", "rationale": "not a Role field"}).status_code == 422


def test_l3_browser_page_is_served(client):
    r = client.get("/l3")
    assert r.status_code == 200 and "L3 · building blocks" in r.text and "/l3/blocks" in r.text
