"""Conformance program (standard §7, Annex E; item 15).

CL1 / CL2 self-assessments evaluated against the published ``criteria.json``, verified
and granted by program verifiers at most at the level the checks support; CL3 / CL4
marks recorded from accredited assessors' ISAE 3000 reports; a public register that
never forgets a mark; the artefacts shipped in the public repo.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.clhear import conformance as cf
from app.clhear.l6 import composer
from app.clhear.platform import contributions as contrib
from tests.test_l6_blueprints import BROKER, _corpus

MAINT = "avner@reg42.ai"
ADA = "ada@example.com"
SCOPE = "Example Broker Ltd (UK, FCA-authorised), retail brokerage business line, jurisdiction UK; no EU entity."


def _blueprint(engine, tmp_path):
    _corpus(engine, tmp_path)
    bp = composer.compose(engine, {"attributes": BROKER}, requested_by="test")
    with engine.connect() as conn:
        stored = composer.get_blueprint(conn, bp["blueprint_id"])
    items = [{"item_id": it["id"], "obligations": it.get("obligations_satisfied") or []} for it in stored["composition"]["items"]]
    return bp["blueprint_id"], items


def _form(blueprint_id, items, level="CL1", **over):
    form = {
        "organization": "Example Broker Ltd", "program": "Conduct & AML program", "scope": SCOPE, "release": "latest",
        "blueprint_id": blueprint_id, "profile_confirmed": True, "level_claimed": level,
        "items": [{"item_id": it["item_id"], "status": "operated", "owner": "Head of Compliance",
                   "evidence": [{"ref": f"DOC-{i:03d}", "kind": "policy", "obligation_ids": it["obligations"]}]} for i, it in enumerate(items)],
        "signatory": {"name": "A. Officer", "role": "Head of Compliance", "date": date.today().isoformat()},
    }
    if level == "CL2":
        form.update(change_tracking={"watchlist_id": "WL-1", "last_reconciled_release": "latest"}, reconciliation_days=14,
                    last_review={"date": date.today().isoformat(), "reviewer_role": "Internal audit", "outcome": "no findings"})
    form.update(over)
    return form


# --------------------------------------------------------------------------- criteria file


def test_criteria_file_is_the_single_source_and_shapes_the_levels():
    c = cf.criteria()
    assert [lv["level"] for lv in c["levels"]] == ["CL1", "CL2", "CL3", "CL4"] and c["version"] == "1.0"
    ids = [x["id"] for x in c["criteria"]]
    assert len(ids) == len(set(ids)) and all(x["level"] in cf.LEVELS for x in c["criteria"])
    assert {x["automated_check"] for x in c["criteria"] if x["automated_check"]} == {
        "release_named", "blueprint_current", "scope_stated", "blueprint_complete", "items_mapped", "owners_named", "exclusions_reasoned",
        "evidence_per_item", "evidence_traces", "deviations_reasoned", "change_tracking", "reconciliation_window", "review_recorded", "signed"}
    assert all(x["verification"] == "assessor" and not x["automated_check"] for x in c["criteria"] if x["level"] in ("CL3", "CL4"))
    assert len(cf.criteria_for("CL1")) < len(cf.criteria_for("CL2")) < len(cf.criteria_for("CL4"))
    with pytest.raises(cf.InvalidSubmission):
        cf.criteria_for("CL9")
    # the documents that explain the criteria ship alongside, and the schema validates the form shape
    d = cf.CONFORMANCE_DIR
    for name in ("README.md", "LEVELS.md", "ANNEX_E_SELF_ASSESSMENT.md", "ASSESSOR_GUIDE.md", "MARKS_POLICY.md", "self_assessment.schema.json",
                 "evidence_templates/README.md", "evidence_templates/document.md", "evidence_templates/role.md"):
        assert (d / name).exists(), name
    annex = (d / "ANNEX_E_SELF_ASSESSMENT.md").read_text()
    assert all(x["id"] in annex for x in c["criteria"] if x["level"] in ("CL1", "CL2"))
    assert all(x["automated_check"] in annex for x in c["criteria"] if x["automated_check"])
    schema = json.loads((d / "self_assessment.schema.json").read_text())
    assert schema["properties"]["level_claimed"]["enum"] == ["CL1", "CL2"] and schema["properties"]["profile_confirmed"]["const"] is True
    guide = (d / "ASSESSOR_GUIDE.md").read_text()
    assert "ISAE 3000" in guide and "independent of the organisation *and of Reg42" in guide and "six months" in guide


# --------------------------------------------------------------------------- self-assessment + checks


def test_cl1_submission_runs_checks_and_supports_cl1_not_cl2(engine, tmp_path):
    bid, items = _blueprint(engine, tmp_path)
    r = cf.submit(engine, _form(bid, items), submitted_by=ADA)
    assert r["id"].startswith("CFA-") and r["status"] == "submitted" and r["level_claimed"] == "CL1" and r["level_supported"] == "CL1"
    assert r["blueprint_fingerprint"] and r["criteria_version"] == "1.0" and r["submitted_by"] == ADA
    by = {c["check"]: c for c in r["checks"]}
    assert all(by[c]["ok"] for c in ("release_named", "blueprint_current", "scope_stated", "blueprint_complete", "items_mapped", "owners_named",
                                     "exclusions_reasoned", "signed"))
    assert by["change_tracking"]["ok"] is False and by["reconciliation_window"]["ok"] is False and by["review_recorded"]["ok"] is False
    assert by["items_mapped"]["detail"].startswith(f"{len(items)}/{len(items)} items mapped")
    assert [c["criterion"] for c in r["checks_failed"]] == ["E.4.1", "E.4.2", "E.5.1"]


def test_cl2_submission_and_the_ways_it_fails(engine, tmp_path):
    bid, items = _blueprint(engine, tmp_path)
    ok = cf.submit(engine, _form(bid, items, "CL2"), submitted_by=ADA)
    assert ok["level_supported"] == "CL2" and ok["checks_failed"] == []
    # an operated item without evidence, an untraced one, an unjustified exclusion, a missing item, a wide window
    form = _form(bid, items, "CL2", reconciliation_days=45)
    form["items"][0]["evidence"] = []
    form["items"][1]["evidence"][0]["obligation_ids"] = []
    form["items"][2] = {"item_id": items[2]["item_id"], "status": "out_of_scope", "reason": "not material to us"}
    dropped = form["items"].pop()
    bad = cf.submit(engine, form, submitted_by=ADA)
    by = {c["check"]: c for c in bad["checks"]}
    assert bad["level_supported"] is None  # a missing item already breaks CL1
    assert by["items_mapped"]["ok"] is False and dropped["item_id"] in by["items_mapped"]["detail"]
    assert by["evidence_per_item"]["ok"] is False and items[0]["item_id"] in by["evidence_per_item"]["detail"]
    assert by["evidence_traces"]["ok"] is False if items[1]["obligations"] else by["evidence_traces"]["ok"] is True
    assert by["exclusions_reasoned"]["ok"] is False and by["reconciliation_window"]["ok"] is False
    # a factual exclusion is fine; an owner-less operated item is not
    form = _form(bid, items, "CL2")
    form["items"][0] = {"item_id": items[0]["item_id"], "status": "out_of_scope", "reason": "no retail clients in this entity"}
    form["items"][1]["owner"] = ""
    r = cf.submit(engine, form, submitted_by=ADA)
    by = {c["check"]: c for c in r["checks"]}
    assert by["exclusions_reasoned"]["ok"] is True and by["owners_named"]["ok"] is False and r["level_supported"] is None
    # form validation
    for broken, msg in ((dict(level_claimed="CL3"), "CL3 and CL4 come from an assessor"), (dict(profile_confirmed=False), "profile_confirmed"),
                        (dict(blueprint_id="nope"), "BLU-"), (dict(items=[]), "items"), (dict(signatory=None), "signatory is required")):
        with pytest.raises(cf.InvalidSubmission, match=msg):
            cf.submit(engine, {**_form(bid, items), **broken}, submitted_by=ADA)
    form = _form(bid, items)
    form["items"][0]["evidence"] = [{"ref": "the whole policy text\npasted here", "kind": "policy"}]
    with pytest.raises(cf.InvalidSubmission, match="not content"):
        cf.submit(engine, form, submitted_by=ADA)
    # an unknown blueprint fails the blueprint checks but is still stored (the submitter sees why)
    r = cf.submit(engine, _form("BLU-999999", items), submitted_by=ADA)
    by = {c["check"]: c for c in r["checks"]}
    assert by["blueprint_current"]["ok"] is False and by["blueprint_complete"]["ok"] is False and by["items_mapped"]["ok"] is False


# --------------------------------------------------------------------------- decisions + marks


def test_verifier_grants_at_most_the_supported_level_and_the_register_remembers(engine, tmp_path):
    bid, items = _blueprint(engine, tmp_path)
    r = cf.submit(engine, _form(bid, items, "CL2"), submitted_by=ADA)
    with pytest.raises(cf.NotPermitted):
        cf.decide(engine, r["id"], decided_by=ADA, decision="grant")
    with pytest.raises(cf.InvalidSubmission):
        cf.decide(engine, r["id"], decided_by=MAINT, decision="approve")
    # claimed CL2, supported CL2, verifier chooses CL1 — allowed (lower); CL3 is not a self-assessed level
    with pytest.raises(cf.InvalidSubmission):
        cf.decide(engine, r["id"], decided_by=MAINT, decision="grant", level="CL3")
    g = cf.decide(engine, r["id"], decided_by=MAINT, decision="grant", level="CL1", note="evidence sample thin; CL2 on re-submission")
    assert g["status"] == "granted" and g["level_granted"] == "CL1" and g["mark_id"].startswith("CFM-") and g["decided_by"] == MAINT
    with pytest.raises(cf.InvalidSubmission, match="already granted"):
        cf.decide(engine, r["id"], decided_by=MAINT, decision="decline")
    m = cf.get_mark(engine, g["mark_id"])
    assert m["level"] == "CL1" and m["mark"] == "CLHEAR CL1 Mapped" and m["status"] == "granted" and m["assessment_id"] == r["id"]
    assert m["organization"] == "Example Broker Ltd" and m["blueprint_id"] == bid and m["granted_by"] == MAINT
    assert date.fromisoformat(m["valid_to"]) - date.fromisoformat(m["valid_from"]) >= timedelta(days=360)
    assert m["statement"].startswith("CLHEAR CL1 Mapped — Conduct & AML program, Example Broker Ltd, release latest") and m["id"] in m["statement"]
    # a submission whose checks support nothing cannot be granted
    weak = _form(bid, items)
    weak["items"][0]["owner"] = ""
    w = cf.submit(engine, weak, submitted_by=ADA)
    with pytest.raises(cf.InvalidSubmission, match="support no level"):
        cf.decide(engine, w["id"], decided_by=MAINT, decision="grant")
    d = cf.decide(engine, w["id"], decided_by=MAINT, decision="request_changes", note="name an owner for every operated item")
    assert d["status"] == "changes_requested"
    # withdraw: the register keeps the entry, the assessment follows
    with pytest.raises(cf.InvalidSubmission):
        cf.withdraw_mark(engine, g["mark_id"], withdrawn_by=MAINT, reason="")
    wd = cf.withdraw_mark(engine, g["mark_id"], withdrawn_by=MAINT, reason="profile changed materially (EU entity added)")
    assert wd["status"] == "withdrawn" and wd["withdrawal_reason"].startswith("profile changed") and wd["withdrawn_by"] == MAINT
    assert cf.get_assessment(engine, r["id"])["status"] == "withdrawn"
    reg = cf.register(engine)
    assert [x["id"] for x in reg] == [g["mark_id"]] and reg[0]["status"] == "withdrawn"
    assert cf.register(engine, include_withdrawn=False) == [] and cf.register(engine, organization="broker")[0]["id"] == g["mark_id"]
    with engine.connect() as conn:  # nothing deleted (I2)
        assert conn.execute(sa.select(sa.func.count()).select_from(cf.marks)).scalar() == 1
        assert conn.execute(sa.select(sa.func.count()).select_from(cf.self_assessments)).scalar() == 2
    s = cf.summary(engine)
    assert s["marks_granted"] == {"CL1": 0, "CL2": 0, "CL3": 0, "CL4": 0} and s["self_assessments"] == 2 and s["pending_verification"] == 0
    # an expired grant reads as expired without touching the row
    with engine.begin() as conn:
        conn.execute(cf.marks.update().where(cf.marks.c.id == g["mark_id"]).values(status="granted", valid_to=date.today() - timedelta(days=1)))
    assert cf.get_mark(engine, g["mark_id"])["status"] == "expired"
    with engine.connect() as conn:
        assert conn.execute(sa.select(cf.marks.c.status)).scalar() == "granted"


def test_cl3_mark_needs_an_accredited_assessor_and_six_months(engine, tmp_path):
    bid, _ = _blueprint(engine, tmp_path)
    with pytest.raises(cf.NotPermitted):
        cf.accredit_assessor(engine, name="I. Assessor", email="ia@audit.example", licence_ref="ICAEW 12345", briefing_completed=date.today(),
                             conflicts_declared=True, accredited_by=ADA)
    with pytest.raises(cf.InvalidSubmission, match="conflicts"):
        cf.accredit_assessor(engine, name="I. Assessor", email="ia@audit.example", licence_ref="ICAEW 12345", briefing_completed=date.today(),
                             conflicts_declared=False, accredited_by=MAINT)
    a = cf.accredit_assessor(engine, name="I. Assessor", email="ia@audit.example", firm="Audit LLP", jurisdiction="UK", licence_ref="ICAEW 12345",
                             briefing_completed=date.today(), conflicts_declared=True, accredited_by=MAINT)
    assert a["id"] and a["accredited_by"] == MAINT and date.fromisoformat(a["valid_to"]) > date.today() + timedelta(days=700)
    pub = cf.list_assessors(engine)
    assert pub[0]["active"] is True and "email" not in pub[0] and pub[0]["firm"] == "Audit LLP"
    common = dict(organization="Example Broker Ltd", program="Conduct & AML program", scope=SCOPE, release="clhear-v2026.09.01", blueprint_id=bid,
                  assessor_id=a["id"], report_ref="ISAE3000-2026-017", recorded_by=MAINT)
    start, end = date.today() - timedelta(days=200), date.today() - timedelta(days=5)
    with pytest.raises(cf.InvalidSubmission, match="CL1/CL2 come from a self-assessment"):
        cf.record_assessed_mark(engine, level="CL2", period_start=start, period_end=end, **common)
    with pytest.raises(cf.InvalidSubmission, match="six months"):
        cf.record_assessed_mark(engine, level="CL3", period_start=end - timedelta(days=90), period_end=end, **common)
    with pytest.raises(cf.InvalidSubmission, match="accredited register"):
        cf.record_assessed_mark(engine, level="CL3", period_start=start, period_end=end, **{**common, "assessor_id": 999})
    with pytest.raises(cf.NotPermitted):
        cf.record_assessed_mark(engine, level="CL3", period_start=start, period_end=end, **{**common, "recorded_by": ADA})
    m = cf.record_assessed_mark(engine, level="CL3", period_start=start, period_end=end, **common)
    assert m["level"] == "CL3" and m["mark"] == "CLHEAR CL3 Assessed" and m["assessor_id"] == a["id"] and m["report_ref"] == "ISAE3000-2026-017"
    assert m["period_start"] == start.isoformat() and m["assessment_id"] is None
    # a withdrawn assessor can no longer anchor a mark
    cf.withdraw_assessor(engine, a["id"], withdrawn_by=MAINT, reason="report found materially wrong")
    assert cf.list_assessors(engine)[0]["active"] is False and cf.list_assessors(engine, active_only=True) == []
    with pytest.raises(cf.InvalidSubmission, match="accredited register"):
        cf.record_assessed_mark(engine, level="CL4", period_start=start, period_end=end, **common)
    assert cf.summary(engine)["marks_granted"]["CL3"] == 1


# --------------------------------------------------------------------------- API + page


def _login(client, email=ADA):
    resp = client.post("/auth/email", json={"email": email})
    link = resp.json()["debug_link"]
    assert client.get(link.split("clhear.reg42.ai")[1], follow_redirects=False).status_code == 307


def test_api_and_page(client, engine, tmp_path, monkeypatch):
    monkeypatch.setenv("CLHEAR_AUTH_DEBUG", "true")
    from app.clhear.settings import get_settings

    get_settings.cache_clear()
    bid, items = _blueprint(engine, tmp_path)
    page = client.get("/conformance")
    assert page.status_code == 200 and 'id="main"' in page.text and "Annex E self-assessment" in page.text
    lv = client.get("/conformance/levels").json()
    assert [x["level"] for x in lv["levels"]] == ["CL1", "CL2", "CL3", "CL4"] and lv["criteria"] and lv["documents"]["annex_e"]
    assert client.get("/conformance/criteria?level=CL1").json()["count"] == len(cf.criteria_for("CL1"))
    assert client.get("/conformance/criteria?level=CL7").status_code == 422
    assert client.get("/conformance/register").json() == {"marks": [], "count": 0, "note": client.get("/conformance/register").json()["note"]}
    assert client.get("/conformance/assessors").json()["count"] == 0
    assert client.get("/conformance/whoami").json() == {"signed_in": False, "verifier": False}
    form = _form(bid, items, "CL2")
    assert client.post("/conformance/self-assessments", json=form).status_code == 401
    _login(client)
    r = client.post("/conformance/self-assessments", json=form)
    assert r.status_code == 201 and r.json()["level_supported"] == "CL2"
    cid = r.json()["id"]
    assert client.post("/conformance/self-assessments", json={**form, "level_claimed": "CL4"}).status_code == 422
    mine = client.get("/conformance/self-assessments").json()
    assert mine["count"] == 1 and mine["assessments"][0]["id"] == cid and "form" not in mine["assessments"][0]
    assert client.get(f"/conformance/self-assessments/{cid}").json()["form"]["organization"] == "Example Broker Ltd"
    assert client.get("/conformance/queue").status_code == 403
    assert client.post(f"/conformance/self-assessments/{cid}/decision", json={"decision": "grant"}).status_code == 403
    # another signed-in user may not read it
    client.post("/auth/logout")
    _login(client, "bob@example.com")
    assert client.get(f"/conformance/self-assessments/{cid}").status_code == 403
    client.post("/auth/logout")
    # the verifier (maintainer header) decides
    h = {"X-Reg42-User": MAINT}
    q = client.get("/conformance/queue", headers=h).json()
    assert q["count"] == 1 and q["assessments"][0]["id"] == cid
    assert client.get(f"/conformance/self-assessments/{cid}", headers=h).status_code == 200
    d = client.post(f"/conformance/self-assessments/{cid}/decision", json={"decision": "grant", "note": "verified"}, headers=h)
    assert d.status_code == 200 and d.json()["level_granted"] == "CL2" and d.json()["mark_id"]
    mid = d.json()["mark_id"]
    reg = client.get("/conformance/register").json()
    assert reg["count"] == 1 and reg["marks"][0]["id"] == mid and reg["marks"][0]["mark"] == "CLHEAR CL2 Traceable"
    one = client.get(f"/conformance/register/{mid}").json()
    assert one["statement"].startswith("CLHEAR CL2 Traceable") and client.get("/conformance/register/CFM-999999").status_code == 404
    # assessors + CL3 mark through the API
    a = client.post("/conformance/assessors", json={"name": "I. Assessor", "email": "ia@audit.example", "firm": "Audit LLP", "jurisdiction": "UK",
                                                    "licence_ref": "ICAEW 12345", "briefing_completed": date.today().isoformat(), "conflicts_declared": True}, headers=h)
    assert a.status_code == 201
    body = {"level": "CL3", "organization": "Example Broker Ltd", "program": "Conduct & AML program", "scope": SCOPE, "release": "latest",
            "blueprint_id": bid, "assessor_id": a.json()["id"], "report_ref": "ISAE3000-2026-017",
            "period_start": (date.today() - timedelta(days=200)).isoformat(), "period_end": date.today().isoformat()}
    assert client.post("/conformance/marks", json=body).status_code == 401
    m3 = client.post("/conformance/marks", json=body, headers=h)
    assert m3.status_code == 201 and m3.json()["level"] == "CL3"
    assert client.post("/conformance/marks", json={**body, "level": "CL1"}, headers=h).status_code == 422
    w = client.post(f"/conformance/marks/{mid}/withdraw", json={"reason": "holder's request"}, headers=h)
    assert w.status_code == 200 and w.json()["status"] == "withdrawn"
    assert client.get("/conformance/register?include_withdrawn=false").json()["count"] == 1
    s = client.get("/conformance/summary").json()
    assert s["marks_granted"] == {"CL1": 0, "CL2": 0, "CL3": 1, "CL4": 0} and s["accredited_assessors"] == 1
    assert client.get("/conformance/whoami", headers=h).json()["verifier"] is True


def test_public_repo_ships_the_conformance_program(engine, tmp_path):
    from app.clhear.platform import public_repo

    repo = tmp_path / "repo"
    repo.mkdir()
    written = {p.relative_to(repo).as_posix() for p in public_repo.copy_static(repo)}
    for name in ("conformance/README.md", "conformance/criteria.json", "conformance/ANNEX_E_SELF_ASSESSMENT.md", "conformance/ASSESSOR_GUIDE.md",
                 "conformance/MARKS_POLICY.md", "conformance/LEVELS.md", "conformance/self_assessment.schema.json", "conformance/evidence_templates/process.md"):
        assert name in written, name
    assert json.loads((repo / "conformance/criteria.json").read_text())["version"] == cf.criteria()["version"]
