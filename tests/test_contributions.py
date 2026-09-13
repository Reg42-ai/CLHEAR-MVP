"""HLD v2 §6 / I12 — the community contribution flow.

Contributions never write a layer table directly: CLA → automated checks →
fleet re-derivation → two reviewers → applied through the record path (the same
functions the approval console uses) → shipped in a release with attribution and
impact. Acceptance test from the plan: an external correction flows to a release
with attribution and an impact count.
"""
from __future__ import annotations

import json

import pytest
import sqlalchemy as sa

from app.clhear.community_models import contributions as contributions_t
from app.clhear.community_models import roles as roles_t
from app.clhear.derived_models import asserts, equivalences, obligations
from app.clhear.l1.models import clauses, family_members, source_families, source_versions, sources
from app.clhear.models import human_edits
from app.clhear.platform import contributions as c
from app.clhear.platform import proposals as l0_proposals
from app.clhear.platform import record

UK_27 = ("A relevant person must apply customer due diligence measures if the person establishes a "
         "business relationship or carries out an occasional transaction exceeding the threshold.")
EU_20 = ("Obliged entities shall apply customer due diligence measures and verify the customer identity "
         "when establishing a business relationship or carrying out an occasional transaction.")
RESTRICTED = ("the organization shall determine the boundaries and applicability of the information security "
              "management system to establish its scope")
OB_UK = "OBL:uksi/2017/692#regulation-27"
OB_EU = "OBL:celex/32024R1624#art_20"
ADA, BOB, AVNER, MAINT = "ada@example.com", "bob@example.com", "avner@reg42.ai", "maintainer@reg42.ai"


def _seed(engine):
    """Two public clauses with obligations + asserts, one restricted clause (no obligation)."""
    with engine.begin() as conn:
        fam = conn.execute(source_families.insert().values(key="t", name="T", scope_charter={})).inserted_primary_key[0]

        def add(key, name, jur, license="open", rights="open_licence"):
            sid = conn.execute(sources.insert().values(family_id=fam, key=key, name=name, kind="regulation", license=license,
                                                       short_name=name[:12], jurisdiction=jur, topics=["financial-crime"],
                                                       rights_basis=rights)).inserted_primary_key[0]
            conn.execute(family_members.insert().values(family_id=fam, source_id=sid, relation="root", tier="binding",
                                                        status="active", added_via="manual"))
            return conn.execute(source_versions.insert().values(source_id=sid, version_label="c", version_kind="consolidated",
                                                                content_hash=f"sha:{key}", s3_uri=f"s3://x/{key}",
                                                                status="in_force")).inserted_primary_key[0]

        uk, eu, iso = add("uksi/2017/692", "UK MLRs", "UK"), add("celex/32024R1624", "EU AMLR", "EU"), \
            add("iso/27001-2022", "ISO 27001", "International", license="restricted", rights="licensed")
        ids = {}
        for vid, ref, order, text, h, public in ((uk, "regulation-27", 27, UK_27, "h27", True),
                                                (eu, "art_20", 20, EU_20, "h20", True),
                                                (iso, "clause-4.3", 43, RESTRICTED, "hiso", False)):
            ids[ref] = conn.execute(clauses.insert().values(source_version_id=vid, ref=ref, path=ref, ordering=order, text=text,
                                                            text_hash=h, public_ok=public)).inserted_primary_key[0]
        for oid, key, ref, title, jur, h in ((OB_UK, "uksi/2017/692", "regulation-27", "Apply CDD", "UK", "h27"),
                                             (OB_EU, "celex/32024R1624", "art_20", "Apply CDD (EU)", "EU", "h20")):
            conn.execute(obligations.insert().values(id=oid, source_key=key, clause_ref=ref, title=title,
                                                     statement="A relevant person must…", modality="must", jurisdiction=jur,
                                                     confidence=0.85, status="derived", text_hash=h))
            conn.execute(asserts.insert().values(id=f"AST-{ref}", obligation_id=oid, clause_id=ids[ref], source_key=key,
                                                 clause_ref=ref, strength="explicit", text_hash=h))


