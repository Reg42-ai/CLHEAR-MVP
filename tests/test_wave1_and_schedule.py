"""Wave-1 plan integrity + fleet schedule dictionary served to the UI."""
from app.clhear.l1.models import FLEET_SCHEDULES
from app.clhear.l1 import registry_etoro


def test_fleet_schedules_match_eventbridge_contract():
    # UI + EventBridge must stay in lockstep (infra/eventbridge.tf).
    assert set(FLEET_SCHEDULES) == {
        "uk_legislation", "eur_lex", "govinfo_us", "irs_gov", "catalog_watchers",
    }
    assert FLEET_SCHEDULES["uk_legislation"]["cadence"] == "daily"
    assert FLEET_SCHEDULES["eur_lex"]["cadence"] == "daily"
    assert FLEET_SCHEDULES["uk_legislation"]["cron"] == "cron(0 0 * * ? *)"
    assert FLEET_SCHEDULES["eur_lex"]["cron"] == "cron(0 0 * * ? *)"
    assert FLEET_SCHEDULES["govinfo_us"]["cadence"] == "daily"
    assert all(s["utc_time"] == "00:00" for s in FLEET_SCHEDULES.values())


def test_meta_serves_schedules(client):
    data = client.get("/api/clhear/meta").json()
    assert "schedules" in data
    assert data["schedules"]["eur_lex"]["utc_time"] == "00:00"


def test_wave1_plan_covers_class_a_only():
    entries = registry_etoro.wave1_entries()
    assert len(entries) >= 50
    assert all(e.get("fetch") for e in entries)
    assert all(e["adapter"] in {"eur_lex", "uk_legislation"} for e in entries)
    keys = {e["key"] for e in entries}
    assert "celex/32014L0065" in keys  # MiFID II
    assert "celex/32023R1114" in keys  # MiCA
    assert "ukpga/2000/8" in keys      # FSMA
    # image-only SI is reference-level, not Wave 1
    assert "uksi/1986/1711" not in keys
    # restricted / later waves stay off the plan
    assert "iso/27001-2022" not in keys
    assert "fca/handbook" not in keys


def test_wave1_adapters_instantiate_with_registry_meta():
    plan = registry_etoro.wave1_adapters("eur_lex")
    assert plan
    entry, adapter = next(p for p in plan if p[0]["key"] == "celex/32014L0065")
    meta = adapter.meta()
    assert meta.short_name == "MiFID II"
    assert meta.family_key == "eu-mifid"
    assert meta.source_key == "celex/32014L0065"
    uk = registry_etoro.wave1_adapters("uk_legislation")
    _, fsma = next(p for p in uk if p[0]["key"] == "ukpga/2000/8")
    assert fsma.meta().family_key == "uk-fca"
