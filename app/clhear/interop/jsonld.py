"""JSON-LD — a published ``@context`` for the CLHEAR vocabulary and node documents for
any public id (standard §8; HLD v2 §4.9 "Build on it": OSCAL and JSON-LD export).

Terms are grounded in vocabularies GRC tools and knowledge graphs already speak:
PROV-O for derivation and why-trails (I3), SKOS for crosswalk relations, Dublin Core
for titles and sources, schema.org for validity windows (I2). CLHEAR-specific
relations (``requires``, ``satisfies``, ``operates``, ``impliesActivity``) live under
``https://clhear.org/ns#``. Ids resolve at ``https://clhear.org/id/<id>``.
"""
from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.clhear.derived_models import activities as activities_t
from app.clhear.derived_models import blocks as blocks_t
from app.clhear.derived_models import blueprints as blueprints_t
from app.clhear.derived_models import characteristics as characteristics_t
from app.clhear.derived_models import obligations as obligations_t
from app.clhear.derived_models import operates as operates_t
from app.clhear.derived_models import profiles as profiles_t
from app.clhear.derived_models import requires as requires_t
from app.clhear.interop import crosswalks
from app.clhear.platform import record

NS = "https://clhear.org/ns#"
ID_BASE = "https://clhear.org/id/"
CONTEXT_URL = "https://clhear.org/ns/context.jsonld"

CONTEXT: dict[str, Any] = {
    "@version": 1.1,
    "@vocab": NS,
    "clhear": NS,
    "schema": "https://schema.org/",
    "dcterms": "http://purl.org/dc/terms/",
    "prov": "http://www.w3.org/ns/prov#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "id": "@id",
    "type": "@type",
    # classes
    "Obligation": "clhear:Obligation",
    "BuildingBlock": "clhear:BuildingBlock",
    "Activity": "clhear:Activity",
    "Profile": "clhear:Profile",
    "Blueprint": "clhear:Blueprint",
    "BlueprintItem": "clhear:BlueprintItem",
    "WhyTrail": "prov:Activity",
    "Characteristic": "clhear:Characteristic",
    # descriptive
    "title": "dcterms:title",
    "name": "schema:name",
    "description": "dcterms:description",
    "statement": "clhear:statement",
    "source": {"@id": "dcterms:source", "@type": "@id"},
    "sourceKey": "clhear:sourceKey",
    "clauseRef": "clhear:clauseRef",
    "jurisdiction": "clhear:jurisdiction",
    "modality": "clhear:modality",
    "addressee": "clhear:addressee",
    "kind": "clhear:kind",
    "status": "clhear:status",
    "layer": "clhear:layer",
    "stableId": "dcterms:identifier",
    "version": "schema:version",
    "release": "clhear:release",
    "validFrom": {"@id": "schema:validFrom", "@type": "xsd:date"},
    "validTo": {"@id": "schema:validThrough", "@type": "xsd:date"},
    "confidence": {"@id": "clhear:confidence", "@type": "xsd:decimal"},
    # relations between layers (derive downward, I1)
    "requires": {"@id": "clhear:requires", "@type": "@id", "@container": "@set"},
    "requiredBy": {"@id": "clhear:requiredBy", "@type": "@id", "@container": "@set"},
    "satisfies": {"@id": "clhear:satisfies", "@type": "@id", "@container": "@set"},
    "satisfiedBy": {"@id": "clhear:satisfiedBy", "@type": "@id", "@container": "@set"},
    "operates": {"@id": "clhear:operates", "@type": "@id", "@container": "@set"},
    "operatedBy": {"@id": "clhear:operatedBy", "@type": "@id", "@container": "@set"},
    "items": {"@id": "clhear:item", "@container": "@set"},
    "block": {"@id": "clhear:block", "@type": "@id"},
    "basis": "clhear:basis",
    "loadBearingFor": {"@id": "clhear:loadBearingFor", "@type": "@id", "@container": "@set"},
    "characteristics": {"@id": "clhear:characteristic", "@container": "@set"},
    "key": "clhear:key",
    "value": "clhear:value",
    "profile": {"@id": "clhear:profile", "@type": "@id"},
    "coverage": {"@id": "clhear:coverage", "@container": "@set"},
    "obligation": {"@id": "clhear:obligation", "@type": "@id"},
    "state": "clhear:coverageState",
    # provenance (I3)
    "whyTrail": {"@id": "prov:wasGeneratedBy", "@type": "@id"},
    "derivedFrom": {"@id": "prov:wasDerivedFrom", "@type": "@id", "@container": "@set"},
    "derivedBy": {"@id": "prov:wasAssociatedWith", "@type": "@id"},
    "derivedAt": {"@id": "prov:generatedAtTime", "@type": "xsd:dateTime"},
    "reasoning": "clhear:reasoningSummary",
    "evidence": {"@id": "prov:used", "@container": "@set"},
    "agent": "prov:agent",
    "skillVersion": "clhear:skillVersion",
    "inputsHash": "clhear:inputsHash",
    # crosswalks (SKOS mapping)
    "exactMatch": {"@id": "skos:exactMatch", "@type": "@id", "@container": "@set"},
    "closeMatch": {"@id": "skos:closeMatch", "@type": "@id", "@container": "@set"},
    "broadMatch": {"@id": "skos:broadMatch", "@type": "@id", "@container": "@set"},
    "narrowMatch": {"@id": "skos:narrowMatch", "@type": "@id", "@container": "@set"},
    "relatedMatch": {"@id": "skos:relatedMatch", "@type": "@id", "@container": "@set"},
    "notation": "skos:notation",
    "inScheme": {"@id": "skos:inScheme", "@type": "@id"},
}

