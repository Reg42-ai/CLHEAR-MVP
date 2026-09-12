import sqlalchemy as sa

from app.clhear.models import events
from app.clhear.platform import proposals as l0_proposals


def _make_proposal(engine) -> str:
    with engine.begin() as conn:
        return l0_proposals.create_proposal(
            conn, layer="l1", kind="family_member", subject_ref="uk-mlr/si-2019-253",
            draft={"relation": "amends"}, rationale="citator feed", confidence=0.97,
        )


def test_approve_requires_identity(engine, client):
    pid = _make_proposal(engine)
    assert client.post(f"/api/clhear/proposals/{pid}/approve").status_code == 401


def test_approve_requires_maintainer_role(engine, client):
    pid = _make_proposal(engine)
    resp = client.post(f"/api/clhear/proposals/{pid}/approve", headers={"X-Reg42-User": "intruder@example.com"})
    assert resp.status_code == 403


def test_double_decision_conflicts(engine, client):
    pid = _make_proposal(engine)
    headers = {"X-Reg42-User": "maintainer@reg42.ai"}
    assert client.post(f"/api/clhear/proposals/{pid}/approve", headers=headers).status_code == 200
    assert client.post(f"/api/clhear/proposals/{pid}/reject", headers=headers).status_code == 409


def test_reject_records_identity_and_emits_event(engine, client):
    pid = _make_proposal(engine)
    resp = client.post(f"/api/clhear/proposals/{pid}/reject", headers={"X-Reg42-User": "avner@reg42.ai"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected" and body["approver"] == "avner@reg42.ai"
    with engine.connect() as conn:
        assert conn.execute(
            sa.select(sa.func.count()).select_from(events).where(events.c.kind == "ProposalRejected")
        ).scalar_one() == 1


def test_review_console_served(client):
    resp = client.get("/review")
    assert resp.status_code == 200
    assert "Review console" in resp.text
