"""HLD v2 §8 item 11 done-test — human-accepted edits are reproduced or escalated.

A maintainer's decision is stored with the basis it was taken on. The next
derivation cycle must either reproduce it (same basis: the value is re-asserted
if a re-run lost it) or escalate it (basis changed: a ``human_edit_conflict``
proposal puts the old edit next to the new derivation). Nothing is silently
overwritten in either direction.
"""
from __future__ import annotations

import sqlalchemy as sa

from app.clhear.derived_models import obligations
from app.clhear.l1 import pipeline
from app.clhear.l2.extract import run_extraction
from app.clhear.models import human_edits, proposals
from app.clhear.platform import console
from app.clhear.platform.gateway import FakeProvider, Gateway
from tests.test_l6_blueprints import SOURCE, UK, UKAdapter, _corpus

MAINTAINER = {"X-Reg42-User": "avner@reg42.ai"}
AMENDED = {**UK, "1": "A firm that holds client money must reconcile its client money records at least twice every business day."}


def _ob(engine, clause_ref: str) -> dict:
    with engine.connect() as conn:
        return dict(conn.execute(sa.select(obligations).where(obligations.c.source_key == SOURCE,
                                                              obligations.c.clause_ref == clause_ref)).mappings().one())


def _edit(engine, edit_id: str) -> dict:
    with engine.connect() as conn:
        return dict(conn.execute(sa.select(human_edits).where(human_edits.c.id == edit_id)).mappings().one())


def _accept_subject_edit(engine, client, ob: dict, value: str = "Every firm holding client money") -> str:
    pid = client.post(f"/l2/obligations/{ob['stable_id']}/modification-requests",
                      json={"field": "subject", "proposed_value": value, "rationale": "sharper subject wording",
                            "requester": "tester"}).json()["proposal_id"]
    body = client.post(f"/console/proposals/{pid}/approve", headers=MAINTAINER).json()
    assert "edit_error" not in body
    return body["edit"]["edit_id"]


def _reingest(engine, tmp_path, provisions, version):
    store = pipeline.LocalStore(tmp_path / "lake")
    pipeline.ingest(engine, UKAdapter(provisions, version, source_key=SOURCE), store, gateway=Gateway(engine, FakeProvider()))
    return run_extraction(engine, source_key=SOURCE)