def _ob(engine, oid=OB_UK):
    with engine.connect() as conn:
        return dict(conn.execute(obligations.select().where(obligations.c.id == oid)).mappings().first())


def _correction(engine, who=ADA, value="Apply customer due diligence measures", field="title", **kw):
    return c.submit(engine, contributor_email=who, kind="correction", target_ref=OB_UK, field=field,
                    proposed={"value": value}, rationale="heading paraphrased too loosely", **kw)


# --------------------------------------------------------------------------- CLA + roles


def test_cla_required_and_grants_contributor_role(engine):
    _seed(engine)
    with pytest.raises(c.CLARequired):
        _correction(engine)
    sig = c.sign_cla(engine, ADA)
    assert sig["cla_version"] == c.CLA_VERSION and sig["text_hash"] == c.cla_hash() and sig["patent_grant_acknowledged"]
    # signing twice is idempotent
    assert c.sign_cla(engine, ADA)["id"] == sig["id"]
    with engine.connect() as conn:
        assert {"reader", "contributor"} <= c.roles_for(conn, ADA)
        assert "maintainer" in c.roles_for(conn, AVNER)  # CLHEAR_MAINTAINERS


def test_roles_grant_and_revoke_keep_history(engine):
    _seed(engine)
    with pytest.raises(PermissionError):
        c.grant_role(engine, email=BOB, role="reviewer", granted_by=ADA)
    with pytest.raises(c.InvalidContribution):
        c.grant_role(engine, email=BOB, role="contributor", granted_by=AVNER)
    g = c.grant_role(engine, email=BOB, role="reviewer", granted_by=AVNER, note="jurisdiction reviewer, UK")
    assert g["role"] == "reviewer" and g["granted_by"] == AVNER
    assert [r["email"] for r in c.role_holders(engine, "reviewer")] == [BOB]
    assert c.revoke_role(engine, email=BOB, role="reviewer", revoked_by=AVNER) == 1
    assert c.role_holders(engine, "reviewer") == []
    with engine.connect() as conn:  # I2: the closed grant stays
        assert conn.execute(sa.select(sa.func.count()).select_from(roles_t).where(roles_t.c.email == BOB)).scalar() == 1


# --------------------------------------------------------------------------- checks


def test_submit_never_writes_the_registry(engine):
    _seed(engine)
    c.sign_cla(engine, ADA)
    before = _ob(engine)
    row = _correction(engine)
    assert row["status"] == "checked" and all(ch["ok"] for ch in row["checks"])
    assert row["proposal_id"] and l0_proposals.get_proposal(engine, row["proposal_id"])["kind"] == "community_contribution"
    assert _ob(engine) == before
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(human_edits)).scalar() == 0


def test_checks_fail_on_schema_noop_duplicate_and_rights(engine):
    _seed(engine)
    c.sign_cla(engine, ADA)
    bad_schema = c.submit(engine, contributor_email=ADA, kind="missing_source", proposed={"url": "ftp://x", "title": "T"})
    assert bad_schema["status"] == "checks_failed"
    assert "jurisdiction is required" in bad_schema["checks"][0]["detail"] and "not http(s)" not in bad_schema["checks"][0]["detail"]
    assert bad_schema["proposal_id"] is None
    noop = _correction(engine, value="Apply CDD")
    assert noop["status"] == "checks_failed" and "already has this value" in noop["checks"][-1]["detail"]
    first = _correction(engine)
    dup = _correction(engine)
    assert dup["status"] == "checks_failed" and first["id"] in dup["checks"][-1]["detail"]
    verbatim = c.submit(engine, contributor_email=ADA, kind="fill", proposed={"text": RESTRICTED}, rationale="")
    rights = next(ch for ch in verbatim["checks"] if ch["check"] == "rights")
    assert verbatim["status"] == "checks_failed" and not rights["ok"] and "republication basis" in rights["detail"]
    unknown_target = c.submit(engine, contributor_email=ADA, kind="correction", target_ref="OBL:nope#1", field="title",
                              proposed={"value": "x"})
    assert unknown_target["status"] == "checks_failed"
    notes = c.notifications(engine, ADA)
    assert {n["kind"] for n in notes} == {"checks_failed"} and len(notes) == 5
    with pytest.raises(c.InvalidContribution):
        c.submit(engine, contributor_email=ADA, kind="wishlist", proposed={})