FRAMEWORK_IRI = {
    "nist/csf-2.0": "https://csrc.nist.gov/ns/csf/2.0#",
    "nist/sp800-53r5": "https://csrc.nist.gov/ns/sp800-53/r5#",
    "iso/27001-2022": "https://www.iso.org/ns/27001/2022#",
    "csa/ccm-4.0": "https://cloudsecurityalliance.org/ns/ccm/4.0#",
}


def context_document() -> dict:
    return {"@context": CONTEXT}


def iri(node_id: str) -> str:
    return ID_BASE + node_id


def framework_iri(framework: str, ref: str) -> str:
    return FRAMEWORK_IRI.get(framework, f"https://clhear.org/ns/frameworks/{framework}#") + ref


def _json(v, default):
    if v is None:
        return default
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return default
    return v


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _common(row, layer: str) -> dict:
    out = {"layer": layer, "status": row.get("status"), "version": row.get("version")}
    if row.get("valid_from"):
        out["validFrom"] = _iso(row["valid_from"])
    if row.get("valid_to"):
        out["validTo"] = _iso(row["valid_to"])
    if row.get("why_trail_id"):
        out["whyTrail"] = iri(row["why_trail_id"])
    if row.get("derived_by"):
        out["derivedBy"] = row["derived_by"]
    if row.get("derived_at"):
        out["derivedAt"] = _iso(row["derived_at"])
    if row.get("confidence") is not None:
        out["confidence"] = float(row["confidence"])
    return {k: v for k, v in out.items() if v is not None}


def _crosswalk_props(conn: Connection, block_id: str) -> dict:
    props: dict[str, list] = {}
    for r in crosswalks.for_block(conn, block_id):
        props.setdefault(r["relation"], []).append(framework_iri(r["framework"], r["ref"]))
    return props


def why_trail(conn: Connection, why_id: str) -> dict | None:
    row = conn.execute(sa.select(record.why_trails).where(record.why_trails.c.id == why_id)).mappings().first()
    if row is None:
        return None
    return {"id": iri(row["id"]), "type": "WhyTrail", "layer": row["layer"], "reasoning": row["reasoning_summary"], "agent": row["agent_id"],
            "skillVersion": row["skill_version"], "inputsHash": row["inputs_hash"], "evidence": _json(row["evidence_refs"], []),
            "confidence": float(row["confidence"]) if row["confidence"] is not None else None, "derivedAt": _iso(row["created_at"])}


