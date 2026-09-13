"""HLD v2 §4.8 — L8 fills, maturity, member benchmarks and open-by-mode (I9).

Fills are drafted per block slot from the block, its characteristics and the
obligations that require it (deterministic templates; the ``l8.fill`` task when a
model is on the router), reviewed against the five-criterion rubric (≥ 85 % endorses),
and re-derived when their basis drifts — every version stays on the record (I2) and
traces to a live block and obligations (I3). Member benchmarks publish only k ≥ 5
cohorts with Laplace noise, refuse differencing pairs, and the automated
re-identification test gates the layer. Content is members-only; existence and
maturity are public metadata.
"""
from __future__ import annotations

import random

import pytest
import sqlalchemy as sa

from app.clhear.derived_models import blocks, obligations
from app.clhear.l8 import aggregate as agg
from app.clhear.l8 import fills as fl
from app.clhear.l8.models import K_MIN, RUBRIC_CRITERIA, RUBRIC_MIN, benchmark_aggregates, benchmark_inputs, fills
from app.clhear.platform import evals as ev
from app.clhear.platform import mode as l8_mode
from app.clhear.platform import record
from app.clhear.platform.gates import LAYER_GATES, gate_status
from tests.test_l3_blocks import _seed

MAINT = "avner@reg42.ai"
ADA = "ada@example.com"
GOOD = {c: 0.9 for c in RUBRIC_CRITERIA}
WEAK = {c: 0.7 for c in RUBRIC_CRITERIA}


def _fills(engine, tmp_path):
    _seed(engine, tmp_path)
    return fl.generate_fills(engine)


# --------------------------------------------------------------------------- drafting


def test_generate_fills_one_per_slot_traced_to_block_and_obligations(engine, tmp_path):
    out = _fills(engine, tmp_path)
    assert out["drafted"] == 6 and out["llm"] == 0 and out["method"] == "fills-v1"
    rows = fl.list_fills(engine)
    assert {(r["block_id"], r["slot"], r["kind"]) for r in rows} == {
        ("BLK-000001", "role_description", "text"), ("BLK-000002", "policy_text", "text"), ("BLK-000002", "review_cycle", "numeric"),
        ("BLK-000003", "control_configuration", "text"), ("BLK-000003", "typology_set", "item_set"), ("BLK-000004", "register_fields", "item_set")}
    for r in rows:
        assert r["id"].startswith("FIL-") and r["maturity"] == "draft" and r["provenance"] == "agent" and r["model"] == "deterministic"
        assert r["obligation_ids"] and r["basis_hash"] and r["why_trail_id"] and r["version"] == 1
    with engine.connect() as conn:
        trails = {r["id"]: r for r in conn.execute(sa.select(record.why_trails).where(record.why_trails.c.layer == "L8")).mappings()}
    for r in rows:
        t = trails[r["why_trail_id"]]
        assert t["agent_id"] == "l8.fill" and r["block_id"] in [e.get("block") for e in t["evidence_refs"] if isinstance(e, dict)]
    role = next(r for r in rows if r["slot"] == "role_description")
    assert role["content"]["text"].startswith("# Money laundering reporting officer") and "## Accountabilities" in role["content"]["text"]
    policy = next(r for r in rows if r["slot"] == "policy_text")
    assert "## Policy statement" in policy["content"]["text"] and "reviewed at least annually" in policy["content"]["text"]
    cycle = next(r for r in rows if r["slot"] == "review_cycle")
    assert cycle["content"]["stated"] is False and cycle["content"]["unit"] == "months"  # no obligation states a figure
    typ = next(r for r in rows if r["slot"] == "typology_set")
    assert typ["content"]["items"] and all("obligation" in it for it in typ["content"]["items"])
    # idempotent: a second pass drafts nothing new
    again = fl.generate_fills(engine)
    assert again["drafted"] == 0 and again["skipped_existing"] == 6
    assert fl.traceability(engine) == {"fills": 6, "traced": 6, "untraced": [], "ratio": 1.0}