# --------------------------------------------------------------------------- re-derivation


def test_rederive_agrees_when_grounded_and_disagrees_when_not(engine):
    _seed(engine)
    c.sign_cla(engine, ADA)
    good = _correction(engine, value="apply customer due diligence measures", field="action")
    v = c.rederive(engine, good["id"])
    assert v["agreement"] == "agree" and v["method"] == "l2.extract" and v["flow"] == c.FLOW_VERSION
    assert c.get(engine, good["id"])["status"] == "rederived"
    bad = _correction(engine, value="file a suspicious activity report within seven days", field="action")
    assert c.rederive(engine, bad["id"])["agreement"] == "disagree"
    failed = c.submit(engine, contributor_email=ADA, kind="fill", proposed={"text": RESTRICTED})
    with pytest.raises(c.WrongStatus):
        c.rederive(engine, failed["id"])
    with pytest.raises(KeyError):
        c.rederive(engine, "CON-999999")
    kinds = {n["kind"] for n in c.notifications(engine, ADA)}
    assert "rederived" in kinds


def test_rederive_pending_is_the_nightly_pass(engine):
    _seed(engine)
    c.sign_cla(engine, ADA)
    _correction(engine, value="apply customer due diligence measures", field="action")
    _correction(engine, value="Relevant person", field="subject")
    out = c.rederive_pending(engine)
    assert out["rederived"] == 2 and out["agree"] >= 1
    assert c.rederive_pending(engine)["rederived"] == 0


# --------------------------------------------------------------------------- review


def test_review_rules_self_non_reviewer_and_one_accept_insufficient(engine):
    _seed(engine)
    c.sign_cla(engine, ADA)
    c.grant_role(engine, email=ADA, role="reviewer", granted_by=AVNER)
    row = _correction(engine)
    with pytest.raises(c.SelfReview):
        c.review(engine, row["id"], reviewer_email=ADA, decision="accept")
    with pytest.raises(c.NotAReviewer):
        c.review(engine, row["id"], reviewer_email=BOB, decision="accept")
    with pytest.raises(c.InvalidContribution):
        c.review(engine, row["id"], reviewer_email=AVNER, decision="maybe")
    one = c.review(engine, row["id"], reviewer_email=AVNER, decision="accept", note="matches the heading")
    assert one["status"] == "rederived" and one["accepts"] == 1 and one["accepts_required"] == 2
    assert _ob(engine)["title"] == "Apply CDD"  # nothing applied yet
    # the same reviewer voting twice does not count twice
    again = c.review(engine, row["id"], reviewer_email=AVNER, decision="accept")
    assert again["status"] == "rederived" and again["accepts"] == 1