def obligation(conn: Connection, ob_id: str) -> dict | None:
    row = conn.execute(sa.select(obligations_t).where(sa.or_(obligations_t.c.id == ob_id, obligations_t.c.stable_id == ob_id))).mappings().first()
    if row is None:
        return None
    row = dict(row)
    req = [r[0] for r in conn.execute(sa.select(requires_t.c.block_id).where(requires_t.c.obligation_id == row["id"], requires_t.c.valid_to.is_(None)))]
    doc = {"id": iri(row.get("stable_id") or row["id"]), "type": "Obligation", "stableId": row.get("stable_id"), "title": row["title"],
           "statement": row.get("statement") or None, "sourceKey": row["source_key"], "source": f"clhear://l1/{row['source_key']}",
           "clauseRef": row["clause_ref"], "jurisdiction": row.get("jurisdiction") or None, "modality": row.get("modality") or None,
           "addressee": row.get("addressee") or None, "requires": [iri(b) for b in sorted(set(req))], **_common(row, "L2")}
    return {k: v for k, v in doc.items() if v not in (None, [], "")}


def block(conn: Connection, block_id: str) -> dict | None:
    row = conn.execute(sa.select(blocks_t).where(blocks_t.c.id == block_id)).mappings().first()
    if row is None:
        return None
    row = dict(row)
    # link by the obligation's public stable id (OBL-000001), the same IRI the obligation node itself carries
    required_by = [r[1] or r[0] for r in conn.execute(
        sa.select(requires_t.c.obligation_id, obligations_t.c.stable_id)
        .select_from(requires_t.outerjoin(obligations_t, obligations_t.c.id == requires_t.c.obligation_id))
        .where(requires_t.c.block_id == block_id, requires_t.c.valid_to.is_(None)))]
    chars = [{"type": "Characteristic", "key": c["key"], "value": c["value"], "status": c["status"]}
             for c in conn.execute(sa.select(characteristics_t).where(characteristics_t.c.block_id == block_id, characteristics_t.c.valid_to.is_(None))).mappings()]
    operated = [r[0] for r in conn.execute(sa.select(operates_t.c.activity_id).where(operates_t.c.block_id == block_id, operates_t.c.valid_to.is_(None)))]
    doc = {"id": iri(block_id), "type": "BuildingBlock", "name": row["name"], "description": row.get("purpose") or row.get("description") or None,
           "kind": row.get("kind"), "requiredBy": [iri(o) for o in sorted(set(required_by))], "operatedBy": [iri(a) for a in sorted(set(operated))],
           "characteristics": chars, **_crosswalk_props(conn, block_id), **_common(row, "L3")}
    return {k: v for k, v in doc.items() if v not in (None, [], "")}


def activity(conn: Connection, act_id: str) -> dict | None:
    row = conn.execute(sa.select(activities_t).where(activities_t.c.id == act_id)).mappings().first()
    if row is None:
        return None
    row = dict(row)
    ops = [r[0] for r in conn.execute(sa.select(operates_t.c.block_id).where(operates_t.c.activity_id == act_id, operates_t.c.valid_to.is_(None)))]
    doc = {"id": iri(act_id), "type": "Activity", "name": row["name"], "description": row.get("description") or None, "kind": row.get("action_type") or None,
           "operates": [iri(b) for b in sorted(set(ops))], **_common(row, "L5")}
    return {k: v for k, v in doc.items() if v not in (None, [], "")}


def profile(conn: Connection, prf_id: str) -> dict | None:
    row = conn.execute(sa.select(profiles_t).where(profiles_t.c.id == prf_id)).mappings().first()
    if row is None:
        return None
    row = dict(row)
    doc = {"id": iri(prf_id), "type": "Profile", "name": row.get("name") or None, "attributes": _json(row["attributes"], {}), **_common(row, "L4")}
    return {k: v for k, v in doc.items() if v not in (None, [], "")}


