"""HLD v2 §8 item 11 — the approval console at ``/console``.

Three queues: low-confidence determinations per layer threshold (I4),
modification requests (l2/l3 + second-model escalations) and contribution
reviews. Every decision needs a maintainer identity, is applied through the
record write path (never a delete) and lands in ``human_edits``.
"""
from __future__ import annotations

from urllib.parse import quote

import sqlalchemy as sa

from app.clhear.derived_models import blocks, characteristics, obligation_reviews, obligations, requires
from app.clhear.l2 import review as l2_review
from app.clhear.l3.kinds import required_fields
from app.clhear.models import events, human_edits
from app.clhear.platform import console, record
from app.clhear.platform import proposals as l0_proposals
from tests.test_l6_blueprints import SOURCE, _corpus

MAINTAINER = {"X-Reg42-User": "avner@reg42.ai"}


def _first_obligation(engine) -> dict:
    with engine.connect() as conn:
        return dict(conn.execute(sa.select(obligations).where(obligations.c.source_key == SOURCE)
                                 .order_by(obligations.c.clause_ref)).mappings().first())


def _edits(engine, **where) -> list[dict]:
    q = sa.select(human_edits).order_by(human_edits.c.id)
    for k, v in where.items():
        q = q.where(getattr(human_edits.c, k) == v)
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(q).mappings()]


# --------------------------------------------------------------------------- low-confidence queue


