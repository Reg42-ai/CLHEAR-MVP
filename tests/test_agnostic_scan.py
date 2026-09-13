"""Instance-mode contract with Reg42 OS (HLD v2 I5, I9; build item 18).

Acceptance (HLD v2 §8 item 18): the agnostic store scan finds **zero organization
identifiers after an instance-mode session**. Plus: the contract shapes and formula,
the read-only session guarantee, the deployment guard, and the public node's refusal.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.clhear import instance_contract as ic
from app.clhear.derived_models import blocks as blocks_t
from app.clhear.l6 import composer
from app.clhear.platform import agnostic_scan, record
from tests.test_l6_blueprints import BROKER, _corpus

ROOT = Path(__file__).resolve().parents[1]

# A synthetic adopter whose identifiers must never appear in the CLHEAR store.
ORG = "Acme Bank plc"
ORG_DOMAIN = "acmebank.com"
ORG_EMAIL = f"cfo@{ORG_DOMAIN}"
ORG_IBAN = "GB29NWBK60161331926819"
ORG_PHONE = "+44 20 7946 0958"
DENYLIST = [ORG.lower(), "acme bank", ORG_DOMAIN]


def _composition() -> dict:
    """A small open blueprint composition in the L6 shape."""
    return {
        "blueprint_id": "BLU-000001", "release": "2026.09.13",
        "items": [
            {"id": "ITM-1", "block_id": "BLK-CDD", "kind": "Process", "name": "Customer due diligence", "basis": "required",
             "obligations_satisfied": ["OB-1", "OB-2"], "load_bearing_for": ["OB-1", "OB-2"],
             "characteristics": [{"key": "verification_before_relationship", "in_profile": True}, {"key": "pep_screening", "in_profile": True}]},
            {"id": "ITM-2", "block_id": "BLK-RECON", "kind": "Workflow", "name": "Client money reconciliation", "basis": "selected",
             "obligations_satisfied": ["OB-3"], "load_bearing_for": [],
             "characteristics": [{"key": "frequency", "in_profile": True}, {"key": "sign_off", "in_profile": True}, {"key": "eu_only", "in_profile": False}]},
            {"id": "ITM-3", "block_id": "BLK-CONFLICTS", "kind": "Document", "name": "Conflicts of interest policy", "basis": "required",
             "obligations_satisfied": ["OB-4"], "load_bearing_for": ["OB-4"], "characteristics": []},
            {"id": "ITM-4", "block_id": "BLK-RETENTION", "kind": "Configuration", "name": "Record retention", "basis": "selected",
             "obligations_satisfied": ["OB-5"], "load_bearing_for": ["OB-5"], "characteristics": []},
            {"id": "ITM-5", "block_id": "BLK-BREACH-NOTICE", "kind": "Process", "name": "Breach notification", "basis": "selected",
             "obligations_satisfied": ["OB-6"], "load_bearing_for": [], "characteristics": []},
        ],
    }


def _overlay(**kw) -> ic.ActualOverlay:
    base = {"blueprint_id": "BLU-000001", "release": "2026.09.13", "as_of": "2026-09-12", "actuals": [
        {"block_id": "BLK-CDD", "state": "present", "owner": f"Head of Compliance, {ORG}", "evidence_refs": [f"https://intranet.{ORG_DOMAIN}/cdd"]},
        {"block_id": "BLK-RECON", "state": "partial", "characteristics_met": ["frequency"], "notes": f"contact {ORG_EMAIL}"},
        {"block_id": "BLK-CONFLICTS", "state": "planned", "target_date": "2026-12-31"},
        {"block_id": "BLK-RETENTION", "state": "absent"},
        {"block_id": "BLK-NOT-IN-BLUEPRINT", "state": "present"},
    ]}
    base.update(kw)
    return ic.ActualOverlay.model_validate(base)


# --------------------------------------------------------------------------- the contract computation (pure)


def test_gap_diff_marks_ghost_solid_and_delta_and_never_echoes_free_text():
    base = {"ITM-1": {"composite": 0.9, "band": "high"}, "BLK-RECON": {"composite": 0.4, "band": "medium"}, "ITM-4": {"composite": 0.6, "band": "medium"}}
    diff = ic.gap_diff(_composition(), _overlay(), base)
    by = {g.block_id: g for g in diff.items}
    assert by["BLK-CDD"].state == "present" and not by["BLK-CDD"].gap and by["BLK-CDD"].obligations_at_risk == [] and by["BLK-CDD"].base_priority == 0.9
    assert by["BLK-RECON"].state == "partial" and by["BLK-RECON"].gap and by["BLK-RECON"].missing_characteristics == ["sign_off"]  # eu_only not in profile
    assert by["BLK-CONFLICTS"].state == "planned" and by["BLK-CONFLICTS"].gap and by["BLK-CONFLICTS"].obligations_at_risk == ["OB-4"]
    assert by["BLK-RETENTION"].state == "absent" and by["BLK-RETENTION"].base_priority == 0.6
    assert by["BLK-BREACH-NOTICE"].state == "absent" and "No actual was supplied" in by["BLK-BREACH-NOTICE"].explanation
    s = diff.summary
    assert (s.items, s.present, s.partial, s.planned, s.absent, s.unassessed, s.gaps) == (5, 1, 1, 1, 2, 1, 4)
    assert s.coverage_ratio == 0.2 and s.obligations_total == 6 and s.obligations_at_risk == 4
    assert diff.unknown_blocks == ["BLK-NOT-IN-BLUEPRINT"]
    dumped = json.dumps(diff.model_dump())
    for secret in (ORG, ORG_EMAIL, ORG_DOMAIN, "Head of Compliance"):
        assert secret not in dumped  # the diff speaks about blocks and obligations, never about the organization
    # the overlay hash pins what was assessed, not who said it
    quiet = _overlay()
    for a in quiet.actuals:
        a.owner = a.notes = None
        a.evidence_refs = []
    assert ic.overlay_hash(quiet) == diff.overlay_hash
    changed = _overlay()
    changed.actuals[3].state = "present"
    assert ic.overlay_hash(changed) != diff.overlay_hash


def test_instance_priorities_follow_the_published_formula_and_carry_factors():
    base = {"ITM-1": {"composite": 0.9, "band": "high"}, "BLK-RECON": {"composite": 0.4, "band": "medium"}, "ITM-4": {"composite": 0.6, "band": "medium"}}
    pri = ic.instance_priorities(ic.gap_diff(_composition(), _overlay(), base))
    by = {p.block_id: p for p in pri.items}
    assert "BLK-CDD" not in by  # present items are not priorities
    # absent + selected + scored 0.6 + load-bearing for one obligation
    assert by["BLK-RETENTION"].instance_priority == round(min(1.0, 0.6 * 1.0 * 1.0 * 1.05), 4)
    # partial (×0.5) + selected + scored 0.4
    assert by["BLK-RECON"].instance_priority == 0.2 and by["BLK-RECON"].factors["gap_weight"] == 0.5
    # planned (×0.25) + required (×1.25) + unscored → base assumed 0.5, load-bearing for one
    conf = by["BLK-CONFLICTS"]
    assert conf.factors["base_assumed"] is True and conf.instance_priority == round(0.5 * 0.25 * 1.25 * 1.05, 4)
    # unassessed absent, selected, unscored: 0.5 × 1 × 1 × 1
    assert by["BLK-BREACH-NOTICE"].instance_priority == 0.5
    assert [p.rank for p in pri.items] == list(range(1, len(pri.items) + 1))
    assert pri.items[0].block_id == "BLK-RETENTION" and pri.items[-1].block_id == "BLK-CONFLICTS"
    assert pri.method_version == ic.METHOD_VERSION and "gap_weight" in pri.method and pri.overlay_hash


def test_overlay_rejects_duplicate_blocks_and_unknown_states():
    with pytest.raises(ValueError, match="duplicate"):
        ic.ActualOverlay.model_validate({"actuals": [{"block_id": "B", "state": "present"}, {"block_id": "B", "state": "absent"}]})
    with pytest.raises(ValueError):
        ic.ActualOverlay.model_validate({"actuals": [{"block_id": "B", "state": "sort_of"}]})


# --------------------------------------------------------------------------- I5 acceptance: zero identifiers after a session


def _allow_instance(monkeypatch, account="123456789012"):
    monkeypatch.setenv("CLHEAR_MODE", "instance")
    monkeypatch.setenv("CLHEAR_INSTANCE_ACCOUNT_ID", account)
    from app.clhear.settings import get_settings

    get_settings.cache_clear()
    real = ic.deployment_guard
    monkeypatch.setattr(ic, "deployment_guard", lambda **kw: real(account_resolver=kw.get("account_resolver") or (lambda: account)))


def test_agnostic_store_has_zero_identifiers_after_an_instance_session(engine, tmp_path, client, monkeypatch):
    _corpus(engine, tmp_path)
    open_bp = composer.compose(engine, {"attributes": BROKER}, requested_by="test")  # the agnostic blueprint, stored
    assert open_bp["items"]
    before = ic._store_fingerprint(engine)
    _allow_instance(monkeypatch)
    assert client.get("/instance/guard").json()["allowed"] is True

    blocks = [it["block_id"] for it in open_bp["items"]]
    states = ["present", "partial", "absent", "planned"]
    overlay = {"blueprint_id": open_bp["blueprint_id"], "release": "2026.09.13", "as_of": "2026-09-12", "actuals": [
        {"block_id": b, "state": states[i % 4], "owner": f"{ORG} – Compliance", "notes": f"call {ORG_EMAIL} or {ORG_PHONE}; settle via {ORG_IBAN}",
         "evidence_refs": [f"s3://{ORG_DOMAIN}-evidence/{b}.pdf"], "characteristics_met": []} for i, b in enumerate(blocks)]}
    diff = client.post("/instance/overlay/diff", json=overlay)
    assert diff.status_code == 200, diff.text
    pri = client.post("/instance/overlay/priorities", json=overlay)
    assert pri.status_code == 200, pri.text
    d, p = diff.json(), pri.json()
    assert d["summary"]["items"] == len(blocks) and d["blueprint_id"] == open_bp["blueprint_id"]
    assert len(p["items"]) == sum(1 for g in d["items"] if g["gap"]) > 0
    # profile-based (not stored) overlay works too and returns no blueprint id
    onfly = client.post("/instance/overlay/diff", json={**overlay, "blueprint_id": None, "profile": {"attributes": BROKER}})
    assert onfly.status_code == 200 and onfly.json()["blueprint_id"] is None and onfly.json()["summary"]["items"] == len(blocks)

    # 1. the responses carry no organization identifier
    for body in (diff.text, pri.text, onfly.text):
        for secret in (ORG, ORG_EMAIL, ORG_DOMAIN, ORG_IBAN, ORG_PHONE):
            assert secret not in body
    # 2. the store is byte-for-byte what it was, except the HTTP audit rows (path/method/status only)
    after = ic._store_fingerprint(engine)
    changed = {t for t in set(before) | set(after) if before.get(t) != after.get(t)}
    assert changed <= {"audit_log"}, changed
    with engine.connect() as conn:
        from app.clhear.platform.audit import audit_log

        rows = conn.execute(sa.select(audit_log.c.action, audit_log.c.resource, audit_log.c.detail).where(audit_log.c.resource.like("/instance/%"))).all()
    assert rows and all(r.action == "http.write" and set(json.loads(r.detail) if isinstance(r.detail, str) else r.detail) == {"method", "status", "duration_ms"} for r in rows)
    # 3. the acceptance criterion: the agnostic scan of the whole store (layers + L0 platform tables) finds zero identifiers
    report = agnostic_scan.scan_engine(engine, denylist=DENYLIST)
    assert report.clean, report.to_dict()
    assert report.tables_scanned > len(record.layer_tables())  # platform tables included
    # … and of everything written to disk during the session
    disk = agnostic_scan.scan_path(tmp_path, denylist=DENYLIST)
    assert disk.clean, disk.to_dict()


def test_session_raises_store_leak_when_anything_is_written(engine):
    with pytest.raises(ic.StoreLeak, match="blocks"):
        with ic.session(engine):
            with engine.begin() as other:  # a write on any other connection during the session
                record.write(other, blocks_t, {"id": "BLK-LEAK", "name": "leak", "description": "", "capability": "", "evidence_artifacts": [],
                                               "satisfies": [], "implements_controls": [], "status": "curated", "kind": "Document", "purpose": "p"},
                             why=record.WhyTrail(layer="L3", reasoning_summary="leak", agent_id="test"))
    # and the session's own connection is always rolled back
    with ic.session(engine) as conn:
        conn.execute(blocks_t.insert().values(id="BLK-ROLLED", name="r", description="", capability="", evidence_artifacts=[],
                                              satisfies=[], implements_controls=[], status="curated", kind="Document", purpose="p"))
    with engine.connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(blocks_t).where(blocks_t.c.id == "BLK-ROLLED")).scalar() == 0


# --------------------------------------------------------------------------- public node: refuses, never reads


def test_public_deployment_serves_the_contract_but_refuses_overlays_without_reading_them(client):
    c = client.get("/instance/contract").json()
    assert c["version"] == ic.CONTRACT_VERSION and set(c["shapes"]) == {"ActualOverlay", "GapDiff", "InstancePriorities"}
    assert "client" in c["runs_only_in"] and c["document"] == "docs/INSTANCE_MODE_CONTRACT.md"
    oa = client.get("/instance/contract/openapi.json").json()
    assert {"/instance/overlay/diff", "/instance/overlay/priorities", "/instance/contract", "/instance/guard"} <= set(oa["paths"])
    assert oa["paths"]["/instance/overlay/diff"]["post"]["requestBody"]["content"]["application/json"]["schema"]["title"] == "ActualOverlay"
    assert oa["x-clhear-contract"]["version"] == ic.CONTRACT_VERSION
    g = client.get("/instance/guard").json()
    assert g["mode"] == "agnostic" and g["allowed"] is False
    # a syntactically broken body still gets the 403 — the guard runs before the body is parsed
    r = client.post("/instance/overlay/diff", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "instance_only"
    r = client.post("/instance/overlay/priorities", json={"actuals": [{"block_id": "B", "state": "absent", "owner": ORG}]})
    assert r.status_code == 403 and ORG not in r.text


def test_deployment_guard_requires_mode_pin_and_matching_account(monkeypatch):
    from app.clhear.settings import get_settings

    monkeypatch.delenv("CLHEAR_INSTANCE_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("CLHEAR_MODE", "agnostic")
    get_settings.cache_clear()
    assert ic.deployment_guard(account_resolver=lambda: "1")["allowed"] is False
    monkeypatch.setenv("CLHEAR_MODE", "instance")
    get_settings.cache_clear()
    g = ic.deployment_guard(account_resolver=lambda: "1")
    assert g["allowed"] is False and "CLHEAR_INSTANCE_ACCOUNT_ID" in g["reason"]
    monkeypatch.setenv("CLHEAR_INSTANCE_ACCOUNT_ID", "123456789012")
    assert ic.deployment_guard(account_resolver=lambda: "999999999999")["allowed"] is False
    assert ic.deployment_guard(account_resolver=lambda: (_ for _ in ()).throw(RuntimeError("no creds")))["allowed"] is False
    assert ic.deployment_guard(account_resolver=lambda: "123456789012")["allowed"] is True
    get_settings.cache_clear()


def test_public_infra_never_enables_instance_mode_and_release_runs_the_scan():
    for tf in (ROOT / "infra").glob("*.tf"):
        text = tf.read_text()
        assert "CLHEAR_MODE" not in text and "CLHEAR_INSTANCE_ACCOUNT_ID" not in text, tf.name
    rel = (ROOT / ".github/workflows/release.yml").read_text()
    assert "app.clhear.platform.agnostic_scan" in rel
    doc = (ROOT / "docs/INSTANCE_MODE_CONTRACT.md").read_text()
    assert ic.CONTRACT_VERSION in doc and ic.METHOD_VERSION in doc and "StoreLeak" in doc
    for k, v in ic.GAP_WEIGHT.items():
        assert f"{k}: {v}" in doc  # the formula in the document is the formula in the code


def test_public_repo_ships_the_open_contract_not_the_closed_data(engine, tmp_path):
    from app.clhear import curated
    from app.clhear.platform import public_repo

    curated.seed(engine)
    repo = tmp_path / "repo"
    written = {p.relative_to(repo).as_posix() for p in public_repo.write_interop(engine, repo, "r1")}
    assert {"standard/instance-mode/openapi.json", "standard/instance-mode/contract.json", "standard/instance-mode/CONTRACT.md"} <= written
    oa = json.loads((repo / "standard/instance-mode/openapi.json").read_text())
    assert "/instance/overlay/diff" in oa["paths"] and oa["x-clhear-contract"]["version"] == ic.CONTRACT_VERSION
    shipped = agnostic_scan.scan_path(repo / "standard" / "instance-mode", denylist=DENYLIST)
    assert shipped.clean, shipped.to_dict()
