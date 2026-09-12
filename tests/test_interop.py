"""Interop exports (HLD v2 §4.6 / §9): OSCAL round trip on a hand-built composition.

Pure unit tests — no store — so the export contract is pinned independently
of what the composer happens to produce for a given corpus.
"""
from __future__ import annotations

import json

from app.clhear.interop import oscal

COMPOSITION = {
    "engine_version": "composer-v2",
    "release": "2026.09",
    "profile_id": "PRF-000007",
    "profile_attributes": {"jurisdictions": ["UK"], "authorisations": ["Client money permission (CASS)"], "customer_base": ["retail"],
                           "data_footprint": "", "crypto_services": False},
    "items": [
        {"block_id": "BLK-CDD-PROGRAMME", "kind": "Process", "name": "Customer due diligence programme", "basis": "required",
         "load_bearing_for": ["OBL:uksi/2017/692#regulation-27"], "obligations_satisfied": ["OBL:uksi/2017/692#regulation-27"],
         "characteristics": [{"key": "refresh_cycle", "value": "annual", "status": "backed", "in_profile": True},
                             {"key": "screening_vendor", "value": "n/a", "status": "not_specified", "in_profile": False}],
         "activities_operated": [{"activity_id": "ACT-ONBOARD-CUSTOMER", "name": "Onboard a customer", "obligation_refs": []}],
         "explanation": "Customer due diligence programme (Process BLK-CDD-PROGRAMME) is required by OBL:uksi/2017/692#regulation-27."},
        {"block_id": "BLK-RECORDS-RETENTION", "kind": "Control", "name": "Records retention", "basis": "selected",
         "load_bearing_for": ["OBL:uksi/2017/692#regulation-40"], "obligations_satisfied": ["OBL:uksi/2017/692#regulation-40"],
         "characteristics": [], "activities_operated": [], "explanation": "Records retention (Control BLK-RECORDS-RETENTION) is selected."},
    ],
    "coverage": [
        {"obligation_id": "OBL:uksi/2017/692#regulation-27", "stable_id": "OBL-000027", "source_key": "uksi/2017/692", "clause_ref": "regulation-27",
         "title": "Customer due diligence", "state": "covered", "covered_by": ["BLK-CDD-PROGRAMME"], "satisfied_by": ["BLK-CDD-PROGRAMME"]},
        {"obligation_id": "OBL:uksi/2017/692#regulation-40", "stable_id": "OBL-000040", "source_key": "uksi/2017/692", "clause_ref": "regulation-40",
         "title": "Record keeping", "state": "covered", "covered_by": ["BLK-RECORDS-RETENTION"], "satisfied_by": ["BLK-RECORDS-RETENTION"]},
        {"obligation_id": "OBL:uksi/2017/692#regulation-33", "stable_id": "OBL-000033", "source_key": "uksi/2017/692", "clause_ref": "regulation-33",
         "title": "Enhanced due diligence", "state": "gap", "covered_by": [], "satisfied_by": []},
    ],
    "coverage_summary": {"covered": 2, "gaps": 1, "total": 3},
    "minimality": {"checked": True, "minimal": True, "proof": []},
    "composition_hash": "abc123",
}


def test_oscal_ssp_shape_and_round_trip():
    doc = oscal.blueprint_ssp(COMPOSITION, blueprint_id="BLU-000042")
    ssp = doc["system-security-plan"]
    assert ssp["metadata"]["oscal-version"] == oscal.OSCAL_VERSION and ssp["metadata"]["version"] == "2026.09"
    assert ssp["system-characteristics"]["system-ids"][0]["id"] == "BLU-000042"
    assert ssp["import-profile"]["href"] == "clhear://l4/profiles/PRF-000007"
    comps = ssp["system-implementation"]["components"]
    assert [c["title"] for c in comps] == ["Customer due diligence programme", "Records retention"]
    cdd = comps[0]
    props = {p["name"]: p for p in cdd["props"]}
    assert props["clhear-id"]["value"] == "BLK-CDD-PROGRAMME" and props["basis"]["value"] == "required"
    assert props["characteristic:refresh_cycle"]["class"] == "backed" and "characteristic:screening_vendor" not in props
    assert props["operated-by"]["value"] == "ACT-ONBOARD-CUSTOMER" and props["load-bearing"]["value"] == "true"
    reqs = {r["control-id"]: r for r in ssp["control-implementation"]["implemented-requirements"]}
    assert len(reqs) == 3
    status = {oid: next(p["value"] for p in r["props"] if p["name"] == "implementation-status") for oid, r in reqs.items()}
    assert status["OBL:uksi/2017/692#regulation-27"] == "implemented" and status["OBL:uksi/2017/692#regulation-33"] == "not-implemented"
    assert reqs["OBL:uksi/2017/692#regulation-27"]["by-components"][0]["component-uuid"] == cdd["uuid"]
    assert reqs["OBL:uksi/2017/692#regulation-33"]["by-components"] == []
    # import reproduces items, satisfied-by sets and the gap exactly
    ok, problems = oscal.round_trip_ok(COMPOSITION)
    assert ok, problems
    back = oscal.import_blueprint(doc)
    assert back["blueprint_id"] == "BLU-000042" and back["gaps"] == ["OBL:uksi/2017/692#regulation-33"]
    assert back["items"]["BLK-CDD-PROGRAMME"]["characteristics"] == {"refresh_cycle": "annual"}
    assert back["coverage"]["OBL:uksi/2017/692#regulation-27"] == ["BLK-CDD-PROGRAMME"]
    assert back["composition_hash"] == "abc123" and back["minimal"] is True


def test_oscal_export_is_deterministic_and_uuids_are_stable():
    a = json.dumps(oscal.blueprint_ssp(COMPOSITION, blueprint_id="BLU-000042"), sort_keys=True)
    b = json.dumps(oscal.blueprint_ssp(COMPOSITION, blueprint_id="BLU-000042"), sort_keys=True)
    assert a == b
    other = oscal.blueprint_ssp(COMPOSITION, blueprint_id="BLU-000043")["system-security-plan"]
    assert other["uuid"] != json.loads(a)["system-security-plan"]["uuid"]
    # same item in two blueprints -> different component uuids (uuid5 over blueprint + block)
    assert other["system-implementation"]["components"][0]["uuid"] != json.loads(a)["system-security-plan"]["system-implementation"]["components"][0]["uuid"]


def test_round_trip_detects_a_tampered_export():
    doc = oscal.blueprint_ssp(COMPOSITION, blueprint_id="BLU-000042")
    ssp = doc["system-security-plan"]
    ssp["control-implementation"]["implemented-requirements"][0]["by-components"] = []
    back = oscal.import_blueprint(doc)
    assert back["coverage"]["OBL:uksi/2017/692#regulation-27"] == []
    # a composition whose coverage claims a block that is not an item is not round-trip clean
    broken = {**COMPOSITION, "items": COMPOSITION["items"][:1]}
    ok, problems = oscal.round_trip_ok(broken)
    assert not ok and any("coverage differs for OBL:uksi/2017/692#regulation-40" in p for p in problems)