def test_numeric_fill_quotes_a_figure_stated_in_an_obligation():
    obs = [{"id": "OB-1", "stable_id": "SYN 1", "statement": "A firm must report suspicious transactions within 5 business days."}]
    c = fl.draft_content("escalation_sla", "numeric", {"id": "BLK-x", "name": "SAR process", "kind": "Process"}, {}, obs)
    assert c == {"value": 5.0, "unit": "business days", "range": [5.0, 5.0], "basis": "stated in SYN 1: “5 business days”", "stated": True,
                 "obligation": "OB-1"}
    wf = fl.draft_content("procedure_steps", "workflow", {"id": "BLK-x", "name": "SAR process", "kind": "Process"}, {}, obs)
    assert wf["steps"][0]["order"] == 1 and wf["steps"][0]["obligation"] == "OB-1"
    with pytest.raises(fl.InvalidFill):
        fl.draft_content("x", "nope", {"id": "b", "name": "n", "kind": "Process"}, {}, obs)


def test_create_fill_validates_and_writes_through_the_record_path(engine, tmp_path):
    _seed(engine, tmp_path)
    with pytest.raises(fl.InvalidFill):
        fl.create_fill(engine, block_id="BLK-000001", slot="x", kind="poem", content={})
    with pytest.raises(fl.InvalidFill):
        fl.create_fill(engine, block_id="BLK-999999", slot="x", kind="text", content={"text": "x"})
    row = fl.create_fill(engine, block_id="BLK-000001", slot="onboarding_note", kind="text", content={"text": "Contributed guidance."},
                         provenance="contributor", contributor="ada", contribution_id="CON-000001", model="human")
    assert row["provenance"] == "contributor" and row["contributor"] == "ada" and row["jurisdictions"] and row["why"]["layer"] == "L8"
    with pytest.raises(record.DeletionForbidden):  # fills are record rows, never a rebuildable projection (I2)
        with engine.begin() as conn:
            record.rebuild_projection(conn, fills, fills.c.id == row["id"])
    assert fills in record.layer_tables() and benchmark_aggregates in record.layer_tables()


# --------------------------------------------------------------------------- review + maturity


def test_rubric_review_moves_maturity_and_versions_the_fill(engine, tmp_path):
    _fills(engine, tmp_path)
    fid = fl.list_fills(engine)[0]["id"]
    assert fl.rubric_score(GOOD) == 0.9 and fl.rubric_score(WEAK) == 0.7
    with pytest.raises(fl.InvalidFill):
        fl.rubric_score({"accuracy": 1.0})
    with pytest.raises(fl.InvalidFill):
        fl.rubric_score({**GOOD, "accuracy": 1.5})
    with pytest.raises(fl.NotEndorsable):
        fl.review_fill(engine, fid, reviewer=MAINT, rubric=WEAK, decision="endorse")
    revised = fl.review_fill(engine, fid, reviewer=MAINT, rubric=WEAK, decision="revise", note="tighten the ownership section")
    assert revised["maturity"] == "reviewed" and revised["rubric_score"] == 0.7 and revised["version"] == 3 and len(revised["reviews"]) == 1
    endorsed = fl.review_fill(engine, fid, reviewer=ADA, rubric=GOOD, decision="endorse")
    assert endorsed["maturity"] == "endorsed" and endorsed["rubric_score"] == 0.9 and endorsed["version"] == 5 and len(endorsed["reviews"]) == 2
    hist = fl.history(engine, fid)
    assert [(h["version"], h["maturity"], h["valid_to"] is None) for h in hist] == [(2, "draft", False), (4, "reviewed", False), (5, "endorsed", True)]
    assert all("invalidated" in str(h["review"]) for h in hist[:-1])  # nothing deleted; closed rows carry the reason (I2)
    assert fl.summary(engine)["by_maturity"] == {"draft": 5, "reviewed": 0, "endorsed": 1}
    assert fl.rubric_gate(engine) == {"endorsed": 1, "below_threshold": [], "threshold": RUBRIC_MIN, "ok": True}
    rejected = fl.review_fill(engine, fid, reviewer=MAINT, rubric=WEAK, decision="reject", note="duplicates the policy fill")
    assert rejected["status"] == "superseded" and fl.get(engine, fid)["status"] == "superseded"
    assert fid not in {r["id"] for r in fl.list_fills(engine)}  # no longer current — but still on the record
    with pytest.raises(KeyError):
        fl.review_fill(engine, "FIL-999999", reviewer=MAINT, rubric=GOOD, decision="endorse")


def test_rubric_gate_catches_an_endorsed_fill_without_a_qualifying_review(engine, tmp_path):
    _fills(engine, tmp_path)
    fid = fl.list_fills(engine)[0]["id"]
    with engine.begin() as conn:  # a hand-edit around the review path
        conn.execute(fills.update().where(fills.c.id == fid).values(maturity="endorsed", rubric_score=0.6))
    gate = fl.rubric_gate(engine)
    assert gate["ok"] is False and gate["below_threshold"] == [fid]
    scores, passed = ev.SUITES["l8_fill_rubric"](engine, None)
    assert passed is False and scores["below_threshold"] == [fid]


