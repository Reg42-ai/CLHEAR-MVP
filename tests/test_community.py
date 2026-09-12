"""Phase C: accounts, cases, validation votes, moderation, named-human gate."""
import sqlalchemy as sa

from app.clhear.community_models import submissions
from app.clhear.derived_models import obligations
from app.clhear.models import proposals


def _login(client, email="ada@example.com"):
    resp = client.post("/auth/email", json={"email": email})
    assert resp.status_code == 200
    link = resp.json()["debug_link"]
    verify = client.get(link.split("clhear.reg42.ai")[1], follow_redirects=False)
    assert verify.status_code == 307
    return client  # cookie now set on the client


def _seed_obligation(engine, oid="OBL:uksi/2017/692#regulation-27"):
    with engine.begin() as conn:
        conn.execute(
            obligations.insert().values(
                id=oid, source_key="uksi/2017/692", clause_ref="regulation-27",
                title="Apply CDD", statement="A relevant person must…", modality="must",
                jurisdiction="UK", confidence=0.85, status="derived", text_hash="h27",
            )
        )
    return oid


def test_magic_link_login_and_me(client, engine, monkeypatch):
    monkeypatch.setenv("CLHEAR_AUTH_DEBUG", "true")
    from app.clhear.settings import get_settings

    get_settings.cache_clear()
    _login(client)
    me = client.get("/auth/me").json()
    assert me["user"]["email"] == "ada@example.com"
    assert me["providers"]["email"] is True
    client.post("/auth/logout")
    assert client.get("/auth/me").json()["user"] is None


def test_submission_requires_auth(client, engine):
    resp = client.post("/api/clhear/community/submissions", json={"kind": "correction", "title": "anon attempt"})
    assert resp.status_code == 401


def test_case_lifecycle_with_named_human_gate(client, engine, monkeypatch):
    monkeypatch.setenv("CLHEAR_AUTH_DEBUG", "true")
    from app.clhear.settings import get_settings

    get_settings.cache_clear()
    _login(client)
    resp = client.post(
        "/api/clhear/community/submissions",
        json={"kind": "new_source", "title": "Cover the EBA ML/TF risk-factor guidelines",
              "body": "Binding-adjacent guidance used by every EU AML supervisor.",
              "evidence_url": "https://www.eba.europa.eu/", "target_layer": "L1"},
    )
    assert resp.status_code == 200, resp.text
    submission_id = resp.json()["id"]

    # Mirrored into the l0 proposals queue.
    with engine.connect() as conn:
        prop = conn.execute(sa.select(proposals).where(proposals.c.kind == "community_new_source")).one()
    assert prop.status == "proposed"

    # Maintainer approves in the review console -> case syncs to accepted.
    approve = client.post(
        f"/api/clhear/proposals/{prop.id}/approve", headers={"X-Reg42-User": "avner@reg42.ai"}
    )
    assert approve.status_code == 200
    with engine.connect() as conn:
        row = conn.execute(sa.select(submissions).where(submissions.c.id == submission_id)).one()
    assert row.status == "accepted"
    assert row.decided_by == "avner@reg42.ai"

    wall = client.get("/api/clhear/community/wall").json()
    assert wall["totals"]["cases_accepted"] == 1
    assert wall["top_contributors"][0]["accepted_cases"] == 1


def test_votes_tally_and_promotion_gate(client, engine, monkeypatch):
    monkeypatch.setenv("CLHEAR_AUTH_DEBUG", "true")
    from app.clhear.settings import get_settings

    get_settings.cache_clear()
    oid = _seed_obligation(engine)
    encoded = oid.replace("#", "%23")

    for i, email in enumerate(["a@x.com", "b@x.com", "c@x.com"]):
        _login(client, email)
        resp = client.post(f"/api/clhear/community/obligations/{encoded}/vote", json={"vote": "confirm"})
        assert resp.status_code == 200, resp.text
    tally = client.get(f"/api/clhear/community/obligations/{encoded}/votes").json()
    assert tally["confirm"] == 3 and tally["dispute"] == 0
    assert tally["promotion_suggested"] is True

    # Votes only SUGGEST: status is still derived until a maintainer acts.
    with engine.connect() as conn:
        assert conn.execute(sa.select(obligations.c.status).where(obligations.c.id == oid)).scalar_one() == "derived"
    promote = client.post(f"/api/clhear/obligations/{encoded}/validate", headers={"X-Reg42-User": "avner@reg42.ai"})
    assert promote.status_code == 200
    with engine.connect() as conn:
        row = conn.execute(sa.select(obligations).where(obligations.c.id == oid)).one()
    assert row.status == "validated" and row.validated_by == "avner@reg42.ai"

    # Re-voting switches, never duplicates.
    resp = client.post(f"/api/clhear/community/obligations/{encoded}/vote", json={"vote": "dispute"})
    assert resp.json()["dispute"] == 1 and resp.json()["confirm"] == 2


def test_vote_unknown_obligation_404(client, engine, monkeypatch):
    monkeypatch.setenv("CLHEAR_AUTH_DEBUG", "true")
    from app.clhear.settings import get_settings

    get_settings.cache_clear()
    _login(client)
    resp = client.post("/api/clhear/community/obligations/OBL:none%23x/vote", json={"vote": "confirm"})
    assert resp.status_code == 404


def test_honeypot_swallows_bots(client, engine, monkeypatch):
    monkeypatch.setenv("CLHEAR_AUTH_DEBUG", "true")
    from app.clhear.settings import get_settings

    get_settings.cache_clear()
    _login(client)
    resp = client.post(
        "/api/clhear/community/submissions",
        json={"kind": "correction", "title": "totally human", "website": "http://spam"},
    )
    assert resp.status_code == 200
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(submissions)).scalar_one() == 0
