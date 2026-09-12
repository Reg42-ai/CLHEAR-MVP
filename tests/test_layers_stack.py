"""The 8-layer stack over REAL data: extraction, composer, lineage,
restricted discipline, honesty labeling, eval gates, UI shells."""
import json

import sqlalchemy as sa

from app.clhear import curated
from app.clhear.derived_models import obligations
from app.clhear.l1.models import change_events, clauses, family_members, source_families, source_versions, sources
from app.clhear.l2.extract import run_extraction
from app.clhear.l6.composer import compose, when_matches
from app.clhear.layers import LAYER_CATALOG, LAYER_ORDER
from app.clhear.platform.evals import run_suite

DUTY_27 = (
    "A relevant person must apply customer due diligence measures if the person "
    "establishes a business relationship or carries out an occasional transaction "
    "that amounts to a transfer of funds exceeding 1,000 euros."
)
DUTY_28 = (
    "This regulation applies when a relevant person is required by regulation 27 to "
    "apply customer due diligence measures. The relevant person must identify the "
    "customer and verify the customer's identity on the basis of a reliable source."
)
NON_DUTY_1 = (
    "Citation and commencement. These Regulations may be cited as the Money "
    "Laundering Regulations 2017 and come into force on 26th June 2017 for the "
    "purposes of regulations made under them."
)


def _seed_corpus(engine):
    with engine.begin() as conn:
        fam = conn.execute(source_families.insert().values(key="uk-mlr", name="UK MLRs", scope_charter={})).inserted_primary_key[0]
        mlr = conn.execute(
            sources.insert().values(
                family_id=fam, key="uksi/2017/692", name="The Money Laundering Regulations 2017",
                kind="regulation", license="open", short_name="MLRs 2017", jurisdiction="UK",
                topics=["financial-crime"],
            )
        ).inserted_primary_key[0]
        iso = conn.execute(
            sources.insert().values(
                family_id=fam, key="iso/27001-2022", name="ISO/IEC 27001:2022",
                kind="standard", license="restricted", short_name="ISO 27001", jurisdiction="International",
            )
        ).inserted_primary_key[0]
        for sid, relation in ((mlr, "root"), (iso, "supplements")):
            conn.execute(
                family_members.insert().values(
                    family_id=fam, source_id=sid, relation=relation, tier="binding", status="active", added_via="manual"
                )
            )
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
        for ref, ordering, text, h in (
            ("regulation-1", 1, NON_DUTY_1, "h1"),
            ("regulation-27", 27, DUTY_27, "h27"),
            ("regulation-28", 28, DUTY_28, "h28"),
        ):
            conn.execute(
                clauses.insert().values(
                    source_version_id=mlr_v, ref=ref, path=f"part/{ref}", ordering=ordering,
                    text=text, text_hash=h, public_ok=True,
                )
            )
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
    curated.seed(engine)


# ------------------------------------------------------------------ registry


def test_catalog_statuses_and_contracts():
    assert LAYER_ORDER == ("L0", "L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8")
    expected = {"L0": "live", "L1": "live", "L2": "derived", "L3": "curated", "L4": "curated",
                "L5": "curated", "L6": "computed", "L7": "computed", "L8": "locked"}
    for code, status in expected.items():
        assert LAYER_CATALOG[code]["status"] == status
        d = LAYER_CATALOG[code]["derivation"]
        assert d["method"] and d["gates"] and d["evidence"]
        assert d.get("generation") and d["generation"].get("guarantee")


def test_layers_index_api(client):
    layers = {l["layer"]: l for l in client.get("/api/clhear/layers").json()["layers"]}
    assert len(layers) == 9
    assert layers["L2"]["banner"]["data_status"] == "derived"
    assert layers["L3"]["banner"]["data_status"] == "curated"
    assert layers["L6"]["banner"]["data_status"] == "computed"
    assert layers["L8"]["status"] == "locked"


# ---------------------------------------------------------------- extraction


