"""Consumer API: release-bound L1, explicit candidates and downstream previews."""
from datetime import timedelta

import pytest
import sqlalchemy as sa

from app.clhear import releases
from app.clhear.l1 import permissions, pipeline
from app.clhear.l1.models import sources
from app.clhear.models import runs
from tests.test_l1_synthetic_amendment import SyntheticAdapter, V1, V2

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
    assert "L1" not in body["layers"]
    assert "L0" in body["layers"]
    assert body["reserved_layers"] == ["L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8"]
    assert "l1" in body


def test_l1_resources_and_reserved_layers(client, engine):
    publish_release(engine, release_id="clhear-v20260826")
    for resource in ("families", "sources", "clauses", "snapshot", "change-events"):
        response = client.get(f"/v1/releases/clhear-v20260826/l1/{resource}", headers=AUTH)
        assert response.status_code == 409
    status = client.get("/v1/releases/clhear-v20260826/l1/status", headers=AUTH).json()
    assert status["published"] is False
    assert status["layer_status"] == "not_published"

    # Preview layers answer 200 but are unmistakably labeled — clients must
    # branch on layer_status; every preview payload carries the honesty banner.
    reserved = client.get("/v1/releases/clhear-v20260826/l2/obligations", headers=AUTH)
    assert reserved.status_code == 200
    assert reserved.json()["layer_status"] == "derived"
    assert reserved.json()["banner"]["data_status"] == "derived"
    assert "registry" in reserved.json()

    derived_l3 = client.get("/v1/releases/clhear-v20260826/l3/building-blocks", headers=AUTH)
    assert derived_l3.status_code == 200
    assert derived_l3.json()["layer_status"] == "derived"  # HLD v2 §4.3: blocks are derived from L2

    derived_l4 = client.get("/v1/releases/clhear-v20260826/l4/profiles", headers=AUTH)
    assert derived_l4.status_code == 200
    assert derived_l4.json()["layer_status"] == "derived"  # HLD v2 §4.4: register-backed ontology + validated profiles

    derived_l5 = client.get("/v1/releases/clhear-v20260826/l5/activities", headers=AUTH)
    assert derived_l5.status_code == 200
    assert derived_l5.json()["layer_status"] == "derived"  # HLD v2 §4.5: junction derived from L2-L4

    # Wrong resource name for the layer still feature-detects as not_published.
    wrong = client.get("/v1/releases/clhear-v20260826/l2/profiles", headers=AUTH)
    assert wrong.status_code == 501
    assert wrong.json()["detail"]["layer_status"] == "not_published"

    # L8 opens as a labeled reference benchmark; peer aggregates stay locked until k is met.
    l8 = client.get("/v1/releases/latest/l8/benchmarks", headers=AUTH)
    assert l8.status_code == 200
    assert l8.json()["layer"] == "L8" and l8.json()["layer_status"] == "reference"
    assert "not peer data" in l8.json()["view"] and l8.json()["peer_aggregates"]["status"] == "locked"