def blueprint(composition: dict, *, blueprint_id: str | None = None, row: dict | None = None) -> dict:
    """A blueprint (L6) as JSON-LD: items link to blocks, coverage links obligations to the blocks that satisfy them."""
    bid = blueprint_id or composition.get("blueprint_id") or "unstored"
    items = []
    for it in composition.get("items") or []:
        items.append({"type": "BlueprintItem", "block": iri(it["block_id"]), "name": it.get("name"), "kind": it.get("kind"), "basis": it.get("basis"),
                      "satisfies": [iri(o) for o in it.get("obligations_satisfied") or []],
                      "loadBearingFor": [iri(o) for o in it.get("load_bearing_for") or []],
                      "operatedBy": [iri(a["activity_id"] if isinstance(a, dict) else a) for a in it.get("activities_operated") or []],
                      "characteristics": [{"type": "Characteristic", "key": c["key"], "value": c["value"], "status": c.get("status")}
                                          for c in it.get("characteristics") or [] if c.get("in_profile", True)],
                      "description": it.get("explanation") or None})
    coverage = [{"obligation": iri(c["obligation_id"]), "state": c["state"], "satisfiedBy": [iri(b) for b in c.get("satisfied_by") or []]}
                for c in composition.get("coverage") or []]
    doc = {"@context": CONTEXT_URL, "id": iri(bid), "type": "Blueprint", "release": composition.get("release") or None,
           "profile": iri(composition["profile_id"]) if composition.get("profile_id") else None,
           "attributes": composition.get("profile_attributes") or None, "items": items, "coverage": coverage,
           "compositionHash": composition.get("composition_hash"), "minimal": bool((composition.get("minimality") or {}).get("minimal")),
           "engineVersion": composition.get("engine_version")}
    if row:
        doc.update(_common(row, "L6"))
    return {k: v for k, v in doc.items() if v not in (None, [], "")}


def node(conn: Connection, node_id: str) -> dict | None:
    """Any public id → its JSON-LD document with the shared context."""
    prefix = node_id.split("-", 1)[0] if "-" in node_id else ""
    doc = None
    if prefix == "OBL" or node_id.startswith("OBL:"):
        doc = obligation(conn, node_id)
    elif prefix == "BLK":
        doc = block(conn, node_id)
    elif prefix == "ACT":
        doc = activity(conn, node_id)
    elif prefix == "PRF":
        doc = profile(conn, node_id)
    elif prefix == "WHY":
        doc = why_trail(conn, node_id)
    elif prefix == "BLU":
        row = conn.execute(sa.select(blueprints_t).where(blueprints_t.c.stable_id == node_id)).mappings().first()
        if row is not None:
            comp = _json(row["composition"], {}) or {}
            return blueprint(comp, blueprint_id=node_id, row=dict(row))
    if doc is None:
        return None
    return {"@context": CONTEXT_URL, **doc}


def import_blueprint(doc: dict) -> dict:
    """Read a blueprint JSON-LD document back into CLHEAR ids (round trip)."""
    strip = lambda s: s[len(ID_BASE):] if isinstance(s, str) and s.startswith(ID_BASE) else s  # noqa: E731
    return {"blueprint_id": strip(doc.get("id")), "items": {strip(i["block"]): {"kind": i.get("kind"), "basis": i.get("basis")} for i in doc.get("items") or []},
            "coverage": {strip(c["obligation"]): sorted(strip(b) for b in c.get("satisfiedBy") or []) for c in doc.get("coverage") or []},
            "gaps": sorted(strip(c["obligation"]) for c in doc.get("coverage") or [] if c.get("state") == "gap")}


def round_trip_ok(composition: dict) -> tuple[bool, list[str]]:
    back = import_blueprint(blueprint(composition))
    problems = []
    want = {i["block_id"] for i in composition.get("items") or []}
    if set(back["items"]) != want:
        problems.append(f"items differ: {sorted(set(back['items']) ^ want)}")
    for c in composition.get("coverage") or []:
        if sorted(c.get("satisfied_by") or []) != back["coverage"].get(c["obligation_id"]):
            problems.append(f"coverage differs for {c['obligation_id']}")
    return not problems, problems