# --------------------------------------------------------------------------- drift


def test_drift_rederives_as_a_new_draft_version_and_retires_orphans(engine, tmp_path):
    _fills(engine, tmp_path)
    fid = next(r["id"] for r in fl.list_fills(engine) if r["block_id"] == "BLK-000001")
    fl.review_fill(engine, fid, reviewer=MAINT, rubric=GOOD, decision="endorse")
    assert fl.detect_drift(engine)["drifted"] == 0
    with engine.begin() as conn:
        conn.execute(blocks.update().where(blocks.c.id == "BLK-000001").values(name="Nominated officer"))
    out = fl.detect_drift(engine)
    assert out["drifted"] == 1 and out["details"][0] == {"id": fid, "action": "rederived", "reason": "block or obligation text changed",
                                                         "previous_maturity": "endorsed"}
    now = fl.get(engine, fid)
    assert now["maturity"] == "draft" and now["rubric_score"] is None and now["drift"]["previous_maturity"] == "endorsed"
    assert now["content"]["text"].startswith("# Nominated officer") and now["version"] == 5
    assert fl.rubric_gate(engine)["endorsed"] == 0  # endorsement never survives a changed basis
    # an obligation retiring changes the basis too
    with engine.begin() as conn:
        ob = now["obligation_ids"][0]
        conn.execute(obligations.update().where(obligations.c.id == ob).values(valid_to=sa.func.current_date()))
    out = fl.detect_drift(engine)
    assert out["drifted"] >= 1 and fl.get(engine, fid)["obligation_ids"] == []
    # a block that is no longer current retires its fills (superseded, not deleted)
    with engine.begin() as conn:
        conn.execute(blocks.update().where(blocks.c.id == "BLK-000004").values(valid_to=sa.func.current_date()))
    out = fl.detect_drift(engine)
    retired = [d for d in out["details"] if d["action"] == "retired"]
    assert retired and retired[0]["reason"] == "block no longer current"
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(fills)).scalar() >= 10  # every version kept
    # traceability flags the fill whose obligation retired
    trace = fl.traceability(engine)
    assert trace["ratio"] < 1.0 and any(fid == u["id"] for u in trace["untraced"])
    _, passed = ev.SUITES["l8_traceability"](engine, None)
    assert passed is False


def test_availability_is_public_metadata_without_content(engine, tmp_path):
    _fills(engine, tmp_path)
    a = fl.availability(engine)
    assert a["count"] == 4 and a["blocks"][0]["block_id"] == "BLK-000001" and a["blocks"][0]["fills_available"] is True
    assert a["blocks"][1]["fills"] == 2 and a["blocks"][1]["by_maturity"] == {"draft": 2, "reviewed": 0, "endorsed": 0}
    assert "## Accountabilities" not in str(a) and all("content" not in b for b in a["blocks"])  # metadata only, never the text
    only = fl.availability(engine, block_ids=["BLK-000003"])
    assert only["count"] == 1 and {s["slot"] for s in only["blocks"][0]["slots"]} == {"control_configuration", "typology_set"}


# --------------------------------------------------------------------------- benchmarks: k-anon + DP + re-identification


def _observe(engine, cohort, n, base=300.0, metric="cdd_refresh_days", prefix="m"):
    for i in range(n):
        agg.submit_input(engine, member_id=f"{prefix}{i}@example.com", cohort_key=cohort, metric=metric, value=base + 10 * i)


