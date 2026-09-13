"""Interoperability (standard §8; item 16): identifier crosswalks with a rights basis,
JSON-LD context and node documents, GraphQL over the open layers, OSCAL importer-side
validation, and the round trips that prove a blueprint leaves and comes back whole.
"""
from __future__ import annotations

import json

import pytest
import sqlalchemy as sa

from app.clhear import curated
from app.clhear.interop import crosswalks, graphql_api, jsonld, oscal
from app.clhear.l6 import composer
from tests.test_interop import COMPOSITION
from tests.test_l6_blueprints import BROKER, _corpus


# --------------------------------------------------------------------------- crosswalks


def test_crosswalk_seed_is_rights_clean_and_well_formed():
    s = crosswalks.seed()
    fws = s["frameworks"]
    assert set(fws) == {"nist/csf-2.0", "nist/sp800-53r5", "iso/27001-2022", "csa/ccm-4.0"}
    assert fws["iso/27001-2022"]["identifiers_only"] and fws["csa/ccm-4.0"]["identifiers_only"] and not fws["nist/csf-2.0"]["identifiers_only"]
    assert set(s["titles"]) == {"nist/csf-2.0", "nist/sp800-53r5"}  # never a title for a licensed framework
    for e in s["framework_edges"]:
        assert e["relation"] in crosswalks.RELATIONS and e["basis"]
        assert crosswalks._valid_ref(e["from"], e["from_ref"]) and crosswalks._valid_ref(e["to"], e["to_ref"]), e
    for e in s["block_edges"]:
        assert e["relation"] in crosswalks.RELATIONS and crosswalks._valid_ref(e["framework"], e["ref"])
    assert crosswalks.title_for("nist/csf-2.0", "GV.OC-03").startswith("Legal, regulatory")
    assert crosswalks.title_for("iso/27001-2022", "A.5.31") is None
    with pytest.raises(crosswalks.UnknownFramework):
        crosswalks.title_for("cobit/2019", "APO01")


def test_crosswalk_rows_direct_and_via_with_degraded_relation(engine):
    curated.seed(engine)
    with engine.connect() as conn:
        rows = crosswalks.for_block(conn, "BLK-BREACH-RESPONSE")
        by = {(r["framework"], r["ref"]): r for r in rows}
        # direct anchors from implements_controls and the seed
        assert by[("nist/sp800-53r5", "ir-4")]["via"] is None and by[("nist/csf-2.0", "RS.MA-01")]["via"] is None
        assert by[("nist/csf-2.0", "RS.CO-02")]["relation"] == "closeMatch" and by[("nist/csf-2.0", "RS.CO-02")]["basis"].startswith("72-hour")
        # one hop: RS.MA-01 → ISO A.5.26 (closeMatch∘closeMatch = closeMatch), RS.CO-02 → ISO A.6.8 (relatedMatch)
        iso = by[("iso/27001-2022", "A.5.26")]
        assert iso["via"] == {"framework": "nist/csf-2.0", "ref": "RS.MA-01", "relation": "closeMatch"} and iso["relation"] == "closeMatch" and iso["title"] is None
        assert by[("iso/27001-2022", "A.6.8")]["relation"] == "relatedMatch"
        assert by[("csa/ccm-4.0", "SEF-03")]["via"]["ref"] == "RS.MA-01" and by[("csa/ccm-4.0", "SEF-03")]["title"] is None
        # public-domain titles travel with the row
        assert by[("nist/csf-2.0", "RS.MA-01")]["title"].startswith("The incident response plan")
        fw = crosswalks.for_framework(conn, "iso/27001-2022")
        assert fw and all(r["framework"] == "iso/27001-2022" and r["title"] is None for r in fw)
        ref = crosswalks.for_ref(conn, "nist/csf-2.0", "ID.RA-01")
        assert {b["block_id"] for b in ref["blocks"]} == {"BLK-BUSINESS-RISK-ASSESSMENT", "BLK-ICT-RESILIENCE", "BLK-INFOSEC-BASELINE"}
        assert {a["framework"] for a in ref["also"]} == {"iso/27001-2022", "csa/ccm-4.0", "nist/sp800-53r5"}
        s = crosswalks.summary(conn)
        assert {f["key"] for f in s["frameworks"]} == set(crosswalks.frameworks()) and s["blocks_with_any"] >= 8
        iso_s = next(f for f in s["frameworks"] if f["key"] == "iso/27001-2022")
        assert iso_s["identifiers_only"] and iso_s["direct"] == 0 and iso_s["via"] > 0
        with pytest.raises(crosswalks.UnknownFramework):
            crosswalks.for_framework(conn, "nope")