def test_two_accepts_apply_through_the_record_path(engine):
    _seed(engine)
    c.sign_cla(engine, ADA)
    row = _correction(engine)
    c.review(engine, row["id"], reviewer_email=AVNER, decision="accept")
    done = c.review(engine, row["id"], reviewer_email=MAINT, decision="accept")
    assert done["status"] == "accepted" and done["applied"]["edit_id"].startswith("EDT-")
    assert done["applied"]["before"] == "Apply CDD" and done["applied"]["after"] == "Apply customer due diligence measures"
    ob = _ob(engine)
    assert ob["title"] == "Apply customer due diligence measures"
    with engine.connect() as conn:
        edit = conn.execute(human_edits.select().where(human_edits.c.subject_ref == OB_UK)).mappings().first()
        assert edit is not None and edit["field"] == "title" and edit["accepted_by"].startswith("reviewers:")
        assert AVNER in edit["accepted_by"] and MAINT in edit["accepted_by"] and edit["proposal_id"] == row["proposal_id"]
        # I2 / I3: the obligation row is versioned and carries a why-trail (the console's record path)
        versions = conn.execute(sa.select(sa.func.count()).select_from(obligations).where(obligations.c.id == OB_UK)).scalar()
        why = conn.execute(sa.select(record.why_trails.c.reasoning_summary).where(record.why_trails.c.subject_ref == OB_UK)
                           .order_by(record.why_trails.c.id.desc())).first()
    assert versions >= 1 and why is not None and row["id"] in why[0]
    assert l0_proposals.get_proposal(engine, row["proposal_id"])["status"] == "approved"
    assert any(n["kind"] == "accepted" for n in c.notifications(engine, ADA))


def test_equivalence_contribution_writes_the_edge_with_why(engine):
    _seed(engine)
    c.sign_cla(engine, BOB)
    row = c.submit(engine, contributor_email=BOB, kind="equivalence", proposed={"obligation_a": OB_UK, "obligation_b": OB_EU},
                   rationale="same CDD duty under MLR 27 and AMLR 20")
    assert row["status"] == "checked"
    v = c.rederive(engine, row["id"])
    assert v["agreement"] in ("agree", "unverified", "disagree") and "similarity" in (v.get("derived") or {})
    c.review(engine, row["id"], reviewer_email=AVNER, decision="accept")
    done = c.review(engine, row["id"], reviewer_email=MAINT, decision="accept")
    assert done["status"] == "accepted" and done["applied"]["edge"] == "equivalence"
    with engine.connect() as conn:
        edge = conn.execute(equivalences.select().where(equivalences.c.id == done["applied"]["id"])).mappings().first()
        assert edge["basis"] == "human" and edge["method"] == f"contribution:{row['id']}"
        why = conn.execute(record.why_trails.select().where(record.why_trails.c.id == edge["why_trail_id"])).mappings().first()
    assert why["layer"] == "L2" and why["agent_id"] == "community"
    dup = c.submit(engine, contributor_email=BOB, kind="equivalence", proposed={"obligation_a": OB_EU, "obligation_b": OB_UK})
    assert dup["status"] == "checks_failed" and "already recorded" in dup["checks"][-1]["detail"]


def test_reject_closes_and_flips_the_proposal(engine):
    _seed(engine)
    c.sign_cla(engine, ADA)
    row = _correction(engine)
    out = c.review(engine, row["id"], reviewer_email=AVNER, decision="reject", note="the heading is fine")
    assert out["status"] == "rejected"
    assert l0_proposals.get_proposal(engine, row["proposal_id"])["status"] == "rejected"
    with pytest.raises(c.WrongStatus):
        c.review(engine, row["id"], reviewer_email=MAINT, decision="accept")
    assert _ob(engine)["title"] == "Apply CDD"
    assert any(n["kind"] == "rejected" for n in c.notifications(engine, ADA))


def test_console_decision_counts_as_one_reviewer_vote(engine, client):
    _seed(engine)
    c.sign_cla(engine, ADA)
    row = _correction(engine)
    resp = client.post(f"/console/proposals/{row['proposal_id']}/approve", headers={"X-Reg42-User": AVNER})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["contribution"]["status"] == "rederived" and body["contribution"]["accepts"] == 1
    assert body["status"] == "proposed"  # the flow flips it when the second accept lands
    assert _ob(engine)["title"] == "Apply CDD"
    done = c.review(engine, row["id"], reviewer_email=MAINT, decision="accept")
    assert done["status"] == "accepted"
    assert l0_proposals.get_proposal(engine, row["proposal_id"])["status"] == "approved"