def test_inputs_are_hashed_validated_and_never_served(engine, monkeypatch):
    monkeypatch.setenv("CLHEAR_BENCHMARK_HMAC_KEY", "test-key")
    from app.clhear.settings import get_settings

    get_settings.cache_clear()
    out = agg.submit_input(engine, member_id="Ada@Example.com", cohort_key=" UK | payments ", metric="cdd_refresh_days", value=365)
    assert out["accepted"] and out["cohort_key"] == "UK|payments" and "hash" not in out
    with pytest.raises(agg.InvalidInput):
        agg.submit_input(engine, member_id=ADA, cohort_key="UK", metric="shoe_size", value=1)
    with pytest.raises(agg.InvalidInput):
        agg.submit_input(engine, member_id=ADA, cohort_key="UK", metric="cdd_refresh_days", value=99999)
    with pytest.raises(agg.InvalidInput):
        agg.submit_input(engine, member_id=ADA, cohort_key="", metric="cdd_refresh_days", value=1)
    with engine.connect() as conn:
        row = conn.execute(sa.select(benchmark_inputs)).mappings().one()
    assert row["member_hash"] == agg.member_hash("ada@example.com") and len(row["member_hash"]) == 64
    assert "ada" not in row["member_hash"] and row["member_hash"] != agg.member_hash("bob@example.com")
    assert agg.list_aggregates(engine) == [] and agg.summary(engine)["aggregates"] == 0
    assert agg.summary(engine)["metrics"][0] == {"metric": "cdd_refresh_days", "label": agg.METRICS["cdd_refresh_days"]["label"],
                                                 "unit": "days", "published_cohorts": 0, "cohorts_collecting": 1}


def test_aggregate_publishes_only_k_cohorts_with_laplace_noise(engine):
    _observe(engine, "UK|payments", 6)
    _observe(engine, "UK|banks", K_MIN - 1, prefix="b")
    with pytest.raises(agg.InvalidInput):
        agg.aggregate(engine, k=2)
    out = agg.aggregate(engine, seed=7, release="rel-1")
    assert out["published"] == 1 and out["suppressed"] == 1 and out["suppressed_detail"][0]["cohort_key"] == "UK|banks"
    rows = agg.list_aggregates(engine)
    assert len(rows) == 1
    a = rows[0]
    assert a["id"].startswith("BMA-") and a["n"] == 6 and a["k_threshold"] == K_MIN and a["release"] == "rel-1" and a["unit"] == "days"
    assert a["noise"]["mechanism"] == "laplace_histogram" and a["noise"]["sensitivity"] == 1 and "membership" not in a["noise"]
    st = a["statistics"]
    assert 0 <= st["mean"] <= 1825 and len(st["histogram"]) == 10 and st["p50"] % st["bucket_width"] == pytest.approx(st["bucket_width"] / 2)
    raw = {300.0, 310.0, 320.0, 330.0, 340.0, 350.0}
    assert not ({st["mean"], st["p50"], st["p90"]} & raw)  # nothing echoed
    # a re-run with an unchanged cohort writes nothing new; a new member re-versions the row
    assert agg.aggregate(engine, seed=8)["published"] == 0
    agg.submit_input(engine, member_id="m6@example.com", cohort_key="UK|payments", metric="cdd_refresh_days", value=400)
    out = agg.aggregate(engine, seed=9)
    assert out["published"] == 1 and out["superseded"] == 1
    again = agg.get_aggregate(engine, a["id"])
    assert again["n"] == 7 and again["version"] == 3 and again["versions"] == 2 and again["why"]["layer"] == "L8"
    with engine.connect() as conn:
        closed = conn.execute(sa.select(benchmark_aggregates).where(benchmark_aggregates.c.valid_to.isnot(None))).mappings().all()
    assert len(closed) == 1 and closed[0]["n"] == 6
    # different seeds, different noise — the mechanism is randomised, not a deterministic rounding
    rng_a, rng_b = random.Random(1), random.Random(2)
    sa_, _ = agg._noised_statistics([300.0] * 6, [0, 1825], 1.0, rng_a)
    sb_, _ = agg._noised_statistics([300.0] * 6, [0, 1825], 1.0, rng_b)
    assert sa_["histogram"] != sb_["histogram"]