def _corpus_anchored(engine, tmp_path):
    """The L6 corpus plus one curated `requires` anchor: L3 decomposition mints its own blocks (BLK-000001…),
    so the curated block the crosswalk seed maps has to be tied to a live obligation explicitly."""
    from app.clhear.derived_models import obligations as obligations_t, requires as requires_t

    _corpus(engine, tmp_path)
    with engine.begin() as conn:
        ob = conn.execute(sa.select(obligations_t.c.id).where(obligations_t.c.valid_to.is_(None)).limit(1)).scalar_one()
        conn.execute(requires_t.insert().values(id="REQ-CW-000001", obligation_id=ob, block_id="BLK-AML-GOVERNANCE",
                                                rationale="curated anchor", method="curated-anchor"))


def test_crosswalk_carries_obligations_once_requires_exist(engine, tmp_path):
    _corpus_anchored(engine, tmp_path)
    with engine.connect() as conn:
        ref = crosswalks.for_ref(conn, "nist/csf-2.0", "GV.OC-03")
        gov = next(b for b in ref["blocks"] if b["block_id"] == "BLK-AML-GOVERNANCE")
        assert gov["required_by"] and all(o["source_key"] and o["clause_ref"] for o in gov["required_by"])


# --------------------------------------------------------------------------- JSON-LD


def test_jsonld_context_grounds_terms_in_prov_skos_dcterms():
    ctx = jsonld.context_document()["@context"]
    assert ctx["@vocab"] == jsonld.NS and ctx["whyTrail"]["@id"] == "prov:wasGeneratedBy" and ctx["derivedFrom"]["@id"] == "prov:wasDerivedFrom"
    assert ctx["closeMatch"]["@id"] == "skos:closeMatch" and ctx["title"] == "dcterms:title" and ctx["validTo"]["@id"] == "schema:validThrough"
    assert ctx["requires"]["@type"] == "@id" and ctx["requires"]["@container"] == "@set"
    for term in ("Obligation", "BuildingBlock", "Activity", "Profile", "Blueprint", "BlueprintItem", "WhyTrail"):
        assert term in ctx


def test_jsonld_nodes_and_blueprint_round_trip(engine, tmp_path):
    _corpus_anchored(engine, tmp_path)
    bp = composer.compose(engine, {"attributes": BROKER}, requested_by="test")
    with engine.connect() as conn:
        blk = jsonld.node(conn, "BLK-AML-GOVERNANCE")
        assert blk["@context"] == jsonld.CONTEXT_URL and blk["id"] == "https://clhear.org/id/BLK-AML-GOVERNANCE" and blk["type"] == "BuildingBlock"
        assert blk["requiredBy"] and all(u.startswith("https://clhear.org/id/") for u in blk["requiredBy"])
        assert "https://csrc.nist.gov/ns/csf/2.0#GV.OC-03" in blk["closeMatch"] + blk.get("relatedMatch", [])
        assert any(u.startswith("https://www.iso.org/ns/27001/2022#A.") for u in blk.get("closeMatch", []) + blk.get("relatedMatch", []))
        ob_iri = blk["requiredBy"][0]
        ob = jsonld.node(conn, ob_iri.rsplit("/", 1)[1])
        assert ob["type"] == "Obligation" and ob["source"].startswith("clhear://l1/") and blk["id"] in ob["requires"] and ob["whyTrail"].startswith("https://clhear.org/id/WHY-")
        why = jsonld.node(conn, ob["whyTrail"].rsplit("/", 1)[1])
        assert why["type"] == "WhyTrail" and why["layer"] == "L2" and why["reasoning"]
        doc = jsonld.node(conn, bp["blueprint_id"])
        assert doc["type"] == "Blueprint" and doc["items"] and doc["coverage"] and doc["whyTrail"]
        assert jsonld.node(conn, "BLK-NOPE") is None and jsonld.node(conn, "ZZZ-1") is None
        ok, problems = jsonld.round_trip_ok(composer.get_blueprint(conn, bp["blueprint_id"])["composition"])
        assert ok, problems
    ok, problems = jsonld.round_trip_ok(COMPOSITION)
    assert ok, problems
    back = jsonld.import_blueprint(jsonld.blueprint(COMPOSITION, blueprint_id="BLU-000001"))
    assert back["blueprint_id"] == "BLU-000001" and back["gaps"] == ["OBL:uksi/2017/692#regulation-33"]
    assert back["coverage"]["OBL:uksi/2017/692#regulation-27"] == ["BLK-CDD-PROGRAMME"]