# --------------------------------------------------------------------------- release, attribution, impact


def test_release_ships_contributions_with_attribution_and_impact(engine):
    _seed(engine)
    c.sign_cla(engine, ADA)
    row = _correction(engine)
    c.review(engine, row["id"], reviewer_email=AVNER, decision="accept")
    c.review(engine, row["id"], reviewer_email=MAINT, decision="accept")
    shipped = c.release_contributions(engine, "2026.09")
    assert len(shipped) == 1 and shipped[0]["contribution_id"] == row["id"] and shipped[0]["contributor"] == "ada"
    assert shipped[0]["impact"]["obligations_changed"] == 1 and "blueprints_changed" in shipped[0]["impact"]
    got = c.get(engine, row["id"])
    assert got["status"] == "released" and got["released_in"] == "2026.09"
    note = next(n for n in c.notifications(engine, ADA) if n["kind"] == "released")
    assert "shipped in release 2026.09" in note["message"] and "1 obligation" in note["message"]
    assert c.attribution(engine, "2026.09") == shipped
    assert c.release_contributions(engine, "2026.10") == []  # nothing left to ship
    board = c.leaderboard(engine)
    assert board[0]["contributor"] == "ada" and board[0]["released"] == 1 and board[0]["score"] > 0
    prof = c.profile(engine, ADA)
    assert prof["cla"]["signed"] and prof["released"] == 1 and prof["contributions"][0]["status"] == "released"
    summary = c.summary(engine)
    assert summary["by_status"] == {"released": 1} and summary["contributors"] == 1


def test_publish_release_carries_contribution_attribution_in_manifest(engine, monkeypatch):
    from app.clhear import releases

    _seed(engine)
    c.sign_cla(engine, ADA)
    row = _correction(engine)
    c.review(engine, row["id"], reviewer_email=AVNER, decision="accept")
    c.review(engine, row["id"], reviewer_email=MAINT, decision="accept")
    manifest = releases.build_manifest(release_id="rel-1", snapshot_uri="s3://x/rel-1.json", content_hash="h", counts={},
                                       engine=engine, contributions=c.release_contributions(engine, "rel-1"))
    assert manifest["contributions"]["count"] == 1 and manifest["contributions"]["contributors"] == ["ada"]
    assert manifest["contributions"]["attribution"][0]["contribution_id"] == row["id"]


# --------------------------------------------------------------------------- API + page


def _login(client, email=ADA):
    resp = client.post("/auth/email", json={"email": email})
    link = resp.json()["debug_link"]
    assert client.get(link.split("clhear.reg42.ai")[1], follow_redirects=False).status_code == 307
    return client


