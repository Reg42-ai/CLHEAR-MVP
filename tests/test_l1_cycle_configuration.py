"""Configuration changes cannot mix verification evidence under one image."""
import sqlalchemy as sa

from app.clhear.l1 import cycles, pipeline, translation
from app.clhear.settings import get_settings
from tests.test_l1_cycles import envelope
from tests.test_l1_pipeline import _StubAdapter, _tree


def test_parser_setting_change_retires_cycle_without_image_change(engine, monkeypatch):
    monkeypatch.setenv("CLHEAR_CODE_REVISION", "a" * 40)
    monkeypatch.setenv("CLHEAR_WORKER_IMAGE_DIGEST", "sha256:" + "1" * 64)
    settings = get_settings()
    cycle_id = cycles.start(engine, envelope("L1CycleRequested", {"cycle_id": "cycle-manual-config-fixture"}))["cycle_id"]
    prior = cycles.runtime_identity()
    monkeypatch.setattr(settings, "clhear_salvage_cap", settings.clhear_salvage_cap / 2)
    changed = cycles.runtime_identity()
    assert changed["worker_image_digest"] == prior["worker_image_digest"]
    assert changed["code_revision"] == prior["code_revision"]
    assert changed["parser_configuration_digest"] != prior["parser_configuration_digest"]
    cycles.reconcile(engine, admit=False)
    row = cycles.cycle_summary(engine, cycle_id)["cycles"][0]
    assert row["status"] == "failed"
    assert row["parser_configuration_digest"] == prior["parser_configuration_digest"]
    assert row["result"]["accepted_release"] is False


def test_english_policy_change_changes_cycle_binding(monkeypatch):
    prior = cycles.runtime_identity()["parser_configuration_digest"]
    monkeypatch.setattr(translation, "POLICY_VERSION", "test-only-other-policy")
    assert cycles.runtime_identity()["parser_configuration_digest"] != prior


def test_cycle_identity_uses_effective_http_mode(monkeypatch):
    monkeypatch.delenv("CLHEAR_HTTP_MODE", raising=False)
    prior = cycles.runtime_identity()["parser_configuration_digest"]
    monkeypatch.setenv("CLHEAR_HTTP_MODE", "replay")
    assert cycles.runtime_identity()["parser_configuration_digest"] == prior
    monkeypatch.setenv("CLHEAR_HTTP_MODE", "live")
    assert cycles.runtime_identity()["parser_configuration_digest"] != prior


def test_parser_identity_includes_runtime_fidelity_controls(monkeypatch):
    adapter = _StubAdapter("v1", _tree(("r1", "Authored configuration fixture.")))
    prior = pipeline.parser_identity(adapter)
    settings = get_settings()
    monkeypatch.setattr(settings, "clhear_fidelity_threshold", 0.999)
    assert pipeline.parser_identity(adapter)["configuration_sha256"] != prior["configuration_sha256"]


def test_old_snapshot_cycle_read_reports_missing_configuration(engine):
    cycles.start(engine, envelope("L1CycleRequested", {"cycle_id": "cycle-manual-legacy-config-fixture"}))
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE l1_cycles DROP COLUMN parser_configuration_digest")
    row = cycles.cycle_summary(engine, "cycle-manual-legacy-config-fixture")["cycles"][0]
    assert row["parser_configuration_digest"] is None
    # The additive migration can be reapplied to an older populated table.
    from migrations.m0033_l1_cycle_configuration import upgrade
    with engine.begin() as conn:
        upgrade(conn)
        upgrade(conn)
        assert conn.execute(sa.select(cycles.cycles.c.cycle_id)).scalar_one() == "cycle-manual-legacy-config-fixture"
