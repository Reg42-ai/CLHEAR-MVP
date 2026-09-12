"""Phase E/F: the /v1/blueprint endpoint + legal blocks + pages."""
from app.clhear.releases import publish_release
from tests.test_layers_stack import _seed_corpus

from app.clhear.l2.extract import run_extraction

AUTH = {"Authorization": "Bearer dev-os-key", "X-App-Id": "os-dev"}

PROFILE = {
    "attributes": {
        "jurisdictions": ["UK"],
        "authorisations": ["EMI"],
        "products": ["payments"],
        "customer_base": ["retail"],
        "data_footprint": "",
        "crypto_services": False,
        "financial_entity_dora": False,
    },
    "activities": ["ACT-ONBOARD-CUSTOMER", "ACT-MONITOR-TRANSACTIONS"],
}


def test_blueprint_requires_auth(client):
    assert client.post("/v1/blueprint", json=PROFILE).status_code == 401


def test_blueprint_tailored_to_profile(client, engine):
    _seed_corpus(engine)
    run_extraction(engine)
    publish_release(engine, release_id="clhear-v20260830")
    resp = client.post("/v1/blueprint", json=PROFILE, headers=AUTH)
    assert resp.status_code == 200, resp.text
    bp = resp.json()
    assert bp["obligations_triggered"] == 2
    assert bp["release"] == "clhear-v20260830"
    assert bp["engine_version"] == "composer-v1"
    assert bp["coverage_summary"]["total"] == 2
    assert bp["layer_status"]["L2"] == "derived"
    # Legal block travels with the blueprint, attribution per involved source.
    assert bp["legal"]["not_legal_advice"] is True
    attributions = {a["source_key"]: a for a in bp["legal"]["attributions"]}
    assert "uksi/2017/692" in attributions
    assert "Open Government Licence" in attributions["uksi/2017/692"]["name"]
    # A crypto-flagged profile pulls a different obligation set.
    other = dict(PROFILE, attributes={**PROFILE["attributes"], "jurisdictions": ["EU"]})
    other_bp = client.post("/v1/blueprint", json=other, headers=AUTH).json()
    assert other_bp["obligations_triggered"] == 0  # EU anchors not in this test corpus
    assert other_bp["unmapped_obligations"]["count"] == 0


def test_blueprint_validation_errors(client, engine):
    _seed_corpus(engine)
    assert client.post("/v1/blueprint", json={}, headers=AUTH).status_code == 422
    assert client.post("/v1/blueprint", json={"attributes": {"jurisdictions": ["UK"]}, "activities": "x"}, headers=AUTH).status_code == 422


def test_source_payloads_carry_attribution(client, engine):
    _seed_corpus(engine)
    detail = client.get("/api/clhear/sources/uksi/2017/692").json()
    assert detail["attribution"]["name"].startswith("Open Government Licence")
    restricted = client.get("/api/clhear/sources/iso/27001-2022").json()
    assert restricted["attribution"]["restricted"] is True


def test_legal_pages_and_meta(client):
    assert "DRAFT" in client.get("/disclaimer").text
    assert "Terms of Use" in client.get("/terms").text
    meta = client.get("/api/clhear/legal").json()
    assert meta["status"] == "draft-pending-counsel-review"
    assert "CC BY 4.0" in meta["contribution_license"]