def test_pin_release(client, engine):
    publish_release(engine, release_id="clhear-v20260921")
    r = client.post("/v1/releases/clhear-v20260921/pin", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["id"] == "clhear-v20260921"
    assert r.json()["pinned"] is True


def test_unknown_layer_kind_is_retained(engine):
    gateway = Gateway(engine, FakeProvider())
    body = (
        '{"event_id":"e-unknown-layer","layer":"l3","kind":"BuildingBlockChanged",'
        '"subject_ref":"x","payload":{},"schema_version":1,"producer":"test","ts":"2026-08-26T00:00:00Z"}'
    )
    from app.clhear.workers import WrongFleet
    with pytest.raises(WrongFleet):
        handle_envelope(engine, gateway, body)
    with engine.connect() as conn:
        assert not conn.execute(sa.select(runs.c.id).where(runs.c.fleet == "worker")).first()


def _candidate(engine, tmp_path, monkeypatch):
    """Only fixture records; no live source or license acquisition."""
    result = pipeline.ingest(engine, SyntheticAdapter(V1, "2026-01-01"),
                             pipeline.LocalStore(tmp_path / "lake"), index_embeddings=False)
    manifest = {
        "id": "clhear-v20260915-fixture", "layers": ["L0", "L1"], "status": "candidate",
        "audience": "restricted-reviewers", "acceptance": {"passed": True},
        "projection": {"bindings": [{"source_key": "synthetic/prin", "source_version_id": result["source_version_id"],
                                     "content_hash": result["content_hash"]}]},
        "l1": {"snapshot_uri": "s3://test-private/fixture.db"}, "manifest_hash": "test-only-manifest",
    }
    original = releases.get_release
    monkeypatch.setattr(releases, "get_release", lambda release_id, engine=None: manifest if release_id == manifest["id"] else original(release_id, engine=engine))
    monkeypatch.setattr(releases, "get_promotion", lambda release_id: None)
    return manifest


def test_candidate_clauses_are_version_pinned_after_new_ingest(client, engine, tmp_path, monkeypatch):
    manifest = _candidate(engine, tmp_path, monkeypatch)
    base = f"/v1/releases/{manifest['id']}/l1"
    before = client.get(base + "/clauses", headers=AUTH)
    assert before.status_code == 200, before.text
    assert before.json()["clauses"]
    assert before.json()["layer_status"] == "candidate"
    assert before.json()["published"] is False
    pipeline.ingest(engine, SyntheticAdapter(V2, "2026-06-01"), pipeline.LocalStore(tmp_path / "lake"), index_embeddings=False)
    after = client.get(base + "/clauses", headers=AUTH)
    assert after.status_code == 200, after.text
    assert after.json()["clauses"] == before.json()["clauses"]
    assert {row["source_version_id"] for row in after.json()["clauses"]} == {manifest["projection"]["bindings"][0]["source_version_id"]}
    assert client.get(base + "/families", headers=AUTH).status_code == 409
    assert client.get(base + "/sources", headers=AUTH).status_code == 409
    events = client.get(base + "/change-events", headers=AUTH).json()["events"]
    assert len(events) == 1
    assert "2026-01-01" in events[0]["new_version"]
    changelog = client.get(f"/v1/releases/{manifest['id']}/changelog", headers=AUTH).json()["events"]
    assert changelog == events


def test_missing_or_changed_frozen_binding_never_falls_back_to_current(client, engine, tmp_path, monkeypatch):
    manifest = _candidate(engine, tmp_path, monkeypatch)
    manifest["projection"]["bindings"][0]["content_hash"] = "wrong-hash"
    assert client.get(f"/v1/releases/{manifest['id']}/l1/clauses", headers=AUTH).status_code == 409
    manifest["projection"]["bindings"] = []
    assert client.get(f"/v1/releases/{manifest['id']}/l1/clauses", headers=AUTH).status_code == 409


def test_promotion_receipt_changes_candidate_status_only(client, engine, tmp_path, monkeypatch):
    manifest = _candidate(engine, tmp_path, monkeypatch)
    url = f"/v1/releases/{manifest['id']}/l1/status"
    assert client.get(url, headers=AUTH).json()["layer_status"] == "candidate"
    monkeypatch.setattr(releases, "get_promotion", lambda release_id: {"verified_signature": True, "manifest_hash": manifest["manifest_hash"]})
    published = client.get(url, headers=AUTH).json()
    assert published["layer_status"] == "published"
    assert published["published"] is True
    manifest["layers"] = ["L0"]
    assert client.get(url, headers=AUTH).json()["layer_status"] == "not_published"


def test_private_snapshot_is_not_presigned_for_app_key(client, engine, tmp_path, monkeypatch):
    import boto3
    manifest = _candidate(engine, tmp_path, monkeypatch)
    def no_client(*args, **kwargs):
        raise AssertionError("Private artifact must not reach presigning")
    monkeypatch.setattr(boto3, "client", no_client)
    response = client.get(f"/v1/releases/{manifest['id']}/l1/snapshot", headers=AUTH)
    assert response.status_code == 403


def test_public_read_helpers_recheck_revoked_permissions_and_hide_counts(client, engine, tmp_path, monkeypatch):
    from app.clhear.l1.public import clauses_public_select, nodes_public_select
    from app.clhear.l1.workflow import utcnow
    manifest = _candidate(engine, tmp_path, monkeypatch)
    key = "finra/rule/test-public"
    with engine.begin() as conn:
        conn.execute(sources.update().where(sources.c.key == "synthetic/prin").values(key=key))
    manifest["projection"]["bindings"][0]["source_key"] = key
    url = f"/v1/releases/{manifest['id']}/l1/clauses"
    assert client.get(url, headers=AUTH).json()["total"] == 0
    def grant(approved=True, **kwargs):
        return permissions.record_permission(engine, source_key=key, permissions={"display_public": True},
            approved=approved, evidence_ref="test-only-rights-evidence", approved_by="test-reviewer", **kwargs)
    grant()
    assert client.get(url, headers=AUTH).json()["total"] > 0
    with engine.connect() as conn:
        assert conn.execute(nodes_public_select(conn)).first()
    grant(approved=False)
    hidden = client.get(url, headers=AUTH).json()
    assert hidden["total"] == 0 and hidden["clauses"] == []
    with engine.connect() as conn:
        assert not conn.execute(clauses_public_select(conn)).first()
        assert not conn.execute(nodes_public_select(conn)).first()
    grant(valid_from=utcnow() - timedelta(hours=2), expires_at=utcnow() - timedelta(hours=1))
    assert client.get(url, headers=AUTH).json()["total"] == 0


def test_family_source_lists_match_selected_bindings(client, engine, tmp_path, monkeypatch):
    manifest = _candidate(engine, tmp_path, monkeypatch)
    base = f"/v1/releases/{manifest['id']}/l1"
    response = client.get(base + "/sources", headers=AUTH)
    assert response.status_code == 200, response.text
    assert {m["key"] for family in response.json()["sources"] for m in family["members"]} == {"synthetic/prin"}
    assert client.get(base + "/families", headers=AUTH).status_code == 200
