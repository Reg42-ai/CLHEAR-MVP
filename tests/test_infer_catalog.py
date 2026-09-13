"""clhear-infer catalog: CLHEAR's private Infer router only ever sees clean models,
every task class has a route, the caps are CLHEAR's own, and the committed files are
exactly what task_classes.py renders (HLD v2 I6)."""
from pathlib import Path

import yaml

from app.clhear.platform import infer_catalog as ic
from app.clhear.platform import task_classes as tc

CATALOG = Path(__file__).resolve().parents[1] / "infra" / "infer-catalog"


def test_committed_catalog_is_the_render():
    rendered = ic.render_all(daily_usd_cap=250, monthly_usd_cap=2000)
    for name, body in rendered.items():
        assert (CATALOG / name).read_text(encoding="utf-8") == body, f"{name} stale: run scripts/render_infer_catalog.py"


def test_only_procurement_clean_models_exist_and_every_class_routes():
    models = yaml.safe_load((CATALOG / "models.yaml").read_text(encoding="utf-8"))
    for key, row in models["models"].items():
        assert row["id"] == key  # keyed by Bedrock id so Infer reports it back verbatim
        assert tc.is_procurement_clean(key), key
    enabled = {k for k, r in models["models"].items() if r.get("enabled", True)}
    assert enabled == set(ic.AVAILABLE)
    for tier, spec in models["tiers"].items():
        assert spec["champion"] in enabled, f"{tier} champion must be a model that answers"
        if tier.startswith("clhear-") and tier != "clhear-nonderivation":
            for m in [spec["champion"], *spec["fallbacks"]]:
                assert tc.is_procurement_clean(m)

    policy = yaml.safe_load((CATALOG / "policy.yaml").read_text(encoding="utf-8"))
    routed = {r["match"]["task_class"][0]: r["tier"] for r in policy["routes"] if r["match"]}
    assert set(routed) == set(tc.TASK_CLASSES)
    for t in tc.TASK_CLASS_LIST:
        assert routed[t.id] in models["tiers"]
        assert tuple(
            [models["tiers"][routed[t.id]]["champion"], *models["tiers"][routed[t.id]]["fallbacks"]]
        ) == t.ladder, t.id
    assert policy["routes"][-1]["match"] == {} and policy["routes"][-1]["tier"] == "clhear-derivation"


def test_caps_belong_to_clhear_alone():
    employees = yaml.safe_load((CATALOG / "employees.yaml").read_text(encoding="utf-8"))
    assert [e["id"] for e in employees["employees"]] == [ic.PRINCIPAL]
    assert employees["employees"][0]["daily_usd_cap"] == 250
    budget = yaml.safe_load((CATALOG / "budget.yaml").read_text(encoding="utf-8"))
    assert budget["company"]["hired_usd"] == 2000 and budget["company"]["name"] == "CLHEAR"
    tools = yaml.safe_load((CATALOG / "tools.yaml").read_text(encoding="utf-8"))
    assert tools["seats"] == {}
    dockerfile = (CATALOG / "Dockerfile").read_text(encoding="utf-8")
    assert "@sha256:" in dockerfile  # pinned by digest, never :latest