def test_extraction_derives_duty_clauses_only(client, engine):
    _seed_corpus(engine)
    summary = run_extraction(engine)
    assert summary["inserted"] >= 2
    with engine.connect() as conn:
        rows = {r.id: r for r in conn.execute(sa.select(obligations))}
    assert "OBL:uksi/2017/692#regulation-27" in rows
    assert "OBL:uksi/2017/692#regulation-28" in rows
    assert "OBL:uksi/2017/692#regulation-1" not in rows  # citation clause: no duty
    assert all(oid.split("#")[0] != "OBL:iso/27001-2022" for oid in rows)  # restricted: never derived
    ob = rows["OBL:uksi/2017/692#regulation-27"]
    assert ob.status == "derived" and ob.modality == "must" and ob.text_hash == "h27"


def test_extraction_is_idempotent_and_rederives_on_change(client, engine):
    _seed_corpus(engine)
    run_extraction(engine)
    second = run_extraction(engine)
    assert second["inserted"] == 0 and second["re_derived"] == 0 and second["unchanged"] >= 2
    # Simulate maintainer validation, then an L1 change on the basis clause.
    with engine.begin() as conn:
        conn.execute(
            obligations.update()
            .where(obligations.c.id == "OBL:uksi/2017/692#regulation-27")
            .values(status="validated", validated_by="avner@reg42.ai")
        )
        conn.execute(clauses.update().where(clauses.c.ref == "regulation-27").values(text_hash="h27-changed"))
    third = run_extraction(engine)
    assert third["re_derived"] == 1
    with engine.connect() as conn:
        ob = conn.execute(sa.select(obligations).where(obligations.c.id == "OBL:uksi/2017/692#regulation-27")).one()
    assert ob.status == "derived" and ob.validated_by is None  # validation does not survive a changed basis


# ------------------------------------------------------------------ composer


def test_when_matches_semantics():
    attrs = {"jurisdictions": ["UK"], "crypto_services": False, "data_footprint": "large-scale personal data"}
    assert when_matches({"jurisdictions": "UK"}, attrs)
    assert not when_matches({"jurisdictions": "EU"}, attrs)
    assert when_matches({"data_footprint": "*"}, attrs)
    assert not when_matches({"crypto_services": True}, attrs)
    assert when_matches({}, attrs)


def test_composer_covers_and_surfaces_gaps(client, engine):
    _seed_corpus(engine)
    run_extraction(engine)
    profile = {"attributes": {"jurisdictions": ["UK"], "data_footprint": ""},
               "activities": ["ACT-ONBOARD-CUSTOMER", "ACT-MONITOR-TRANSACTIONS"]}
    bp = compose(engine, profile, requested_by="test")
    assert bp["obligations_triggered"] == 2  # reg-27 + reg-28
    states = {c["obligation_id"]: c["state"] for c in bp["coverage"]}
    assert states["OBL:uksi/2017/692#regulation-27"] == "covered"  # BLK-CDD-PROGRAMME
    assert any(b["id"] == "BLK-CDD-PROGRAMME" for b in bp["blocks"])
    assert "blueprint_id" in bp

    # Determinism: same inputs => same output (minus the request log id).
    bp2 = compose(engine, profile, requested_by="test", log_request=False)
    a = {k: v for k, v in bp.items() if k != "blueprint_id"}
    assert a == bp2


def test_composer_gap_when_no_block(client, engine):
    _seed_corpus(engine)
    # Add a duty clause no curated block satisfies.
    with engine.begin() as conn:
        version_id = conn.execute(sa.select(source_versions.c.id).limit(1)).scalar_one()
        conn.execute(
            clauses.insert().values(
                source_version_id=version_id, ref="regulation-99", path="part/regulation-99", ordering=99,
                text="A relevant person must notify the registrar of beneficial ownership discrepancies within thirty days of discovery.",
                text_hash="h99", public_ok=True,
            )
        )
    run_extraction(engine)
    with engine.begin() as conn:
        from app.clhear.derived_models import activities as activities_t

        conn.execute(
            activities_t.insert().values(
                id="ACT-TEST-GAP", name="Test gap", description="", business_owner="",
                triggers=[{"anchor": {"source_key": "uksi/2017/692", "refs": ["regulation-99"]}, "when": {}}],
                status="curated",
            )
        )
    bp = compose(engine, {"attributes": {"jurisdictions": ["UK"]}, "activities": ["ACT-TEST-GAP"]}, log_request=False)
    assert bp["coverage_summary"]["gaps"] == 1
    assert bp["coverage"][0]["state"] == "gap"


