"""A published release carries the derived layers only; /v1 serves L2 to L8 and blueprints from it."""
import sqlalchemy as sa

from app.clhear import layer_service
from app.clhear.db import make_engine
from app.clhear.derived_models import blueprints
from app.clhear.l2.extract import run_extraction
from app.clhear.l4.validate import create_profile
from app.clhear.l6.composer import compose, compose_for_profile
from app.clhear.scope_release import _copy_derived
from tests.test_layers_stack import _seed_corpus


def _release(engine, tmp_path):
    _seed_corpus(engine)
    run_extraction(engine)
    profile = create_profile(engine, {"jurisdictions": ["UK"]}, name="Tenant Test", source="api", allow_invalid=True)
    stored = compose_for_profile(engine, profile["id"], requested_by="test")
    path = tmp_path / "release.db"
    _copy_derived(engine, path)
    return make_engine(f"sqlite:///{path}"), profile, stored


def test_a_release_serves_obligations_profiles_and_the_tenant_program(engine, tmp_path):
    release, profile, stored = _release(engine, tmp_path)
    with release.connect() as conn:
        assert not layer_service._has_table(conn, layer_service.sample_profiles_t)
    assert layer_service.layer_items(release, "L2")["total"] >= 1
    l4 = layer_service.layer_items(release, "L4")
    assert profile["id"] in [p["id"] for p in l4["profiles"]] and l4["sample_profiles"] == []
    programs = layer_service.layer_items(release, "L6")
    assert all(p["status"] == "current" for p in programs)
    program = next(p for p in programs if p["profile_id"] == profile["id"])
    assert program["id"] == f"PRG:{stored['blueprint_id']}" and program["name"] == "Program — Tenant Test"
    assert program["coverage_summary"] == stored["coverage_summary"] and len(program["items"]) == len(stored["items"])


def test_the_live_database_lists_tenant_programs_before_samples_with_their_lineage(engine, tmp_path):
    _, _, stored = _release(engine, tmp_path)
    programs = layer_service.layer_items(engine, "L6")
    assert programs[0]["status"] == "current" and any(p["status"] == "computed-sample" for p in programs[1:])
    node = layer_service.lineage(engine, "L6", f"PRG:{stored['blueprint_id']}")
    assert node["kind"] == "program" and node["meta"]["blueprint_id"] == stored["blueprint_id"]
    assert {child["layer"] for child in node["children"]} <= {"L3"}


def test_a_blueprint_request_stores_nothing_in_a_release(engine, tmp_path):
    release, _, _ = _release(engine, tmp_path)
    with release.connect() as conn:
        before = conn.execute(sa.select(sa.func.count()).select_from(blueprints)).scalar_one()
    result = compose(release, {"attributes": {"jurisdictions": ["UK"]}}, requested_by="galaxy", log_request=False)
    with release.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(blueprints)).scalar_one() == before
    assert "blueprint_id" not in result and result["coverage_summary"]["total"] >= 1