# --------------------------------------------------------------------------- OSCAL importer-side validation


def test_oscal_validator_passes_our_export_and_catches_a_broken_one():
    doc = oscal.blueprint_ssp(COMPOSITION, blueprint_id="BLU-000001")
    assert oscal.validate_ssp(doc) == []
    bad = json.loads(json.dumps(doc))
    bad["system-security-plan"]["control-implementation"]["implemented-requirements"][0]["by-components"][0]["component-uuid"] = "00000000-0000-4000-8000-000000000000"
    del bad["system-security-plan"]["metadata"]["oscal-version"]
    bad["system-security-plan"]["system-implementation"]["components"][0]["uuid"] = "not-a-uuid"
    problems = oscal.validate_ssp(bad)
    assert any("unknown component" in p for p in problems) and any("missing oscal-version" in p for p in problems) and any("malformed uuid" in p for p in problems)
    assert oscal.validate_ssp({"nope": {}}) and oscal.validate_component_definition({"nope": {}})
    ok, problems = oscal.round_trip_ok(COMPOSITION)
    assert ok, problems


def test_oscal_component_definition_validates_on_a_real_store(engine, tmp_path):
    _corpus(engine, tmp_path)
    with engine.connect() as conn:
        cd = oscal.component_definition(conn, release="r1")
    assert oscal.validate_component_definition(cd) == [] and cd["component-definition"]["components"]


# --------------------------------------------------------------------------- GraphQL


def test_graphql_schema_builds_and_walks_the_layers(engine, tmp_path):
    _corpus_anchored(engine, tmp_path)
    bp = composer.compose(engine, {"attributes": BROKER}, requested_by="test")
    q = """
    query($id: ID!) {
      frameworks { key identifiersOnly }
      blocks(kind: "Role", limit: 5) { id name kind characteristics { key value } requiredBy { id title jurisdiction why { id layer agent } }
        crosswalk(framework: "nist/csf-2.0") { ref title relation via { ref } } }
      blueprint(id: $id) { id status minimal coverageSummary { covered gaps total }
        items { blockId basis block { name } obligationsSatisfied { id } }
        coverage(state: "covered") { obligationId satisfiedBy { id } } why { layer reasoning } }
      l8Availability { count note }
      release { id }
    }"""
    out = graphql_api.execute(engine, q, variables={"id": bp["blueprint_id"]})
    assert "errors" not in out, out.get("errors")
    d = out["data"]
    assert {f["key"] for f in d["frameworks"]} == set(crosswalks.frameworks())
    gov = next(b for b in d["blocks"] if b["id"] == "BLK-AML-GOVERNANCE")
    assert gov["requiredBy"] and gov["requiredBy"][0]["why"]["layer"] == "L2" and gov["requiredBy"][0]["why"]["agent"]
    assert {c["ref"] for c in gov["crosswalk"]} >= {"GV.OC-03", "GV.RR-02"} and all(c["title"] for c in gov["crosswalk"])
    b = d["blueprint"]
    assert b["id"] == bp["blueprint_id"] and b["coverageSummary"]["total"] > 0 and b["items"] and b["items"][0]["block"]["name"]
    assert all(c["satisfiedBy"] for c in b["coverage"]) and b["why"]["layer"] == "L6"
    assert d["l8Availability"]["count"] == 0 and "member" in d["l8Availability"]["note"].lower()
    # a lookup by stable id and by derivation id resolve to the same obligation
    oid = gov["requiredBy"][0]["id"]
    one = graphql_api.execute(engine, "{ obligation(id: %s) { id stableId requires { id } } }" % json.dumps(oid))["data"]["obligation"]
    assert one["id"] == oid and "BLK-AML-GOVERNANCE" in {r["id"] for r in one["requires"]}
    # crosswalk by ref
    x = graphql_api.execute(engine, '{ crosswalk(framework: "nist/csf-2.0", ref: "GV.OC-03") { frameworkName title blocks { blockId relation } also { framework ref } } }')
    assert x["data"]["crosswalk"]["blocks"] and x["data"]["crosswalk"]["also"] and x["data"]["crosswalk"]["title"]
    err = graphql_api.execute(engine, '{ crosswalk(framework: "nope", ref: "x") { ref } }')
    assert err["errors"][0]["message"].startswith("unknown framework")