def test_api_flow_end_to_end(client, engine, monkeypatch):
    monkeypatch.setenv("CLHEAR_AUTH_DEBUG", "true")
    from app.clhear.settings import get_settings

    get_settings.cache_clear()
    _seed(engine)
    page = client.get("/contribute")
    assert page.status_code == 200 and 'id="main"' in page.text and "Contribute" in page.text
    kinds = client.get("/contributions/kinds").json()
    assert {k["kind"] for k in kinds["kinds"]} >= {"correction", "golden_case", "equivalence"} and kinds["accepts_required"] == 2
    body = {"kind": "correction", "target_ref": OB_UK, "field": "title", "proposed": {"value": "Apply customer due diligence measures"}}
    assert client.post("/contributions", json=body).status_code == 401
    _login(client)
    cla = client.get("/cla").json()
    assert cla["signed"] is None and cla["text_hash"] == c.cla_hash() and "patent" in cla["text"].lower()
    denied = client.post("/contributions", json=body)
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "cla_required"
    assert client.post("/cla/sign", json={"acknowledge_patent_grant": False}).status_code == 422
    assert client.post("/cla/sign", json={"acknowledge_patent_grant": True}).status_code == 200
    filed = client.post("/contributions", json=body)
    assert filed.status_code == 201, filed.text
    cid = filed.json()["id"]
    assert client.post("/contributions", json={"kind": "nonsense", "proposed": {}}).status_code == 422
    assert client.get("/contributions?status=checked").json()["count"] == 1
    assert client.get(f"/contributions/{cid}").json()["status"] == "checked"
    assert client.get("/contributions/nope").status_code == 404
    # a non-reviewer cannot re-derive; the session user wins over a maintainer header, so ada's vote is refused
    assert client.post(f"/contributions/{cid}/rederive").status_code == 403
    own = client.post(f"/contributions/{cid}/reviews", json={"decision": "accept"}, headers={"X-Reg42-User": AVNER})
    assert own.status_code == 403 and own.json()["detail"]["code"] == "not_a_reviewer"
    client.post("/auth/logout")
    stranger = client.post(f"/contributions/{cid}/reviews", json={"decision": "accept"}, headers={"X-Reg42-User": "nobody@x.org"})
    assert stranger.status_code == 401  # not a session user, not a maintainer header
    # maintainers vote through the header identity
    r1 = client.post(f"/contributions/{cid}/reviews", json={"decision": "accept"}, headers={"X-Reg42-User": AVNER})
    assert r1.status_code == 200 and r1.json()["accepts"] == 1 and r1.json()["status"] == "rederived"
    r2 = client.post(f"/contributions/{cid}/reviews", json={"decision": "accept"}, headers={"X-Reg42-User": MAINT})
    assert r2.status_code == 200 and r2.json()["status"] == "accepted"
    assert _ob(engine)["title"] == "Apply customer due diligence measures"
    _login(client)
    notes = client.get("/contributions/notifications?unread=true").json()
    assert notes["unread"] >= 1 and client.post("/contributions/notifications/read").json()["marked"] >= 1
    me = client.get("/contributors/me").json()
    assert me["accepted"] == 1 and "contributor" in me["roles"]
    board = client.get("/contributors").json()
    assert board["items"][0]["contributor"] == "ada"
    # roles need the maintainer role
    assert client.post("/roles", json={"email": BOB, "role": "reviewer"}).status_code == 403
    both = client.post("/roles", json={"email": BOB, "role": "reviewer"}, headers={"X-Reg42-User": AVNER})
    assert both.status_code == 403  # the session user (ada) wins over the header when both are present
    client.post("/auth/logout")
    assert client.post("/roles", json={"email": BOB, "role": "reviewer"}, headers={"X-Reg42-User": AVNER}).status_code == 201
    assert [r["email"] for r in client.get("/roles?role=reviewer").json()["items"]] == [BOB]
    gov = client.get("/governance").json()
    assert {d["slug"] for d in gov["docs"]} >= {"charter", "cla", "code-of-conduct", "release-policy", "deprecation-policy",
                                                 "conflict-of-interest", "steering-voting", "working-groups"}
    assert client.get("/governance/charter").text.startswith("#")
    assert client.get("/governance/nope").status_code == 404
    assert "roadmap" in client.get("/roadmap").text.lower()
    assert client.get("/contributions/summary").json()["by_status"] == {"accepted": 1}
    assert client.get("/newsletter/digest").json()["count"] == 0
    assert client.post("/newsletter/subscribe", json={"email": "nope"}).status_code == 422
    assert client.post("/newsletter/subscribe", json={"email": ADA}).json()["subscribed"] is False  # unconfigured: inert


def test_nightly_stack_includes_the_community_pass(engine):
    """The nightly outputs carry the re-derivation counts and the (inert) digest."""
    import inspect

    from app.clhear import fleets

    src = inspect.getsource(fleets.run_nightly_stack)
    assert "rederive_pending" in src and "send_digest" in src and '"community": community' in src


def test_contributions_table_is_in_the_community_schema(engine):
    assert contributions_t.schema == "community"
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(contributions_t)).scalar() == 0
    assert json.dumps(c.REQUIRED_FIELDS)  # serialisable reference for the UI