def test_differencing_attack_is_refused_at_publish_time_and_caught_by_the_gate(engine):
    _observe(engine, "UK|payments", 6)
    assert agg.aggregate(engine, seed=1)["published"] == 1
    _observe(engine, "UK", 7)  # superset: UK minus UK|payments would isolate one member
    out = agg.aggregate(engine, seed=2)
    assert out["published"] == 0 and out["suppressed_detail"][0]["reason"].startswith("differencing risk")
    rt = agg.reidentification_test(engine)
    assert rt["passed"] is True and rt["aggregates"] == 1 and rt["checks"] == ["k_anonymity", "raw_echo", "differencing", "identity_in_store"]
    # force the second aggregate onto the record the way a bug would, and the gate must catch it
    with engine.begin() as conn:
        why = record.WhyTrail(layer="L8", subject_ref="UK/cdd_refresh_days", reasoning_summary="test: bypass", agent_id="test")
        record.write(conn, benchmark_aggregates, {"id": "BMA-TEST", "cohort_key": "UK", "metric": "cdd_refresh_days", "block_id": None, "n": 7,
                                                  "k_threshold": K_MIN, "epsilon": 1.0, "statistics": {"mean": 330.0}, "noise": {}, "unit": "days",
                                                  "release": "", "status": "current"}, why=why)
    rt = agg.reidentification_test(engine)
    checks = {f["check"] for f in rt["findings"]}
    assert rt["passed"] is False and "differencing" in checks and "raw_echo" in checks  # 330.0 is also a raw input
    scores, passed = ev.SUITES["l8_reidentification"](engine, None)
    assert passed is False and scores["findings"]
    # the layer gate reflects it
    ev.run_suite(engine, "l8_reidentification", release="r")
    status = gate_status(engine, "L8", release="r")
    assert status["passed"] is False and "l8_reidentification" in status["failed"]


def test_reidentification_flags_a_sub_k_aggregate(engine):
    _observe(engine, "UK|payments", 6)
    agg.aggregate(engine, seed=3)
    with engine.begin() as conn:
        conn.execute(benchmark_aggregates.update().values(n=3))
    rt = agg.reidentification_test(engine)
    assert rt["passed"] is False and rt["findings"][0]["check"] == "k_anonymity"


def test_l8_gate_is_registered_and_passes_on_a_clean_store(engine, tmp_path):
    assert LAYER_GATES["L8"] == ("l8_reidentification", "l8_traceability", "l8_fill_rubric", "l8_k_anonymity")
    assert set(LAYER_GATES["L8"]) <= set(ev.SUITES) and set(LAYER_GATES["L8"]) <= set(ev.gate_suites())
    _fills(engine, tmp_path)
    _observe(engine, "UK|payments", 6)
    agg.aggregate(engine, seed=4)
    for suite in LAYER_GATES["L8"]:
        assert ev.run_suite(engine, suite, release="r")["passed"], suite
    assert gate_status(engine, "L8", release="r")["passed"] is True


# --------------------------------------------------------------------------- mode + API


def test_membership_and_request_mode(engine, monkeypatch):
    assert l8_mode.deployment_mode() == "agnostic" and not l8_mode.is_instance()
    with engine.connect() as conn:
        assert not l8_mode.is_member(conn, email=ADA) and not l8_mode.is_member(conn)
    m = l8_mode.grant_membership(engine, email="Ada@Example.com", granted_by=MAINT, org_label="Ada Ltd")
    assert m["email"] == ADA and m["plan"] == "member" and m["user_id"]
    assert l8_mode.grant_membership(engine, email=ADA, granted_by=MAINT)["id"] == m["id"]  # idempotent
    with engine.connect() as conn:
        assert l8_mode.is_member(conn, email=ADA) and l8_mode.is_member(conn, user_id=m["user_id"])
    assert [x["email"] for x in l8_mode.list_members(engine)] == [ADA]
    assert l8_mode.revoke_membership(engine, email=ADA) == 1 and l8_mode.list_members(engine) == []
    with engine.connect() as conn:
        assert not l8_mode.is_member(conn, email=ADA)
    monkeypatch.setenv("CLHEAR_MODE", "instance")
    from app.clhear.settings import get_settings

    get_settings.cache_clear()
    assert l8_mode.deployment_mode() == "instance" and l8_mode.is_instance()


def _login(client, email):
    resp = client.post("/auth/email", json={"email": email})
    link = resp.json()["debug_link"]
    assert client.get(link.split("clhear.reg42.ai")[1], follow_redirects=False).status_code == 307