# ------------------------------------------------------------------- lineage


def test_l2_lineage_bottoms_in_verbatim_clause(client, engine):
    _seed_corpus(engine)
    run_extraction(engine)
    oid = "OBL:uksi/2017/692#regulation-27"
    body = client.get(f"/api/clhear/layers/l2/items/{oid.replace('#', '%23')}/lineage").json()
    chain = body["lineage"]
    assert chain["meta"]["status"] == "derived"
    leaf = chain["children"][0]
    assert leaf["layer"] == "L1" and leaf["meta"]["resolved"] is True
    assert "customer due diligence" in leaf["meta"]["text"]
    assert leaf["meta"]["text_hash"] == "h27"


def test_restricted_text_never_leaks_through_lineage(client, engine):
    _seed_corpus(engine)
    run_extraction(engine)
    body = client.get("/api/clhear/layers/l3/items/BLK-INFOSEC-BASELINE/lineage").json()
    raw = json.dumps(body)
    assert "MUST NEVER LEAK" not in raw
    iso_leaves = [
        n for n in body["lineage"]["children"]
        if n["kind"] == "clause" and n["id"].startswith("iso/")
    ]
    assert iso_leaves and iso_leaves[0]["meta"]["text"] is None
    assert iso_leaves[0]["meta"]["locked"] is True


def test_program_lineage_walks_to_clauses(client, engine):
    _seed_corpus(engine)
    run_extraction(engine)
    body = client.get("/api/clhear/layers/l6/items/PRG:PRF-UK-EMI/lineage").json()

    def collect(node, acc):
        acc.append(node)
        for c in node.get("children", []):
            collect(c, acc)
        return acc

    nodes = collect(body["lineage"], [])
    assert {"L6", "L3", "L2", "L1"} <= {n["layer"] for n in nodes}
    assert any(n["kind"] == "clause" and (n["meta"].get("text") or "") for n in nodes)


def test_risk_items_computed_with_live_churn(client, engine):
    _seed_corpus(engine)
    run_extraction(engine)
    items = client.get("/api/clhear/layers/l7").json()["items"]
    assert items, "risk areas should be computed for sample profiles"
    uk = next(i for i in items if i["profile_id"] == "PRF-UK-EMI" and i["area"] == "financial-crime")
    assert uk["live_inputs"]["computed_live"] is True
    assert uk["live_inputs"]["change_events"] == 1
    assert uk["result"]["formula_version"] == "risk-v1"


# --------------------------------------------------------------------- evals


def test_l2_eval_gates(client, engine):
    _seed_corpus(engine)
    run_extraction(engine)
    integrity = run_suite(engine, "l2_basis_integrity")
    assert integrity["passed"] is True
    quality = run_suite(engine, "l2_extraction_quality")
    assert quality["scores"]["evaluated_in_corpus"] >= 3
    referential = run_suite(engine, "l3_l5_referential")
    assert referential["passed"] is True, referential["scores"]

    # Drift the basis clause without re-deriving: integrity must go red.
    with engine.begin() as conn:
        conn.execute(clauses.update().where(clauses.c.ref == "regulation-27").values(text_hash="drifted"))
    assert run_suite(engine, "l2_basis_integrity")["passed"] is False


# ----------------------------------------------------------------- API + UI


def test_l2_registry_endpoint_filters(client, engine):
    _seed_corpus(engine)
    run_extraction(engine)
    body = client.get("/api/clhear/layers/l2?q=due diligence").json()
    assert body["registry"]["total"] >= 1
    assert body["banner"]["data_status"] == "derived"
    assert all("due diligence" in (i["statement"] + i["title"]).lower() for i in body["registry"]["items"])


def test_unknown_items_404(client, engine):
    _seed_corpus(engine)
    assert client.get("/api/clhear/layers/l2/items/OBL:none%23nope/lineage").status_code == 404
    assert client.get("/api/clhear/layers/l9").status_code == 404


def test_ui_shells_served(client):
    home = client.get("/").text
    assert "the compliance stack" in home
    assert "Eval Studio" in home or "nav(\"/eval\")" in home
    assert "AI Ops" in home or "nav(\"/ops\")" in home
    assert client.get("/static/theme.css").status_code == 200
    assert client.get("/sources").status_code == 200
