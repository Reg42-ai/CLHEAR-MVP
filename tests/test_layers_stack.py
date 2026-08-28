"""The 8-layer stack: registry contracts, demo labeling, lineage to real
clauses, restricted-leak guard, and the Stack UI shell."""
import json
from pathlib import Path

import sqlalchemy as sa

from app.clhear.demo import DEMO_DIR, FILES, load_layer_items
from app.clhear.l1.models import change_events, clauses, source_families, source_versions, sources
from app.clhear.layers import LAYER_CATALOG, LAYER_ORDER

OPEN_PREFIXES = ("uksi/", "ukpga/", "celex/", "nist/", "usc/", "cfr/", "eur/", "irs/", "lists/", "fatf/", "wolfsberg/")
RESTRICTED_PREFIXES = ("iso/", "aicpa/", "pci/", "ifrs/")


def _seed_corpus(engine):
    """Minimal real-shaped L1 corpus: open MLRs clauses + a restricted ISO row."""
    with engine.begin() as conn:
        fam = conn.execute(source_families.insert().values(key="uk-mlr", name="UK MLRs", scope_charter={})).inserted_primary_key[0]
        mlr = conn.execute(
            sources.insert().values(
                family_id=fam, key="uksi/2017/692", name="The Money Laundering Regulations 2017",
                kind="regulation", license="open", short_name="MLRs 2017",
            )
        ).inserted_primary_key[0]
        iso = conn.execute(
            sources.insert().values(
                family_id=fam, key="iso/27001-2022", name="ISO/IEC 27001:2022",
                kind="standard", license="restricted", short_name="ISO 27001",
            )
        ).inserted_primary_key[0]
        mlr_v = conn.execute(
            source_versions.insert().values(
                source_id=mlr, version_label="consolidated:2026-06-30", version_kind="consolidated",
                content_hash="sha256:mlr", s3_uri="s3://reg42-clhear-datalake/public-ok/mlr.xml", status="in_force",
            )
        ).inserted_primary_key[0]
        iso_v = conn.execute(
            source_versions.insert().values(
                source_id=iso, version_label="edition:2022", version_kind="edition",
                content_hash="sha256:iso", s3_uri="s3://reg42-clhear-datalake/restricted/iso.pdf", status="in_force",
            )
        ).inserted_primary_key[0]
        conn.execute(
            clauses.insert().values(
                source_version_id=mlr_v, ref="regulation-27", path="part-3/regulation-27", ordering=27,
                text="A relevant person must apply customer due diligence measures when the person establishes a business relationship.",
                text_hash="h27", public_ok=True,
            )
        )
        conn.execute(
            clauses.insert().values(
                source_version_id=mlr_v, ref="regulation-28", path="part-3/regulation-28", ordering=28,
                text="Customer due diligence measures: identify and verify the customer, and assess the purpose of the relationship.",
                text_hash="h28", public_ok=True,
            )
        )
        # Restricted clause: text present in the raw table but public_ok=False —
        # the lineage resolver must never emit it.
        conn.execute(
            clauses.insert().values(
                source_version_id=iso_v, ref="clause-4.3", path="clause-4/clause-4.3", ordering=43,
                text="RESTRICTED VERBATIM TEXT THAT MUST NEVER LEAK", text_hash="hiso", public_ok=False,
            )
        )
        conn.execute(
            change_events.insert().values(
                source_id=mlr, kind="amended", old_version="consolidated:2026-01-01",
                new_version="consolidated:2026-06-30", clause_refs=["regulation-27"], diff_s3_uri="",
            )
        )


# ------------------------------------------------------------------ registry


def test_catalog_has_nine_layers_with_derivation_contracts():
    assert LAYER_ORDER == ("L0", "L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8")
    for code, meta in LAYER_CATALOG.items():
        assert meta["status"] in {"live", "demo", "locked"}
        derivation = meta["derivation"]
        assert derivation["method"], code
        assert isinstance(derivation["inputs"], list)
        assert derivation["gates"], code
        assert derivation["evidence"], code
    assert LAYER_CATALOG["L0"]["status"] == "live"
    assert LAYER_CATALOG["L1"]["status"] == "live"
    assert LAYER_CATALOG["L8"]["status"] == "locked"


def test_layers_index_api(client):
    body = client.get("/api/clhear/layers").json()
    layers = {l["layer"]: l for l in body["layers"]}
    assert len(layers) == 9
    assert layers["L1"]["counts"]["sources"] == 0  # empty corpus, still answers
    assert layers["L2"]["banner"]["data_status"] == "demo"
    assert "derivation" in layers["L7"]
    assert layers["L8"]["status"] == "locked"


def test_demo_layer_detail_is_labeled(client):
    body = client.get("/api/clhear/layers/l3").json()
    assert body["status"] == "demo"
    assert body["banner"]["data_status"] == "demo"
    assert body["items"]
    for item in body["items"]:
        assert item["derivation"]["status"] == "demo-authored"


# ------------------------------------------------------------------- lineage


