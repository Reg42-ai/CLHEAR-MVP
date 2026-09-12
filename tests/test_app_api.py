"""Consumer /v1 API: L1 live, L2–L7 demo-labeled, L8 locked (501), app-key auth."""
from app.clhear.releases import publish_release
from app.clhear.workers import handle_envelope
from app.clhear.platform.gateway import FakeProvider, Gateway


AUTH = {"Authorization": "Bearer dev-os-key", "X-App-Id": "os-dev"}


def test_app_api_rejects_missing_key(client):
    assert client.get("/v1/releases/latest").status_code == 401


def test_latest_release_synthesizes_live(client):
    r = client.get("/v1/releases/latest", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "L1" in body["layers"]
    assert "L0" in body["layers"]
    assert body["reserved_layers"] == ["L2", "L3", "L4", "L5", "L6", "L7", "L8"]
    assert "l1" in body


def test_l1_resources_and_reserved_layers(client, engine):
    publish_release(engine, release_id="clhear-v20260826")
    fam = client.get("/v1/releases/clhear-v20260826/l1/families", headers=AUTH)
    assert fam.status_code == 200
    assert fam.json()["layer"] == "L1"
    src = client.get("/v1/releases/clhear-v20260826/l1/sources", headers=AUTH)
    assert src.status_code == 200
    clauses = client.get("/v1/releases/clhear-v20260826/l1/clauses", headers=AUTH)
    assert clauses.status_code == 200
    assert "clauses" in clauses.json()
    snap = client.get("/v1/releases/clhear-v20260826/l1/snapshot", headers=AUTH)
    assert snap.status_code == 200
    assert "url" in snap.json()

    # Preview layers answer 200 but are unmistakably labeled — clients must
    # branch on layer_status; every preview payload carries the honesty banner.
    reserved = client.get("/v1/releases/clhear-v20260826/l2/obligations", headers=AUTH)
    assert reserved.status_code == 200
    assert reserved.json()["layer_status"] == "derived"
    assert reserved.json()["banner"]["data_status"] == "derived"
    assert "registry" in reserved.json()

    curated_resp = client.get("/v1/releases/clhear-v20260826/l3/building-blocks", headers=AUTH)
    assert curated_resp.status_code == 200
    assert curated_resp.json()["layer_status"] == "curated"

    # Wrong resource name for the layer still feature-detects as not_published.
    wrong = client.get("/v1/releases/clhear-v20260826/l2/profiles", headers=AUTH)
    assert wrong.status_code == 501
    assert wrong.json()["detail"]["layer_status"] == "not_published"

    # L8 is locked by design: its data endpoint never opens.
    l8 = client.get("/v1/releases/latest/l8/benchmarks", headers=AUTH)
    assert l8.status_code == 501
    assert l8.json()["detail"]["layer"] == "L8"


def test_pin_release(client, engine):
    publish_release(engine, release_id="clhear-v20260921")
    r = client.post("/v1/releases/clhear-v20260921/pin", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["id"] == "clhear-v20260921"
    assert r.json()["pinned"] is True


def test_unknown_layer_kind_is_ignored(engine):
    gateway = Gateway(engine, FakeProvider())
    body = (
        '{"event_id":"e-unknown-layer","layer":"l3","kind":"BuildingBlockChanged",'
        '"subject_ref":"x","payload":{},"schema_version":1,"producer":"test","ts":"2026-08-26T00:00:00Z"}'
    )
    out = handle_envelope(engine, gateway, body)
    assert out["ignored"] is True