def test_queue_lists_low_confidence_items_per_layer_threshold(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    body = client.get("/console/queue").json()
    assert body["thresholds"] == {k: record.LOW_CONFIDENCE_THRESHOLDS[k] for k in console.QUEUE_LAYERS}
    assert set(body["counts"]) == set(console.QUEUE_LAYERS)
    l2 = [i for i in body["items"] if i["layer"] == "L2"]
    assert l2 and body["counts"]["L2"] == len(l2)
    for item in l2:
        assert item["table"] == "obligations" and item["threshold"] == record.LOW_CONFIDENCE_THRESHOLDS["L2"]
        assert item["confidence"] is None or item["confidence"] < item["threshold"]
        assert item["public_id"].startswith("OBL-") and item["href"] == f"/l2#{item['public_id']}"
        assert item["basis_hash"]
    # lowest confidence first; filtered by layer; bad layer refused
    confs = [float(i["confidence"] or 0) for i in body["items"]]
    assert confs == sorted(confs)
    only_l2 = client.get("/console/queue?layer=L2").json()
    assert set(i["layer"] for i in only_l2["items"]) == {"L2"} and list(only_l2["thresholds"]) == ["L2"]
    assert client.get("/console/queue?layer=L9").status_code == 422
    # nothing in the L2 queue is already validated
    with engine.connect() as conn:
        statuses = {r.id: r.status for r in conn.execute(sa.select(obligations.c.id, obligations.c.status))}
    assert all(statuses[i["subject_ref"]] == "derived" for i in l2)


def test_validate_needs_a_maintainer(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    item = client.get("/console/queue?layer=L2").json()["items"][0]
    url = f"/console/queue/L2/{item['public_id']}/validate"
    assert client.post(url, json={"note": "x"}).status_code == 401
    assert client.post(url, json={"note": "x"}, headers={"X-Reg42-User": "intruder@example.com"}).status_code == 403
    assert client.post("/console/queue/L2/OBL-999999/validate", headers=MAINTAINER).status_code == 404
    assert client.post("/console/queue/L8/anything/validate", headers=MAINTAINER).status_code == 422


def test_validate_promotes_records_edit_and_leaves_queue(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    before = client.get("/console/queue?layer=L2").json()
    item = before["items"][0]
    # both the public id and the (percent-encoded) derivation key are accepted
    resp = client.post(f"/console/queue/L2/{quote(item['subject_ref'], safe=':/')}/validate",
                       json={"note": "checked against the clause"}, headers=MAINTAINER)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "validated" and out["subject_ref"] == item["subject_ref"] and out["edit_id"].startswith("EDT-")
    with engine.connect() as conn:
        ob = conn.execute(sa.select(obligations).where(obligations.c.id == item["subject_ref"])).mappings().one()
        n_events = conn.execute(sa.select(sa.func.count()).select_from(events)
                                .where(events.c.kind.in_(("ObligationValidated", "clhear.l0.human_edit")),
                                       events.c.subject_ref == item["subject_ref"])).scalar_one()
    assert ob["status"] == "validated" and ob["validated_by"] == "avner@reg42.ai"
    assert ob["review"][-1]["event"] == "validated" and ob["review"][-1]["by"] == "avner@reg42.ai"
    assert ob["review"][-1]["why_trail_id"] and ob["version"] >= 2
    assert n_events == 2
    edit, = _edits(engine, subject_ref=item["subject_ref"])
    assert edit["kind"] == "validation" and edit["basis_hash"] == ob["text_hash"] and edit["after"] == "validated"
    assert edit["status"] == "accepted" and edit["rationale"] == "checked against the clause"
    after = client.get("/console/queue?layer=L2").json()
    assert after["counts"]["L2"] == before["counts"]["L2"] - 1
    assert item["subject_ref"] not in {i["subject_ref"] for i in after["items"]}
    # the ledger is readable and the summary counts it
    assert [e["id"] for e in client.get("/console/edits?status=accepted&layer=L2").json()] == [edit["id"]]
    summary = client.get("/console/summary").json()
    assert summary["low_confidence"]["L2"] == after["counts"]["L2"] and summary["human_edits"] == {"accepted": 1}


def test_validate_unbacked_characteristic_marks_it_backed(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    with engine.begin() as conn:
        bid = conn.execute(sa.select(blocks.c.id).limit(1)).scalar_one()
        why = record.WhyTrail(layer="L3", reasoning_summary="test", evidence_refs=[], inputs=(), agent_id="test",
                              skill_version="t", confidence=0.4, subject_ref=bid)
        record.write(conn, characteristics, {"block_id": bid, "key": "owner", "value": "CFO", "status": "unbacked",
                                             "method": "model", "confidence": 0.4}, why=why)
        cid = conn.execute(sa.select(characteristics.c.id).where(characteristics.c.block_id == bid,
                                                                  characteristics.c.key == "owner")).scalar_one()
    items = client.get("/console/queue?layer=L3").json()["items"]
    mine = [i for i in items if i["subject_ref"] == f"CHR-{cid}"]
    assert mine and mine[0]["table"] == "characteristics" and "unbacked" in mine[0]["context"]
    resp = client.post(f"/console/queue/L3/CHR-{cid}/validate", json={"note": "the policy names the CFO"}, headers=MAINTAINER)
    assert resp.status_code == 200, resp.text
    with engine.connect() as conn:
        row = conn.execute(sa.select(characteristics).where(characteristics.c.id == cid)).mappings().one()
    assert row["status"] == "backed" and row["method"] == "human-validated" and row["backing_span"] == "the policy names the CFO"
    assert f"CHR-{cid}" not in {i["subject_ref"] for i in client.get("/console/queue?layer=L3").json()["items"]}


# --------------------------------------------------------------------------- modification requests


def test_l2_modification_request_approved_applies_field_and_records_edit(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    ob = _first_obligation(engine)
    req = client.post(f"/l2/obligations/{ob['stable_id']}/modification-requests",
                      json={"field": "subject", "proposed_value": "Every relevant person", "rationale": "clearer subject wording",
                            "requester": "tester"})
    assert req.status_code == 201
    pid = req.json()["proposal_id"]
    listed = client.get("/console/requests").json()
    assert [p["id"] for p in listed] == [pid] and listed[0]["kind"] == "l2_modification"
    assert client.get("/console/summary").json()["modification_requests"] == 1
    assert client.post(f"/console/proposals/{pid}/approve").status_code == 401
    resp = client.post(f"/console/proposals/{pid}/approve", headers=MAINTAINER)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved" and body["approver"] == "avner@reg42.ai" and "edit_error" not in body
    assert body["edit"]["field"] == "subject" and body["edit"]["after"] == "Every relevant person"
    with engine.connect() as conn:
        now = conn.execute(sa.select(obligations).where(obligations.c.id == ob["id"])).mappings().one()
    assert now["subject"] == "Every relevant person" and now["version"] == (ob["version"] or 1) + 1
    assert now["review"][-1] == {**now["review"][-1], "event": "human_edit", "field": "subject", "proposal_id": pid}
    assert now["why_trail_id"] != ob["why_trail_id"]
    edit, = _edits(engine, proposal_id=pid)
    assert edit["kind"] == "field_edit" and edit["before"] == ob["subject"] and edit["after"] == "Every relevant person"
    assert edit["basis_hash"] == ob["text_hash"] and edit["accepted_by"] == "avner@reg42.ai"
    # decided once
    assert client.post(f"/console/proposals/{pid}/reject", headers=MAINTAINER).status_code == 409
    assert client.get("/console/requests").json() == []
    assert client.post("/console/proposals/PRP-nope/approve", headers=MAINTAINER).status_code == 404


def test_l2_modification_rejected_leaves_row_alone(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    ob = _first_obligation(engine)
    pid = client.post(f"/l2/obligations/{ob['stable_id']}/modification-requests",
                      json={"field": "subject", "proposed_value": "Nobody", "rationale": "not a good idea, really",
                            "requester": "tester"}).json()["proposal_id"]
    resp = client.post(f"/console/proposals/{pid}/reject", headers=MAINTAINER)
    assert resp.status_code == 200 and resp.json()["status"] == "rejected" and resp.json()["edit"] is None
    with engine.connect() as conn:
        now = conn.execute(sa.select(obligations).where(obligations.c.id == ob["id"])).mappings().one()
    assert now["subject"] == ob["subject"] and now["version"] == ob["version"]
    assert _edits(engine) == []


def test_maintainer_override_edits_the_proposed_value_before_applying(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    ob = _first_obligation(engine)
    pid = client.post(f"/l2/obligations/{ob['stable_id']}/modification-requests",
                      json={"field": "subject", "proposed_value": "a frim", "rationale": "typo in the derived subject",
                            "requester": "tester"}).json()["proposal_id"]
    resp = client.post(f"/console/proposals/{pid}/approve", json={"override": {"proposed_value": "A firm"}}, headers=MAINTAINER)
    assert resp.status_code == 200 and resp.json()["edit"]["after"] == "A firm"
    with engine.connect() as conn:
        assert conn.execute(sa.select(obligations.c.subject).where(obligations.c.id == ob["id"])).scalar_one() == "A firm"


def test_legacy_proposal_route_shares_the_console_path(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    ob = _first_obligation(engine)
    pid = client.post(f"/l2/obligations/{ob['stable_id']}/modification-requests",
                      json={"field": "action", "proposed_value": "reconcile daily", "rationale": "plainer action verb",
                            "requester": "tester"}).json()["proposal_id"]
    resp = client.post(f"/api/clhear/proposals/{pid}/approve", headers=MAINTAINER)
    assert resp.status_code == 200 and resp.json()["edit"]["after"] == "reconcile daily"
    with engine.connect() as conn:
        assert conn.execute(sa.select(obligations.c.action).where(obligations.c.id == ob["id"])).scalar_one() == "reconcile daily"
    assert len(_edits(engine, proposal_id=pid)) == 1


def test_unknown_field_is_refused_but_the_decision_is_kept(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    ob = _first_obligation(engine)
    with engine.begin() as conn:
        pid = l0_proposals.create_proposal(conn, layer="L2", kind="l2_modification", subject_ref=ob["stable_id"],
                                           draft={"derivation_key": ob["id"], "field": "text_hash", "proposed_value": "x"},
                                           rationale="tampering with the basis", confidence=None)
    body = client.post(f"/console/proposals/{pid}/approve", headers=MAINTAINER).json()
    assert body["status"] == "approved" and "not editable" in body["edit_error"]
    with engine.connect() as conn:
        assert conn.execute(sa.select(obligations.c.text_hash).where(obligations.c.id == ob["id"])).scalar_one() == ob["text_hash"]
    assert _edits(engine) == []


def test_l2_review_escalation_approve_means_expert_incorrect(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    ob = _first_obligation(engine)
    with engine.begin() as conn:
        conn.execute(obligation_reviews.insert().values(obligation_id=ob["id"], reviewer_kind="model", reviewer="judge",
                                                        verdict="incorrect", confidence=0.6, text_hash=ob["text_hash"],
                                                        notes="", derived_by="model"))
        pid = l0_proposals.create_proposal(conn, layer="L2", kind="l2_review", subject_ref=ob["stable_id"],
                                           draft={"obligation_id": ob["stable_id"], "derivation_key": ob["id"],
                                                  "verdict": "incorrect", "confidence": 0.6, "reviewer": "judge"},
                                           rationale="second model doubts the duty", confidence=0.6)
    assert l2_review.precision(engine)["by_kind"]["model"] == 1
    body = client.post(f"/console/proposals/{pid}/approve", headers=MAINTAINER).json()
    assert body["edit"]["verdict"] == "incorrect" and body["edit"]["status"] == "rejected"
    with engine.connect() as conn:
        now = conn.execute(sa.select(obligations).where(obligations.c.id == ob["id"])).mappings().one()
        expert = conn.execute(sa.select(obligation_reviews).where(obligation_reviews.c.obligation_id == ob["id"],
                                                                  obligation_reviews.c.reviewer_kind == "expert")).mappings().all()
    assert now["status"] == "rejected" and now["review_confidence"] == 1.0 and now["review"][-1]["event"] == "expert_verdict"
    assert len(expert) == 1 and expert[0]["verdict"] == "incorrect" and expert[0]["reviewer"] == "avner@reg42.ai"
    edit, = _edits(engine, proposal_id=pid)
    assert edit["kind"] == "verdict" and edit["before"] == "derived" and edit["after"] == "rejected"


def test_l2_review_escalation_reject_means_expert_correct_and_overrides_model(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    ob = _first_obligation(engine)
    with engine.begin() as conn:
        conn.execute(obligation_reviews.insert().values(obligation_id=ob["id"], reviewer_kind="model", reviewer="judge",
                                                        verdict="incorrect", confidence=0.6, text_hash=ob["text_hash"],
                                                        notes="", derived_by="model"))
        pid = l0_proposals.create_proposal(conn, layer="L2", kind="l2_review", subject_ref=ob["stable_id"],
                                           draft={"obligation_id": ob["stable_id"], "derivation_key": ob["id"],
                                                  "verdict": "incorrect", "confidence": 0.6, "reviewer": "judge"},
                                           rationale="second model doubts the duty", confidence=0.6)
    assert l2_review.precision(engine)["precision"] == 0.0
    body = client.post(f"/console/proposals/{pid}/reject", headers=MAINTAINER).json()
    assert body["status"] == "rejected" and body["edit"]["verdict"] == "correct" and body["edit"]["status"] == "validated"
    with engine.connect() as conn:
        now = conn.execute(sa.select(obligations).where(obligations.c.id == ob["id"])).mappings().one()
    assert now["status"] == "validated" and now["validated_by"] == "avner@reg42.ai"
    prec = l2_review.precision(engine)
    assert prec["precision"] == 1.0 and prec["by_kind"] == {"model": 0, "expert": 1}


def test_l3_modification_requests_apply_through_the_record_path(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    ob = _first_obligation(engine)
    with engine.connect() as conn:
        b = dict(conn.execute(sa.select(blocks).join(requires, requires.c.block_id == blocks.c.id)
                              .where(requires.c.valid_to.is_(None)).limit(1)).mappings().one())
        req_key = next(iter(required_fields(b["kind"])), None)
    # plain field
    pid = client.post(f"/l3/blocks/{b['id']}/modification-requests",
                      json={"field": "name", "proposed_value": "Renamed block", "rationale": "the name was misleading",
                            "requester": "tester"}).json()["proposal_id"]
    body = client.post(f"/console/proposals/{pid}/approve", headers=MAINTAINER).json()
    assert body["edit"]["after"] == "Renamed block" and body["edit"]["before"] == b["name"]
    with engine.connect() as conn:
        now = dict(conn.execute(sa.select(blocks).where(blocks.c.id == b["id"])).mappings().one())
    assert now["name"] == "Renamed block" and now["version"] == (b["version"] or 1) + 1 and now["review"][-1]["proposal_id"] == pid
    # characteristic: old live row invalidated (never deleted), new row backed at confidence 1.0
    if req_key:
        pid = client.post(f"/l3/blocks/{b['id']}/modification-requests",
                          json={"field": f"characteristic:{req_key}", "proposed_value": "set by hand",
                                "rationale": "the derived value was wrong", "requester": "tester"}).json()["proposal_id"]
        body = client.post(f"/console/proposals/{pid}/approve", headers=MAINTAINER).json()
        assert "edit_error" not in body and body["edit"]["after"] == "set by hand"
        with engine.connect() as conn:
            rows = conn.execute(sa.select(characteristics).where(characteristics.c.block_id == b["id"],
                                                                  characteristics.c.key == req_key)
                                .order_by(characteristics.c.id)).mappings().all()
        live = [r for r in rows if r["valid_to"] is None]
        assert len(live) == 1 and live[0]["value"] == "set by hand" and live[0]["status"] == "backed"
        assert live[0]["method"] == "human-edit" and live[0]["confidence"] == 1.0
        assert all(r["valid_to"] is not None for r in rows if r["id"] != live[0]["id"])
    # requires edge: remove then add back, both versioned
    with engine.connect() as conn:
        linked = conn.execute(sa.select(requires.c.obligation_id).where(requires.c.block_id == b["id"],
                                                                        requires.c.valid_to.is_(None))).scalars().all()
    target = linked[0] if linked else ob["id"]
    pid = client.post(f"/l3/blocks/{b['id']}/modification-requests",
                      json={"field": f"requires:{target}", "proposed_value": "remove", "rationale": "the block does not serve this duty",
                            "requester": "tester"}).json()["proposal_id"]
    body = client.post(f"/console/proposals/{pid}/approve", headers=MAINTAINER).json()
    assert body["edit"]["after"] == "unlinked"
    with engine.connect() as conn:
        edges = conn.execute(sa.select(requires).where(requires.c.block_id == b["id"], requires.c.obligation_id == target)
                             .order_by(requires.c.valid_from)).mappings().all()
    assert edges and all(e["valid_to"] is not None for e in edges)
    pid = client.post(f"/l3/blocks/{b['id']}/modification-requests",
                      json={"field": f"requires:{target}", "proposed_value": "add", "rationale": "on reflection it does serve it",
                            "requester": "tester"}).json()["proposal_id"]
    body = client.post(f"/console/proposals/{pid}/approve", headers=MAINTAINER).json()
    assert body["edit"]["after"] == "linked"
    with engine.connect() as conn:
        live_edges = conn.execute(sa.select(requires).where(requires.c.block_id == b["id"], requires.c.obligation_id == target,
                                                            requires.c.valid_to.is_(None))).mappings().all()
    assert len(live_edges) == 1 and live_edges[0]["method"] == "human-edit"
    assert len(_edits(engine, layer="L3")) >= 3 and all(e["kind"] == "field_edit" for e in _edits(engine, layer="L3"))


# --------------------------------------------------------------------------- contribution reviews


def test_contribution_reviews_are_listed_separately_and_recorded(engine, client):
    with engine.begin() as conn:
        cid = l0_proposals.create_proposal(conn, layer="L2", kind="community_correction", subject_ref="OBL-000001",
                                           draft={"field": "statement", "proposed_value": "…", "contributor": "alice"},
                                           rationale="community correction", confidence=None)
        mid = l0_proposals.create_proposal(conn, layer="L2", kind="l2_modification", subject_ref="OBL-000001",
                                           draft={"field": "subject", "proposed_value": "x"}, rationale="mod", confidence=None)
    assert [p["id"] for p in client.get("/console/contributions").json()] == [cid]
    assert [p["id"] for p in client.get("/console/requests").json()] == [mid]
    summary = client.get("/console/summary").json()
    assert summary["contribution_reviews"] == 1 and summary["modification_requests"] == 1 and summary["other_proposals"] == 0
    body = client.post(f"/console/proposals/{cid}/approve", headers=MAINTAINER).json()
    assert body["status"] == "approved"
    edit, = _edits(engine, proposal_id=cid)
    assert edit["kind"] == "contribution" and edit["field"] == "community_correction" and edit["after"]["contributor"] == "alice"
    assert client.get("/console/contributions").json() == []
    assert client.get("/console/contributions?status=approved").json()[0]["id"] == cid


# --------------------------------------------------------------------------- page


def test_console_page_served_and_linked(client):
    resp = client.get("/console")
    assert resp.status_code == 200 and "Approval console" in resp.text
    assert 'href="/console"' in client.get("/review").text
    for path in ("/console/summary", "/console/queue", "/console/requests", "/console/contributions", "/console/edits"):
        assert client.get(path).status_code == 200, path
