"""OSCAL export — L3 blocks as a *component-definition*, an L6 blueprint as a
*system-security-plan* (OSCAL 1.1.2 JSON shapes).

Mapping (lossless for what a GRC tool needs to import a program):

* obligation (L2)      -> ``control-id`` (the CLHEAR stable / derivation id); the
                          instrument is the ``source`` (``clhear://l2/<source_key>``)
* block (L3)           -> ``component`` (type from the block kind); its
                          characteristics are ``props`` in the CLHEAR namespace
* blueprint item (L6)  -> ``by-components`` entry under the obligation's
                          ``implemented-requirement``; basis / load-bearing are props
* gap                  -> ``implemented-requirement`` with
                          ``implementation-status = not-implemented``

UUIDs are uuid5 of the CLHEAR id so repeated exports of the same release are
byte-identical. ``import_blueprint`` reads the SSP back into item / coverage
sets so the round trip can be checked.
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.clhear.derived_models import blocks as blocks_t
from app.clhear.derived_models import characteristics as characteristics_t
from app.clhear.derived_models import requires as requires_t

OSCAL_VERSION = "1.1.2"
NS = "https://clhear.org/ns/oscal"
_UUID_NS = uuid.UUID("6f1c2b7e-3f7a-4d3b-9c1e-0c1ea7000001")

_KIND_TYPE = {
    "System": "software", "Document": "policy", "Role": "process", "Configuration": "software",
    "Process": "process", "Workflow": "process", "Asset": "hardware", "Body": "process",
}


def _uuid(*parts: str) -> str:
    return str(uuid.uuid5(_UUID_NS, "|".join(parts)))


def _prop(name: str, value, cls: str | None = None) -> dict:
    p = {"name": name, "ns": NS, "value": str(value)}
    if cls:
        p["class"] = cls
    return p


def _metadata(title: str, version: str) -> dict:
    # deterministic: the release pins the content, so the timestamp is fixed
    return {"title": title, "last-modified": "1970-01-01T00:00:00Z",
            "version": version or "unreleased", "oscal-version": OSCAL_VERSION,
            "remarks": "Exported by CLHEAR. Obligations are controls; building blocks are components."}


def _json(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        import json

        try:
            return json.loads(value)
        except ValueError:
            return default
    return value


# ----------------------------------------------------------------- L3 -> component-definition


def component_definition(conn: Connection, *, release: str = "", block_ids: list[str] | None = None) -> dict:
    q = sa.select(blocks_t).where(blocks_t.c.valid_to.is_(None)).order_by(blocks_t.c.id)
    if block_ids is not None:
        q = q.where(blocks_t.c.id.in_(block_ids))
    rows = [dict(r) for r in conn.execute(q).mappings()]
    chars: dict[str, list[dict]] = {}
    try:
        for r in conn.execute(sa.select(characteristics_t).where(characteristics_t.c.valid_to.is_(None))).mappings():
            chars.setdefault(r["block_id"], []).append(dict(r))
    except sa.exc.OperationalError:
        pass
    reqs: dict[str, list[tuple[str, str]]] = {}
    try:
        for oid, bid, rationale in conn.execute(
                sa.select(requires_t.c.obligation_id, requires_t.c.block_id, requires_t.c.rationale)
                .where(requires_t.c.valid_to.is_(None)).order_by(requires_t.c.id)).all():
            reqs.setdefault(bid, []).append((oid, rationale or ""))
    except sa.exc.OperationalError:
        pass
    components = []
    for b in rows:
        props = [_prop("clhear-id", b["id"]), _prop("kind", b.get("kind") or "Process"), _prop("status", b.get("status") or "")]
        for c in chars.get(b["id"], []):
            props.append(_prop(f"characteristic:{c['key']}", c["value"], cls=c["status"]))
        by_source: dict[str, list[dict]] = {}
        for oid, rationale in reqs.get(b["id"], []):
            source = oid.split("#", 1)[0].removeprefix("OBL:") if oid.startswith("OBL:") else "registry"
            by_source.setdefault(source, []).append(
                {"uuid": _uuid("req", b["id"], oid), "control-id": oid, "description": rationale or f"{b['name']} is required by {oid}."})
        for sel in _json(b.get("satisfies"), []) or []:
            key = sel["source_key"]
            for ref in sel.get("refs") or ["*"]:
                oid = f"OBL:{key}#{ref}" if ref != "*" else f"OBL:{key}"
                by_source.setdefault(key, []).append(
                    {"uuid": _uuid("sat", b["id"], oid), "control-id": oid, "description": f"{b['name']} satisfies {oid} (curated selector)."})
        components.append({
            "uuid": _uuid("component", b["id"]),
            "type": _KIND_TYPE.get(b.get("kind") or "Process", "process"),
            "title": b["name"],
            "description": b.get("purpose") or b.get("description") or "",
            "props": props,
            "control-implementations": [
                {"uuid": _uuid("ci", b["id"], source), "source": f"clhear://l2/{source}",
                 "description": f"Obligations from {source} this block implements.", "implemented-requirements": items}
                for source, items in sorted(by_source.items())
            ],
        })
    return {"component-definition": {"uuid": _uuid("component-definition", release or "unreleased"),
                                     "metadata": _metadata("CLHEAR building blocks (L3)", release), "components": components}}


# ----------------------------------------------------------------- L6 -> system-security-plan


def blueprint_ssp(composition: dict, *, blueprint_id: str | None = None) -> dict:
    bid = blueprint_id or composition.get("blueprint_id") or composition.get("fingerprint") or "unstored"
    release = composition.get("release") or ""
    attrs = composition.get("profile_attributes") or {}
    items = composition.get("items") or []
    components = []
    for it in items:
        props = [_prop("clhear-id", it["block_id"]), _prop("kind", it["kind"]), _prop("basis", it["basis"]),
                 _prop("load-bearing", "true" if it.get("load_bearing_for") else "false")]
        for c in it.get("characteristics") or []:
            if c.get("in_profile", True):
                props.append(_prop(f"characteristic:{c['key']}", c["value"], cls=c["status"]))
        for a in it.get("activities_operated") or []:
            props.append(_prop("operated-by", a["activity_id"] if isinstance(a, dict) else a))
        components.append({"uuid": _uuid("item", bid, it["block_id"]), "type": _KIND_TYPE.get(it["kind"], "process"),
                           "title": it["name"], "description": it.get("explanation") or it.get("purpose") or "",
                           "props": props, "status": {"state": "operational"}})
    implemented = []
    for c in composition.get("coverage") or []:
        satisfied = c.get("satisfied_by") or []
        req = {
            "uuid": _uuid("ir", bid, c["obligation_id"]),
            "control-id": c["obligation_id"],
            "props": [_prop("implementation-status", "implemented" if c["state"] == "covered" else "not-implemented"),
                      _prop("source", c["source_key"]), _prop("clause-ref", c["clause_ref"])]
                     + ([_prop("stable-id", c["stable_id"])] if c.get("stable_id") else []),
            "remarks": c.get("title") or "",
            "by-components": [
                {"component-uuid": _uuid("item", bid, block), "uuid": _uuid("bc", bid, c["obligation_id"], block),
                 "description": f"{block} satisfies {c['obligation_id']}."}
                for block in satisfied
            ],
        }
        implemented.append(req)
    return {"system-security-plan": {
        "uuid": _uuid("ssp", bid),
        "metadata": _metadata(f"CLHEAR blueprint {bid}", release),
        "import-profile": {"href": f"clhear://l4/profiles/{composition.get('profile_id') or 'ad-hoc'}"},
        "system-characteristics": {
            "system-ids": [{"identifier-type": NS, "id": bid}],
            "system-name": f"Compliance program for {', '.join(attrs.get('jurisdictions') or []) or 'profile'}",
            "description": "Leanest complete compliance program composed by CLHEAR for this profile.",
            "props": [_prop(f"profile:{k}", ", ".join(map(str, v)) if isinstance(v, list) else v) for k, v in sorted(attrs.items())
                      if v not in (None, "", [], {})]
                     + [_prop("engine-version", composition.get("engine_version") or ""),
                        _prop("minimal", str(bool((composition.get("minimality") or {}).get("minimal"))).lower()),
                        _prop("composition-hash", composition.get("composition_hash") or "")],
            "security-sensitivity-level": "moderate",
            "system-information": {"information-types": [{"title": "Regulatory obligations", "description": "CLHEAR L2 registry entries applicable to this profile."}]},
            "status": {"state": "operational"},
            "authorization-boundary": {"description": "The obligations applicable to the profile and the blocks that satisfy them."},
        },
        "system-implementation": {"users": [], "components": components},
        "control-implementation": {"description": "One implemented-requirement per applicable obligation.",
                                   "implemented-requirements": implemented},
    }}


def import_blueprint(doc: dict) -> dict:
    """Read an exported SSP back: items, coverage and gaps as CLHEAR ids."""
    ssp = doc.get("system-security-plan") or {}
    comps = {c["uuid"]: c for c in (ssp.get("system-implementation") or {}).get("components") or []}
    def prop(obj, name):
        return next((p["value"] for p in obj.get("props") or [] if p["name"] == name), None)
    items = {prop(c, "clhear-id"): {"kind": prop(c, "kind"), "basis": prop(c, "basis"),
                                    "characteristics": {p["name"].split(":", 1)[1]: p["value"] for p in c.get("props") or []
                                                        if p["name"].startswith("characteristic:")}}
             for c in comps.values()}
    coverage = {}
    gaps = []
    for ir in (ssp.get("control-implementation") or {}).get("implemented-requirements") or []:
        blocks = sorted(prop(comps[bc["component-uuid"]], "clhear-id") for bc in ir.get("by-components") or [] if bc["component-uuid"] in comps)
        coverage[ir["control-id"]] = blocks
        if prop(ir, "implementation-status") == "not-implemented":
            gaps.append(ir["control-id"])
    sysc = ssp.get("system-characteristics") or {}
    return {"blueprint_id": (sysc.get("system-ids") or [{}])[0].get("id"), "items": items, "coverage": coverage, "gaps": gaps,
            "composition_hash": prop(sysc, "composition-hash"), "minimal": prop(sysc, "minimal") == "true"}


def round_trip_ok(composition: dict) -> tuple[bool, list[str]]:
    """Export -> import must reproduce items and satisfied-by sets exactly."""
    back = import_blueprint(blueprint_ssp(composition))
    problems = []
    want_items = {i["block_id"] for i in composition.get("items") or []}
    if set(back["items"]) != want_items:
        problems.append(f"items differ: {sorted(set(back['items']) ^ want_items)}")
    for c in composition.get("coverage") or []:
        if sorted(c.get("satisfied_by") or []) != back["coverage"].get(c["obligation_id"]):
            problems.append(f"coverage differs for {c['obligation_id']}")
    want_gaps = sorted(c["obligation_id"] for c in composition.get("coverage") or [] if c["state"] == "gap")
    if sorted(back["gaps"]) != want_gaps:
        problems.append("gaps differ")
    return not problems, problems
