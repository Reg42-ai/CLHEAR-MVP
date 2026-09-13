"""Explore (HLD v2 §5): graph + list across layers. Any CLHEAR id resolves to
one node page — why (reasoning + evidence), history, who else needs this,
request a change — plus a one-hop constellation for the graph view and a
one-click cross-jurisdiction compare. Read-only; open (I9).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.engine import Connection

from app.clhear.db import get_engine
from app.clhear.derived_models import (
    activities,
    applies_to,
    asserts,
    blocks,
    blueprint_items,
    blueprints,
    characteristics,
    licences,
    mitigates,
    obligations,
    operates,
    profiles,
    requires,
)
from app.clhear.l1.models import clauses, sources
from app.clhear.platform import graph, record

router = APIRouter(tags=["explore"])
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

LIVE = ("derived", "validated")
_ID_KIND = [
    (re.compile(r"^OBL[-:]"), "obligation"), (re.compile(r"^BLK-"), "block"), (re.compile(r"^ACT-"), "activity"),
    (re.compile(r"^PRF-"), "profile"), (re.compile(r"^BLU-"), "blueprint"), (re.compile(r"^ITM-"), "item"),
    (re.compile(r"^LIC:"), "licence"), (re.compile(r"^CLS-|^\d+$"), "clause"),
]


def _json(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value


def kind_of(node_id: str) -> str | None:
    for rx, kind in _ID_KIND:
        if rx.match(node_id):
            return kind
    return "source" if "/" in node_id else None


def _why(conn: Connection, ids: list[str]) -> list[dict]:
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return []
    rows = conn.execute(sa.select(record.why_trails).where(record.why_trails.c.id.in_(ids))).mappings()
    return [{"id": r["id"], "layer": r["layer"], "subject_ref": r["subject_ref"], "reasoning_summary": r["reasoning_summary"],
             "evidence_refs": _json(r["evidence_refs"], []), "model_manifest": _json(r["model_manifest"], {}),
             "confidence": float(r["confidence"]) if r["confidence"] is not None else None, "agent_id": r["agent_id"],
             "created_at": str(r["created_at"])}
            for r in rows]


def _why_for_subject(conn: Connection, subject_ref: str, limit: int = 20) -> list[dict]:
    rows = conn.execute(sa.select(record.why_trails.c.id).where(record.why_trails.c.subject_ref == subject_ref)
                        .order_by(record.why_trails.c.id.desc()).limit(limit)).all()
    return _why(conn, [r[0] for r in rows])


def _shared(row) -> dict:
    return {"version": row.get("version"), "valid_from": str(row["valid_from"]) if row.get("valid_from") else None,
            "valid_to": str(row["valid_to"]) if row.get("valid_to") else None, "why_trail_id": row.get("why_trail_id"),
            "review": _json(row.get("review"), []), "status": row.get("status")}


def _edge(a: str, b: str, rel: str, layer: str, **extra) -> dict:
    return {"from": a, "to": b, "rel": rel, "layer": layer, **extra}


def _node(node_id: str, kind: str, layer: str, label: str, **extra) -> dict:
    return {"id": node_id, "kind": kind, "layer": layer, "label": label, "href": _href(node_id, kind), **extra}


def _href(node_id: str, kind: str) -> str:
    return graph.href_for(node_id, kind)


def _obligation(conn: Connection, ref: str):
    return conn.execute(sa.select(obligations).where(sa.or_(obligations.c.id == ref, obligations.c.stable_id == ref))).mappings().first()


# --------------------------------------------------------------------------- neighbourhood


def neighbourhood(conn: Connection, node_id: str) -> dict | None:
    """The node with its one-hop edges across layers (constellation view)."""
    kind = kind_of(node_id)
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    if kind == "obligation":
        ob = _obligation(conn, node_id)
        if ob is None:
            return None
        oid = ob["id"]
        nodes[oid] = _node(oid, "obligation", "L2", ob["title"], stable_id=ob["stable_id"], source_key=ob["source_key"],
                           clause_ref=ob["clause_ref"], jurisdiction=ob["jurisdiction"], status=ob["status"])
        nodes[ob["source_key"]] = _node(ob["source_key"], "source", "L1", ob["source_key"])
        edges.append(_edge(oid, ob["source_key"], "asserted_by", "L1", clause_ref=ob["clause_ref"]))
        for a in conn.execute(sa.select(asserts).where(asserts.c.obligation_id == oid, asserts.c.valid_to.is_(None))).mappings():
            cid = f"CLS-{a['clause_id']}"
            nodes[cid] = _node(cid, "clause", "L1", f"{ob['source_key']} · {ob['clause_ref']}", span=[a.get("span_start"), a.get("span_end")])
            edges.append(_edge(oid, cid, "asserts", "L2", strength=a.get("strength")))
        for r in conn.execute(sa.select(requires, blocks.c.name, blocks.c.kind).join(blocks, blocks.c.id == requires.c.block_id)
                              .where(requires.c.obligation_id == oid, requires.c.valid_to.is_(None))).mappings():
            nodes[r["block_id"]] = _node(r["block_id"], "block", "L3", r["name"], block_kind=r["kind"])
            edges.append(_edge(oid, r["block_id"], "requires", "L3", rationale=r["rationale"], method=r["method"]))
        for p in conn.execute(sa.select(applies_to).where(applies_to.c.obligation_id == oid, applies_to.c.valid_to.is_(None))).mappings():
            nodes[p["id"]] = _node(p["id"], "predicate", "L4", json.dumps(_json(p["predicate"], {})), basis=p["basis"], rationale=p["rationale"])
            edges.append(_edge(oid, p["id"], "applies_to", "L4"))
        for a in conn.execute(sa.select(activities).where(activities.c.valid_to.is_(None))).mappings():
            for t in _json(a["triggers"], []):
                if (t.get("obligation_ref") in (oid, ob["stable_id"])) or (
                        (t.get("anchor") or {}).get("source_key") == ob["source_key"] and ob["clause_ref"] in ((t.get("anchor") or {}).get("refs") or [])):
                    nodes[a["id"]] = _node(a["id"], "activity", "L5", a["name"], side=a.get("side"))
                    edges.append(_edge(a["id"], oid, "triggered_by", "L5"))
                    break
    elif kind == "block":
        b = conn.execute(sa.select(blocks).where(blocks.c.id == node_id)).mappings().first()
        if b is None:
            return None
        nodes[node_id] = _node(node_id, "block", "L3", b["name"], block_kind=b["kind"], purpose=b["purpose"], status=b["status"])
        for r in conn.execute(sa.select(requires, obligations.c.title, obligations.c.stable_id)
                              .join(obligations, obligations.c.id == requires.c.obligation_id)
                              .where(requires.c.block_id == node_id, requires.c.valid_to.is_(None))).mappings():
            nodes[r["obligation_id"]] = _node(r["obligation_id"], "obligation", "L2", r["title"], stable_id=r["stable_id"])
            edges.append(_edge(r["obligation_id"], node_id, "requires", "L3", rationale=r["rationale"]))
        for c in conn.execute(sa.select(characteristics).where(characteristics.c.block_id == node_id, characteristics.c.valid_to.is_(None))).mappings():
            cid = f"{node_id}/{c['key']}"
            nodes[cid] = _node(cid, "characteristic", "L3", f"{c['key']} = {c['value']}", status=c["status"])
            edges.append(_edge(node_id, cid, "characteristic", "L3", backing=c["backing_obligation_id"]))
        for o in conn.execute(sa.select(operates, activities.c.name).join(activities, activities.c.id == operates.c.activity_id)
                              .where(operates.c.block_id == node_id, operates.c.valid_to.is_(None))).mappings():
            nodes[o["activity_id"]] = _node(o["activity_id"], "activity", "L5", o["name"])
            edges.append(_edge(o["activity_id"], node_id, "operates", "L5"))
        for it in conn.execute(sa.select(blueprint_items.c.blueprint_id, blueprint_items.c.basis, blueprints.c.profile_id, blueprints.c.status)
                               .join(blueprints, blueprints.c.stable_id == blueprint_items.c.blueprint_id)
                               .where(blueprint_items.c.block_id == node_id, blueprints.c.status == "current")).mappings():
            nodes[it["blueprint_id"]] = _node(it["blueprint_id"], "blueprint", "L6", it["blueprint_id"], profile_id=it["profile_id"])
            edges.append(_edge(it["blueprint_id"], node_id, "item", "L6", basis=it["basis"]))
    elif kind == "activity":
        a = conn.execute(sa.select(activities).where(activities.c.id == node_id)).mappings().first()
        if a is None:
            return None
        nodes[node_id] = _node(node_id, "activity", "L5", a["name"], side=a.get("side"), action_type=a.get("action_type"), status=a["status"])
        for t in _json(a["triggers"], [])[:40]:
            ref = t.get("obligation_ref")
            if ref:
                ob = _obligation(conn, ref)
                if ob is not None:
                    nodes[ob["id"]] = _node(ob["id"], "obligation", "L2", ob["title"], stable_id=ob["stable_id"])
                    edges.append(_edge(node_id, ob["id"], "triggered_by", "L5", when=t.get("when")))
        for o in conn.execute(sa.select(operates, blocks.c.name).join(blocks, blocks.c.id == operates.c.block_id)
                              .where(operates.c.activity_id == node_id, operates.c.valid_to.is_(None))).mappings():
            nodes[o["block_id"]] = _node(o["block_id"], "block", "L3", o["name"])
            edges.append(_edge(node_id, o["block_id"], "operates", "L5"))
        for m in conn.execute(sa.select(mitigates).where(sa.or_(mitigates.c.compliance_activity_id == node_id, mitigates.c.business_activity_id == node_id),
                                                            mitigates.c.valid_to.is_(None))).mappings():
            other = m["business_activity_id"] if m["compliance_activity_id"] == node_id else m["compliance_activity_id"]
            row = conn.execute(sa.select(activities.c.name, activities.c.side).where(activities.c.id == other)).first()
            if row:
                nodes[other] = _node(other, "activity", "L5", row.name, side=row.side)
                edges.append(_edge(m["compliance_activity_id"], m["business_activity_id"], "mitigates", "L5"))
    elif kind == "profile":
        p = conn.execute(sa.select(profiles).where(profiles.c.id == node_id)).mappings().first()
        if p is None:
            return None
        attrs = _json(p["attributes"], {})
        nodes[node_id] = _node(node_id, "profile", "L4", p["name"], attributes=attrs, status=p["status"])
        for lic in attrs.get("authorisations") or []:
            row = conn.execute(sa.select(licences.c.id, licences.c.name).where(licences.c.name == lic)).first()
            if row:
                nodes[row.id] = _node(row.id, "licence", "L4", row.name)
                edges.append(_edge(node_id, row.id, "holds", "L4"))
        for b in conn.execute(sa.select(blueprints.c.stable_id, blueprints.c.status).where(blueprints.c.profile_id == node_id)
                              .order_by(blueprints.c.id.desc()).limit(5)).mappings():
            nodes[b["stable_id"]] = _node(b["stable_id"], "blueprint", "L6", b["stable_id"], status=b["status"])
            edges.append(_edge(node_id, b["stable_id"], "blueprint", "L6", status=b["status"]))
    elif kind == "blueprint":
        b = conn.execute(sa.select(blueprints).where(blueprints.c.stable_id == node_id)).mappings().first()
        if b is None:
            return None
        comp = _json(b["composition"], {}) or {}
        nodes[node_id] = _node(node_id, "blueprint", "L6", node_id, profile_id=b["profile_id"], status=b["status"],
                               coverage_summary=comp.get("coverage_summary"))
        if b["profile_id"]:
            nodes[b["profile_id"]] = _node(b["profile_id"], "profile", "L4", b["profile_id"])
            edges.append(_edge(node_id, b["profile_id"], "for_profile", "L4"))
        for it in comp.get("items") or []:
            nodes[it["block_id"]] = _node(it["block_id"], "block", "L3", it["name"], block_kind=it["kind"])
            edges.append(_edge(node_id, it["block_id"], "item", "L6", basis=it["basis"]))
            for oid in it.get("obligations_satisfied") or []:
                cov = next((c for c in comp.get("coverage") or [] if c["obligation_id"] == oid), {})
                nodes.setdefault(oid, _node(oid, "obligation", "L2", cov.get("title") or oid, stable_id=cov.get("stable_id")))
                edges.append(_edge(it["block_id"], oid, "satisfies", "L6"))
        for c in comp.get("coverage") or []:
            if c["state"] == "gap":
                nodes.setdefault(c["obligation_id"], _node(c["obligation_id"], "obligation", "L2", c.get("title") or c["obligation_id"], gap=True))
    elif kind == "licence":
        lic = conn.execute(sa.select(licences).where(licences.c.id == node_id)).mappings().first()
        if lic is None:
            return None
        nodes[node_id] = _node(node_id, "licence", "L4", lic["name"], jurisdiction=lic["jurisdiction"], regulator=lic["regulator"],
                               register=lic["register"], register_url=lic["register_url"])
        for p in conn.execute(sa.select(profiles.c.id, profiles.c.name, profiles.c.attributes).where(profiles.c.valid_to.is_(None))).mappings():
            if lic["name"] in (_json(p["attributes"], {}).get("authorisations") or []):
                nodes[p["id"]] = _node(p["id"], "profile", "L4", p["name"])
                edges.append(_edge(p["id"], node_id, "holds", "L4"))
    elif kind == "source":
        s = conn.execute(sa.select(sources).where(sources.c.key == node_id)).mappings().first()
        if s is None:
            return None
        nodes[node_id] = _node(node_id, "source", "L1", s["name"], jurisdiction=s["jurisdiction"], issuer=s["issuer"], rights_basis=s.get("rights_basis"))
        for ob in conn.execute(sa.select(obligations.c.id, obligations.c.title, obligations.c.stable_id).where(obligations.c.source_key == node_id,
                                                                                                                obligations.c.status.in_(LIVE)).limit(60)).mappings():
            nodes[ob["id"]] = _node(ob["id"], "obligation", "L2", ob["title"], stable_id=ob["stable_id"])
            edges.append(_edge(ob["id"], node_id, "asserted_by", "L1"))
    elif kind == "item":
        it = conn.execute(sa.select(blueprint_items).where(blueprint_items.c.id == node_id)).mappings().first()
        if it is None:
            return None
        nodes[node_id] = _node(node_id, "item", "L6", it["name"], block_id=it["block_id"], basis=it["basis"], blueprint_id=it["blueprint_id"])
        nodes[it["blueprint_id"]] = _node(it["blueprint_id"], "blueprint", "L6", it["blueprint_id"])
        nodes[it["block_id"]] = _node(it["block_id"], "block", "L3", it["name"])
        edges.append(_edge(it["blueprint_id"], node_id, "item", "L6"))
        edges.append(_edge(node_id, it["block_id"], "block", "L3"))
        for oid in _json(it["obligations_satisfied"], []):
            nodes.setdefault(oid, _node(oid, "obligation", "L2", oid))
            edges.append(_edge(node_id, oid, "satisfies", "L6"))
    else:
        return None
    return {"focus": node_id, "kind": kind, "nodes": list(nodes.values()), "edges": edges}


# --------------------------------------------------------------------------- node page


def who_else(conn: Connection, node_id: str, kind: str) -> dict:
    """Who else needs this: stored profiles / current blueprints that carry the node."""
    out = {"profiles": [], "blueprints": [], "count": 0}
    rows = conn.execute(sa.select(blueprints.c.stable_id, blueprints.c.profile_id, blueprints.c.composition, profiles.c.name)
                        .join(profiles, profiles.c.id == blueprints.c.profile_id, isouter=True)
                        .where(blueprints.c.status == "current")).mappings()
    for r in rows:
        comp = _json(r["composition"], {}) or {}
        hit = False
        if kind == "block":
            hit = any(i["block_id"] == node_id for i in comp.get("items") or [])
        elif kind == "obligation":
            hit = any(c["obligation_id"] == node_id or c.get("stable_id") == node_id for c in comp.get("coverage") or [])
        elif kind == "activity":
            hit = node_id in (comp.get("activities_evaluated") or []) and any(
                node_id in (c.get("triggered_by") or []) for c in comp.get("coverage") or [])
        if hit:
            out["blueprints"].append(r["stable_id"])
            if r["profile_id"] and r["profile_id"] not in [p["id"] for p in out["profiles"]]:
                out["profiles"].append({"id": r["profile_id"], "name": r["name"]})
    out["count"] = len(out["profiles"])
    return out


def _history_rows(conn: Connection, kind: str, node_id: str) -> list[dict]:
    if kind == "obligation":
        from app.clhear.derived_models import l2_change_events

        ob = _obligation(conn, node_id)
        if ob is None:
            return []
        return [{"when": str(c["detected_at"]), "event": c["kind"], "effective_date": str(c["effective_date"]) if c["effective_date"] else None,
                 "detail": _json(c["detail"], {}), "id": c["id"]}
                for c in conn.execute(sa.select(l2_change_events).where(l2_change_events.c.obligation_id == ob["id"]).order_by(l2_change_events.c.id)).mappings()]
    if kind == "blueprint":
        from app.clhear.l6 import composer

        return [{"when": h.get("created_at"), "event": h.get("status"), "id": h.get("blueprint_id"), "detail": h}
                for h in composer.history(conn, node_id)]
    table = {"block": blocks, "activity": activities, "profile": profiles, "licence": licences}.get(kind)
    if table is None:
        return []
    row = conn.execute(sa.select(table).where(table.c.id == node_id)).mappings().first()
    if row is None:
        return []
    shared = _shared(dict(row))
    entries = [{"when": shared["valid_from"], "event": "derived", "version": shared["version"], "id": node_id, "detail": {}}]
    for note in shared["review"] or []:
        entries.append({"when": note.get("at") or note.get("when"), "event": note.get("event") or "review", "id": node_id, "detail": note})
    if shared["valid_to"]:
        entries.append({"when": shared["valid_to"], "event": "invalidated", "id": node_id, "detail": {}})
    return entries


def _request_change(node_id: str, kind: str) -> dict | None:
    return {"obligation": {"method": "POST", "url": f"/l2/obligations/{node_id}/modification-requests", "layer": "L2",
                           "body": {"field": "determination", "proposed_value": "", "rationale": "", "requester": "anonymous"}},
            "block": {"method": "POST", "url": f"/l3/blocks/{node_id}/modification-requests", "layer": "L3",
                      "body": {"field": "name", "proposed_value": "", "rationale": "", "requester": "anonymous"}},
            "activity": {"method": "POST", "url": "/api/community/submissions", "layer": "L5", "target_id": node_id, "signed_in": True,
                         "body": {"kind": "correction", "target_layer": "L5", "target_id": node_id, "title": "", "body": ""}},
            "blueprint": {"method": "POST", "url": "/api/community/submissions", "layer": "L6", "target_id": node_id, "signed_in": True,
                          "body": {"kind": "correction", "target_layer": "L6", "target_id": node_id, "title": "", "body": ""}}}.get(kind)


def node_page(conn: Connection, node_id: str) -> dict | None:
    hood = neighbourhood(conn, node_id)
    if hood is None:
        return None
    kind = hood["kind"]
    focus = next(n for n in hood["nodes"] if n["id"] == (hood["focus"] if kind != "obligation" else next(
        n["id"] for n in hood["nodes"] if n["kind"] == "obligation" and (n["id"] == node_id or n.get("stable_id") == node_id))))
    why = _why_for_subject(conn, focus["id"])
    if kind == "obligation":
        ob = _obligation(conn, node_id)
        why = why or _why(conn, [ob["why_trail_id"]] if ob and ob.get("why_trail_id") else [])
        why += _why_for_subject(conn, ob["stable_id"]) if ob and ob["stable_id"] and ob["stable_id"] != focus["id"] else []
    elif kind in ("block", "activity", "profile", "licence", "blueprint"):
        table = {"block": blocks, "activity": activities, "profile": profiles, "licence": licences}.get(kind)
        if table is not None:
            row = conn.execute(sa.select(table.c.why_trail_id).where(table.c.id == node_id)).first()
            if row and row[0]:
                why = why or _why(conn, [row[0]])
        else:
            row = conn.execute(sa.select(blueprints.c.why_trail_id).where(blueprints.c.stable_id == node_id)).first()
            if row and row[0]:
                why = why or _why(conn, [row[0]])
    return {"node": focus, "kind": kind, "layer": focus["layer"], "why": why, "history": _history_rows(conn, kind, focus["id"]),
            "who_else": who_else(conn, focus["id"], kind), "request_change": _request_change(focus["id"], kind),
            "neighbours": [n for n in hood["nodes"] if n["id"] != focus["id"]], "edges": hood["edges"]}


# --------------------------------------------------------------------------- search


def search(conn: Connection, q: str, limit: int = 30) -> list[dict]:
    like = f"%{q.lower()}%"
    out: list[dict] = []
    for r in conn.execute(sa.select(obligations.c.id, obligations.c.stable_id, obligations.c.title, obligations.c.jurisdiction)
                          .where(obligations.c.status.in_(LIVE))
                          .where(sa.or_(sa.func.lower(obligations.c.title).like(like), sa.func.lower(obligations.c.id).like(like),
                                        sa.func.lower(obligations.c.stable_id).like(like))).limit(limit)).mappings():
        out.append(_node(r["id"], "obligation", "L2", r["title"], stable_id=r["stable_id"], jurisdiction=r["jurisdiction"]))
    for r in conn.execute(sa.select(blocks.c.id, blocks.c.name, blocks.c.kind).where(blocks.c.valid_to.is_(None))
                          .where(sa.or_(sa.func.lower(blocks.c.name).like(like), sa.func.lower(blocks.c.id).like(like))).limit(limit)).mappings():
        out.append(_node(r["id"], "block", "L3", r["name"], block_kind=r["kind"]))
    for r in conn.execute(sa.select(activities.c.id, activities.c.name, activities.c.side).where(activities.c.valid_to.is_(None))
                          .where(sa.or_(sa.func.lower(activities.c.name).like(like), sa.func.lower(activities.c.id).like(like))).limit(limit)).mappings():
        out.append(_node(r["id"], "activity", "L5", r["name"], side=r["side"]))
    for r in conn.execute(sa.select(profiles.c.id, profiles.c.name).where(profiles.c.valid_to.is_(None))
                          .where(sa.or_(sa.func.lower(profiles.c.name).like(like), sa.func.lower(profiles.c.id).like(like))).limit(limit)).mappings():
        out.append(_node(r["id"], "profile", "L4", r["name"]))
    for r in conn.execute(sa.select(licences.c.id, licences.c.name, licences.c.jurisdiction).where(licences.c.valid_to.is_(None))
                          .where(sa.or_(sa.func.lower(licences.c.name).like(like), sa.func.lower(licences.c.id).like(like))).limit(limit)).mappings():
        out.append(_node(r["id"], "licence", "L4", r["name"], jurisdiction=r["jurisdiction"]))
    for r in conn.execute(sa.select(sources.c.key, sources.c.name, sources.c.jurisdiction)
                          .where(sa.or_(sa.func.lower(sources.c.name).like(like), sa.func.lower(sources.c.key).like(like))).limit(limit)).mappings():
        out.append(_node(r["key"], "source", "L1", r["name"], jurisdiction=r["jurisdiction"]))
    for r in conn.execute(sa.select(blueprints.c.stable_id, blueprints.c.profile_id).where(blueprints.c.status == "current")
                          .where(sa.func.lower(blueprints.c.stable_id).like(like)).limit(limit)).mappings():
        out.append(_node(r["stable_id"], "blueprint", "L6", r["stable_id"], profile_id=r["profile_id"]))
    return out[:limit]


def layer_lists(conn: Connection) -> dict:
    """Counts + the list view per layer (the 'list' half of graph + list)."""
    def count(table, *where):
        q = sa.select(sa.func.count()).select_from(table)
        for w in where:
            q = q.where(w)
        try:
            return conn.execute(q).scalar_one()
        except sa.exc.OperationalError:
            return 0

    return {
        "L1": {"count": count(sources), "href": "/l1", "api": "/l1/sources"},
        "L2": {"count": count(obligations, obligations.c.status.in_(LIVE)), "href": "/l2", "api": "/l2/obligations"},
        "L3": {"count": count(blocks, blocks.c.valid_to.is_(None)), "href": "/l3", "api": "/l3/blocks"},
        "L4": {"count": count(profiles, profiles.c.valid_to.is_(None)), "licences": count(licences, licences.c.valid_to.is_(None)), "href": "/l4", "api": "/l4/profiles"},
        "L5": {"count": count(activities, activities.c.valid_to.is_(None)), "href": "/l5", "api": "/l5/activities"},
        "L6": {"count": count(blueprints, blueprints.c.status == "current"), "href": "/l6", "api": "/l6/blueprints"},
    }


# --------------------------------------------------------------------------- routes


@router.get("/explore", response_class=HTMLResponse, include_in_schema=False)
def explore_page() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "explore.html").read_text(), headers={"Cache-Control": "no-cache, must-revalidate"})


@router.get("/explore/layers")
def explore_layers() -> dict:
    with get_engine().connect() as conn:
        return {"layers": layer_lists(conn)}


@router.get("/explore/search")
def explore_search(q: str = Query(min_length=1), limit: int = Query(default=30, ge=1, le=100)) -> dict:
    with get_engine().connect() as conn:
        hits = search(conn, q, limit)
    return {"q": q, "count": len(hits), "hits": hits}


@router.get("/explore/graph")
def explore_graph(focus: str = Query(...)) -> dict:
    with get_engine().connect() as conn:
        hood = neighbourhood(conn, focus)
    if hood is None:
        raise HTTPException(status_code=404, detail=f"unknown node {focus}")
    return hood


@router.get("/explore/node/{node_id:path}")
def explore_node(node_id: str) -> dict:
    with get_engine().connect() as conn:
        page = node_page(conn, node_id)
    if page is None:
        raise HTTPException(status_code=404, detail=f"unknown node {node_id}")
    return page


@router.get("/explore/compare")
def explore_compare(jurisdictions: str = Query(description="comma-separated, e.g. uk,eu,us"), q: str | None = None,
                    limit: int = Query(default=100, ge=1, le=500)) -> dict:
    """One-click cross-jurisdiction compare (delegates to the L2 registry compare)."""
    from app.clhear.v1.l2 import compare as l2_compare

    return l2_compare(jurisdictions=jurisdictions, q=q, limit=limit)