def test_lineage_bottoms_out_in_real_clauses(client, engine):
    _seed_corpus(engine)
    body = client.get("/api/clhear/layers/l2/items/OBL-UK-AML-CDD/lineage").json()
    chain = body["lineage"]
    assert chain["layer"] == "L2"
    leaves = chain["children"]
    assert {leaf["layer"] for leaf in leaves} == {"L1"}
    reg27 = next(l for l in leaves if l["id"].endswith("regulation-27"))
    assert reg27["meta"]["resolved"] is True
    assert "customer due diligence" in reg27["meta"]["text"]
    assert reg27["meta"]["s3_uri"].startswith("s3://")
    assert reg27["meta"]["text_hash"] == "h27"
    assert reg27["meta"]["content_hash"] == "sha256:mlr"


def test_lineage_never_leaks_restricted_text(client, engine):
    _seed_corpus(engine)
    body = client.get("/api/clhear/layers/l2/items/OBL-ISO-ISMS-SCOPE/lineage").json()
    raw = json.dumps(body)
    assert "MUST NEVER LEAK" not in raw
    leaf = body["lineage"]["children"][0]
    assert leaf["meta"]["resolved"] is True
    assert leaf["meta"]["text"] is None
    assert leaf["meta"]["locked"] is True
    assert leaf["meta"]["text_hash"] == "hiso"  # the anchor is provable without the text


def test_deep_lineage_program_to_clauses(client, engine):
    _seed_corpus(engine)
    body = client.get("/api/clhear/layers/l6/items/PRG-UK-EMI-2026/lineage").json()

    def collect(node, acc):
        acc.append(node)
        for child in node.get("children", []):
            collect(child, acc)
        return acc

    nodes = collect(body["lineage"], [])
    layers_seen = {n["layer"] for n in nodes}
    assert {"L6", "L3", "L2", "L1"} <= layers_seen
    resolved = [n for n in nodes if n["kind"] == "clause" and n["meta"].get("resolved")]
    assert any("customer due diligence" in (n["meta"].get("text") or "") for n in resolved)


def test_risk_lineage_uses_live_churn(client, engine):
    _seed_corpus(engine)
    body = client.get("/api/clhear/layers/l7/items/RSK-UK-EMI-FINCRIME/lineage").json()
    chain = body["lineage"]
    live = next(n for n in chain["children"] if n["kind"] == "live_input")
    assert live["meta"]["computed_live"] is True
    assert live["meta"]["change_events"] == 1  # the seeded change event
    assert chain["meta"]["result"]["formula_version"] == "demo-v1"
    assert set(chain["meta"]["result"]["components"]) == {"coverage_deficit", "churn_pressure", "open_ratio"}


def test_unknown_item_404(client):
    assert client.get("/api/clhear/layers/l2/items/NOPE/lineage").status_code == 404
    assert client.get("/api/clhear/layers/l9").status_code == 404


# ------------------------------------------------------ restricted-leak guard


def test_demo_seeds_respect_restricted_discipline():
    """No demo seed may carry text for a restricted source, and every cited
    source key must be a known-prefix key so a typo can't smuggle text in."""
    for layer, filename in FILES.items():
        for item in json.loads((DEMO_DIR / filename).read_text()):
            refs = list(item.get("basis", [])) + list(item.get("implements_controls", []))
            for ref in refs:
                key = ref["source_key"]
                assert key.startswith(OPEN_PREFIXES + RESTRICTED_PREFIXES), f"unknown source prefix: {key}"
                if key.startswith(RESTRICTED_PREFIXES):
                    assert item.get("restricted") is True, f"{item['id']} cites {key} but is not marked restricted"
            if item.get("restricted"):
                assert item.get("summary") in (None, ""), f"{item['id']}: restricted items must not carry a summary"


def test_all_demo_items_declare_demo_derivation():
    for layer in ("L2", "L3", "L4", "L5", "L6", "L7"):
        for item in load_layer_items(layer):
            assert item["derivation"]["status"] == "demo-authored", (layer, item["id"])


def test_l8_is_locked_no_aggregates():
    for item in load_layer_items("L8"):
        assert item["locked"] is True
        assert "▓" in item["blurred_sample"]  # nothing real behind the blur


def test_demo_graph_referential_integrity():
    obligations = {i["id"] for i in load_layer_items("L2")}
    blocks = {i["id"] for i in load_layer_items("L3")}
    activities = {i["id"] for i in load_layer_items("L5")}
    programs = {i["id"] for i in load_layer_items("L6")}
    for block in load_layer_items("L3"):
        assert set(block["satisfies"]) <= obligations, block["id"]
    for act in load_layer_items("L5"):
        assert {t["obligation"] for t in act["triggers"]} <= obligations, act["id"]
    for profile in load_layer_items("L4"):
        assert set(profile["activities"]) <= activities, profile["id"]
    for program in load_layer_items("L6"):
        assert set(program["blocks"]) <= blocks, program["id"]
        assert {c["obligation"] for c in program["coverage"]} <= obligations, program["id"]
    for risk in load_layer_items("L7"):
        assert risk["program"] in programs, risk["id"]
        assert set(risk["obligations"]) <= obligations, risk["id"]


# ------------------------------------------------------------------ UI shell


def test_stack_ui_served_at_root(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "the compliance stack" in resp.text
    assert "no-cache" in resp.headers.get("cache-control", "")


def test_sources_explorer_still_served(client):
    resp = client.get("/sources")
    assert resp.status_code == 200
    assert "Sources Explorer" in resp.text