def test_graphql_bounds_depth_and_list_sizes_and_reports_syntax_errors(engine):
    curated.seed(engine)
    deep = "{ blocks { requiredBy { requires { requiredBy { requires { requiredBy { requires { requiredBy { requires { id } } } } } } } } } }"
    out = graphql_api.execute(engine, deep)
    assert out["data"] is None and "exceeds the limit" in out["errors"][0]["message"]
    assert graphql_api.execute(engine, "{ blocks( { id }")["errors"][0]["message"].startswith("Syntax Error")
    assert graphql_api.execute(engine, "{ nope }")["errors"][0]["message"].startswith("Cannot query field")
    assert graphql_api._lim(10_000) == graphql_api.MAX_LIMIT and graphql_api._lim(0) == 1
    ok = graphql_api.execute(engine, "{ blocks(limit: 5000) { id } }")
    assert "errors" not in ok and len(ok["data"]["blocks"]) <= graphql_api.MAX_LIMIT
    assert "type Query" in graphql_api.sdl() and "l8Availability" in graphql_api.sdl()


# --------------------------------------------------------------------------- API + public repo


def test_interop_api(client, engine, tmp_path):
    _corpus_anchored(engine, tmp_path)
    bp = composer.compose(engine, {"attributes": BROKER}, requested_by="test")
    idx = client.get("/interop").json()
    assert idx["graphql"]["endpoint"] == "/graphql" and idx["jsonld"]["context"] == "/interop/context.jsonld" and "iso/27001-2022" in idx["crosswalks"]["frameworks"]
    ctx = client.get("/interop/context.jsonld")
    assert ctx.status_code == 200 and ctx.headers["content-type"].startswith("application/ld+json") and "@context" in ctx.json()
    node = client.get("/interop/jsonld/BLK-AML-GOVERNANCE")
    assert node.status_code == 200 and node.headers["content-type"].startswith("application/ld+json") and node.json()["type"] == "BuildingBlock"
    assert client.get("/interop/jsonld/BLK-NOPE").status_code == 404
    assert client.get(f"/l6/blueprints/{bp['blueprint_id']}/export?format=jsonld").json()["type"] == "Blueprint"
    assert client.get(f"/l6/blueprints/{bp['blueprint_id']}/export?format=xml").status_code == 422
    cw = client.get("/interop/crosswalks").json()
    assert {f["key"] for f in cw["frameworks"]} == set(crosswalks.frameworks())
    iso = client.get("/interop/crosswalks/iso/27001-2022").json()
    assert iso["identifiers_only"] is True and iso["count"] > 0 and all(r["title"] is None for r in iso["rows"])
    ref = client.get("/interop/crosswalks/nist/csf-2.0/GV.OC-03").json()
    assert ref["title"] and ref["blocks"] and ref["blocks"][0]["required_by"]
    assert client.get("/interop/crosswalks/cobit/2019/APO01").status_code == 404
    assert client.get("/interop/blocks/BLK-BREACH-RESPONSE/crosswalk").json()["count"] >= 6
    assert "type Query" in client.get("/interop/schema.graphql").text
    g = client.post("/graphql", json={"query": "{ blocks(limit: 2) { id kind } release { id } }"})
    assert g.status_code == 200 and len(g.json()["data"]["blocks"]) == 2
    assert client.get("/graphql", params={"query": "{ frameworks { key } }"}).json()["data"]["frameworks"]
    assert client.post("/graphql", json={}).status_code == 400
    assert client.post("/graphql", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 400
    assert client.post("/graphql", json={"query": "{ nope }"}).json()["errors"]


def test_public_repo_ships_context_sdl_and_crosswalks(engine, tmp_path):
    from app.clhear.platform import public_repo

    curated.seed(engine)
    repo = tmp_path / "repo"
    written = {p.relative_to(repo).as_posix() for p in public_repo.write_interop(engine, repo, "r1")}
    assert {"schema/context.jsonld", "schema/schema.graphql", "crosswalks/index.json", "crosswalks/iso_27001-2022.json", "crosswalks/nist_csf-2.0.json"} <= written
    iso = json.loads((repo / "crosswalks/iso_27001-2022.json").read_text())
    assert iso["rows"] and all(r["title"] is None for r in iso["rows"]) and "identifiers only" in iso["rights"]
    idx = json.loads((repo / "crosswalks/index.json").read_text())
    assert idx["release"] == "r1" and idx["frameworks"]["iso/27001-2022"]["identifiers_only"] is True
    static = {p.relative_to(repo).as_posix() for p in public_repo.copy_static(repo)}
    assert "crosswalks/README.md" in static and "crosswalks/seed.json" in static