def test_edit_survives_an_unchanged_re_derivation(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    ob = _ob(engine, "1")
    eid = _accept_subject_edit(engine, client, ob)
    stats = run_extraction(engine, source_key=SOURCE)
    assert stats["re_derived"] == 0 and stats["unchanged"] >= 5
    out = console.reproduce_human_edits(engine, layer="L2")
    assert out == {**out, "checked": 1, "intact": 1, "reproduced": 0, "escalated": 0} and out["details"] == []
    edit = _edit(engine, eid)
    assert edit["status"] == "accepted" and edit["checks"] == 1 and edit["last_outcome"] == "intact"
    assert _ob(engine, "1")["subject"] == "Every firm holding client money"


def test_lost_value_on_same_basis_is_reproduced(engine, client, tmp_path):
    """A re-run that rewrote the field on the same text (same basis hash) is
    corrected: the human value is re-asserted through the same write path and a
    superseding edit carries the lineage."""
    _corpus(engine, tmp_path)
    ob = _ob(engine, "1")
    eid = _accept_subject_edit(engine, client, ob)
    with engine.begin() as conn:  # simulate an agent overwriting the field without touching the basis
        conn.execute(obligations.update().where(obligations.c.id == ob["id"]).values(subject="A firm"))
    out = console.reproduce_human_edits(engine, layer="L2")
    assert out["checked"] == 1 and out["reproduced"] == 1 and out["escalated"] == 0
    assert out["details"][0]["edit_id"] == eid and out["details"][0]["outcome"].startswith("reproduced")
    now = _ob(engine, "1")
    assert now["subject"] == "Every firm holding client money" and now["review"][-1]["event"] == "human_edit"
    old = _edit(engine, eid)
    assert old["status"] == "superseded" and "reproduced" in old["last_outcome"]
    live = [e for e in console.list_edits(engine, layer="L2") if e["status"] in ("accepted", "reproduced")]
    assert len(live) == 1 and live[0]["id"] != eid and live[0]["after"] == "Every firm holding client money"
    assert live[0]["accepted_by"] == "avner@reg42.ai" and live[0]["basis_hash"] == ob["text_hash"]
    # the second pass finds everything intact
    again = console.reproduce_human_edits(engine, layer="L2")
    assert again["checked"] == 1 and again["intact"] == 1


def test_amended_basis_escalates_instead_of_overwriting(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    ob = _ob(engine, "1")
    eid = _accept_subject_edit(engine, client, ob)
    stats = _reingest(engine, tmp_path, AMENDED, "2026-06-01")
    assert stats["re_derived"] == 1
    amended = _ob(engine, "1")
    assert amended["text_hash"] != ob["text_hash"] and amended["status"] == "derived"
    out = console.reproduce_human_edits(engine, layer="L2")
    assert out["checked"] == 1 and out["escalated"] == 1 and out["reproduced"] == 0
    edit = _edit(engine, eid)
    assert edit["status"] == "escalated" and edit["escalation_proposal_id"] and "basis changed" in edit["last_outcome"]
    with engine.connect() as conn:
        prop = dict(conn.execute(sa.select(proposals).where(proposals.c.id == edit["escalation_proposal_id"])).mappings().one())
    assert prop["kind"] == "human_edit_conflict" and prop["status"] == "proposed" and prop["subject_ref"] == ob["id"]
    assert prop["draft"]["edit"]["id"] == eid and prop["draft"]["edit"]["after"] == "Every firm holding client money"
    assert prop["draft"]["current_basis_hash"] == amended["text_hash"] and prop["draft"]["current_value"] == amended["subject"]
    # the derivation was not touched, and the conflict is in the console's modification queue
    assert amended["subject"] != "Every firm holding client money"
    listed = client.get("/console/requests").json()
    assert [p["id"] for p in listed] == [prop["id"]]
    # escalated once: another pass does not pile up proposals
    again = console.reproduce_human_edits(engine, layer="L2")
    assert again["checked"] == 0
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(proposals)
                            .where(proposals.c.kind == "human_edit_conflict")).scalar_one() == 1


def test_re_accepting_a_conflict_applies_the_edit_on_the_new_basis(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    ob = _ob(engine, "1")
    eid = _accept_subject_edit(engine, client, ob)
    _reingest(engine, tmp_path, AMENDED, "2026-06-01")
    console.reproduce_human_edits(engine, layer="L2")
    pid = _edit(engine, eid)["escalation_proposal_id"]
    body = client.post(f"/console/proposals/{pid}/approve", headers=MAINTAINER).json()
    assert body["status"] == "approved" and "edit_error" not in body and body["edit"]["after"] == "Every firm holding client money"
    now = _ob(engine, "1")
    assert now["subject"] == "Every firm holding client money"
    new_edit = _edit(engine, body["edit"]["edit_id"])
    assert new_edit["basis_hash"] == now["text_hash"] and new_edit["status"] == "accepted" and new_edit["proposal_id"] == pid
    assert _edit(engine, eid)["status"] == "superseded"
    out = console.reproduce_human_edits(engine, layer="L2")
    assert out["checked"] == 1 and out["intact"] == 1


def test_letting_the_derivation_stand_supersedes_the_edit(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    ob = _ob(engine, "1")
    eid = _accept_subject_edit(engine, client, ob)
    _reingest(engine, tmp_path, AMENDED, "2026-06-01")
    console.reproduce_human_edits(engine, layer="L2")
    pid = _edit(engine, eid)["escalation_proposal_id"]
    derived_subject = _ob(engine, "1")["subject"]
    body = client.post(f"/console/proposals/{pid}/reject", headers=MAINTAINER).json()
    assert body["status"] == "rejected" and body["edit"] == {"edit_id": eid, "outcome": "derivation stands"}
    assert _ob(engine, "1")["subject"] == derived_subject
    edit = _edit(engine, eid)
    assert edit["status"] == "superseded" and "derivation stands" in edit["last_outcome"]
    assert console.reproduce_human_edits(engine, layer="L2")["checked"] == 0


def test_validation_is_re_asserted_when_a_same_text_rerun_resets_status(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    item = client.get("/console/queue?layer=L2").json()["items"][0]
    resp = client.post(f"/console/queue/L2/{item['public_id']}/validate", json={"note": "ok"}, headers=MAINTAINER)
    assert resp.status_code == 200
    eid = resp.json()["edit_id"]
    with engine.begin() as conn:  # a faulty re-run resets the status without changing the text
        conn.execute(obligations.update().where(obligations.c.id == item["subject_ref"]).values(status="derived"))
    # the queue keeps hiding it: a live validation at the same basis still covers the row
    assert item["subject_ref"] not in {i["subject_ref"] for i in client.get("/console/queue?layer=L2").json()["items"]}
    out = console.reproduce_human_edits(engine, layer="L2")
    assert out["reproduced"] == 1 and out["details"][0]["outcome"] == "reproduced: status re-asserted"
    with engine.connect() as conn:
        row = conn.execute(sa.select(obligations).where(obligations.c.id == item["subject_ref"])).mappings().one()
    assert row["status"] == "validated" and row["validated_by"] == "avner@reg42.ai"
    assert _edit(engine, eid)["status"] == "reproduced"


def test_validation_on_amended_text_is_escalated(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    ob = _ob(engine, "1")
    resp = client.post(f"/console/queue/L2/{ob['stable_id']}/validate", json={"note": "ok"}, headers=MAINTAINER)
    assert resp.status_code == 200
    eid = resp.json()["edit_id"]
    _reingest(engine, tmp_path, AMENDED, "2026-06-01")
    assert _ob(engine, "1")["status"] == "derived"
    out = console.reproduce_human_edits(engine, layer="L2")
    assert out["escalated"] == 1 and _edit(engine, eid)["status"] == "escalated"
    # the amended obligation is back in the low-confidence queue (its validation no longer covers this basis)
    queue = client.get("/console/queue?layer=L2").json()["items"]
    assert ob["id"] in {i["subject_ref"] for i in queue}


def test_reproduce_endpoint_and_nightly_report(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    ob = _ob(engine, "1")
    _accept_subject_edit(engine, client, ob)
    assert client.post("/console/edits/reproduce").status_code == 401
    body = client.post("/console/edits/reproduce?layer=L2", headers=MAINTAINER).json()
    assert body["by"] == "avner@reg42.ai" and body["checked"] == 1 and body["intact"] == 1