def test_api_open_by_mode(client, engine, tmp_path, monkeypatch):
    monkeypatch.setenv("CLHEAR_AUTH_DEBUG", "true")
    from app.clhear.settings import get_settings

    get_settings.cache_clear()
    _fills(engine, tmp_path)
    _observe(engine, "UK|payments", 6)
    agg.aggregate(engine, seed=5)
    fid = fl.list_fills(engine)[0]["id"]

    page = client.get("/l8")
    assert page.status_code == 200 and 'id="main"' in page.text and "Member content" in page.text
    # public metadata in agnostic mode
    m = client.get("/l8/mode").json()
    assert m["mode"] == "agnostic" and m["signed_in"] is False and "L8 fills" in m["closed"]
    av = client.get("/l8/availability?block=BLK-000002").json()
    assert av["count"] == 1 and av["blocks"][0]["fills"] == 2
    s = client.get("/l8/summary").json()
    assert s["fills"]["fills"] == 6 and s["benchmarks"]["aggregates"] == 1 and s["method"]["k_min"] == K_MIN
    assert client.get("/l8/metrics").json()["metrics"][0]["metric"] == "cdd_refresh_days"
    # content is closed
    for path in ("/l8/fills", f"/l8/fills/{fid}", f"/l8/fills/{fid}/history", "/l8/benchmarks"):
        r = client.get(path)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "members_only", path
    assert client.post("/l8/benchmarks/inputs", json={"cohort_key": "UK", "metric": "cdd_refresh_days", "value": 1}).status_code == 403
    assert client.get("/l8/members").status_code == 401

    # a signed-in non-member is still agnostic
    _login(client, ADA)
    assert client.get("/l8/mode").json() == {**client.get("/l8/mode").json(), "mode": "agnostic", "signed_in": True}
    assert client.get("/l8/fills").status_code == 403 and client.get("/l8/members").status_code == 403

    # a maintainer grants membership (header identity; session cookie must not be in the way)
    client.post("/auth/logout")
    r = client.post("/l8/members", json={"email": ADA, "org_label": "Ada Ltd"}, headers={"X-Reg42-User": MAINT})
    assert r.status_code == 201 and r.json()["email"] == ADA
    assert client.get("/l8/members", headers={"X-Reg42-User": MAINT}).json()["count"] == 1

    # the member reads content
    _login(client, ADA)
    assert client.get("/l8/mode").json()["mode"] == "member"
    lst = client.get("/l8/fills?block=BLK-000002").json()
    assert lst["count"] == 2 and lst["mode"] == "member" and all("content" in f for f in lst["fills"])
    one = client.get(f"/l8/fills/{fid}").json()
    assert one["id"] == fid and one["why"]["layer"] == "L8" and one["reviews"] == []
    assert client.get(f"/l8/fills/{fid}/history").json()["versions"][0]["version"] == 1
    assert client.get("/l8/fills/FIL-999999").status_code == 404
    b = client.get("/l8/benchmarks?metric=cdd_refresh_days").json()
    assert b["count"] == 1 and b["aggregates"][0]["n"] == 6 and "membership" not in b["aggregates"][0]["noise"]
    assert client.get(f"/l8/benchmarks/{b['aggregates'][0]['id']}").json()["why"]["agent_id"] == "l8.aggregate"
    r = client.post("/l8/benchmarks/inputs", json={"cohort_key": "UK|payments", "metric": "cdd_refresh_days", "value": 420})
    assert r.status_code == 201 and r.json()["accepted"] is True
    assert client.post("/l8/benchmarks/inputs", json={"cohort_key": "UK", "metric": "nope", "value": 1}).status_code == 422
    # a member without the reviewer role cannot review; a reviewer can
    body = {"rubric": GOOD, "decision": "endorse", "note": "solid"}
    assert client.post(f"/l8/fills/{fid}/reviews", json=body).status_code == 403
    from app.clhear.platform import contributions as contrib

    contrib.grant_role(engine, email=ADA, role="reviewer", granted_by=MAINT)
    r = client.post(f"/l8/fills/{fid}/reviews", json=body)
    assert r.status_code == 201 and r.json()["maturity"] == "endorsed" and r.json()["reviews"][0]["reviewer"] == ADA
    weak = client.post(f"/l8/fills/{fid}/reviews", json={"rubric": WEAK, "decision": "endorse"})
    assert weak.status_code == 422 and weak.json()["detail"]["code"] == "below_rubric"
    # existence + maturity now public
    assert client.get("/l8/availability").json()["blocks"][0]["endorsed"] == 1

    # revoke → closed again
    client.post("/auth/logout")
    assert client.post("/l8/members/revoke", json={"email": ADA}, headers={"X-Reg42-User": MAINT}).json()["revoked"] == 1
    _login(client, ADA)
    assert client.get("/l8/fills").status_code == 403


def test_instance_mode_opens_l8_without_membership(client, engine, tmp_path, monkeypatch):
    monkeypatch.setenv("CLHEAR_MODE", "instance")
    from app.clhear.settings import get_settings

    get_settings.cache_clear()
    _fills(engine, tmp_path)
    assert client.get("/l8/mode").json()["mode"] == "instance"
    assert client.get("/l8/fills").json()["count"] == 6
