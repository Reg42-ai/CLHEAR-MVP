"""GraphQL over the open layers (standard §8; HLD v2 "REST + GraphQL + bulk snapshots").

One schema in SDL, resolvers over the record tables, executed with ``graphql-core``.
Everything here is agnostic-mode content: L2 obligations, L3 blocks and
characteristics, L5 activities, L4 profiles, L6 blueprints, why-trails, crosswalks
and the release. L8 appears as *availability* metadata only (I9). Queries are
bounded: depth ≤ 8, list arguments capped at 200.
"""
from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from graphql import GraphQLError, ValidationRule, build_schema, execute_sync, parse, specified_rules, validate
from graphql.language import FieldNode, FragmentSpreadNode, InlineFragmentNode, OperationDefinitionNode
from sqlalchemy.engine import Connection, Engine

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

MAX_DEPTH = 8
MAX_LIMIT = 200

SDL = """
\"\"\"CLHEAR open layers. Ids are the public stable ids (OBL-, BLK-, ACT-, PRF-, BLU-, WHY-).\"\"\"
type Query {
  obligation(id: ID!): Obligation
  obligations(jurisdiction: String, sourceKey: String, search: String, limit: Int = 50): [Obligation!]!
  block(id: ID!): BuildingBlock
  blocks(kind: String, search: String, limit: Int = 50): [BuildingBlock!]!
  activity(id: ID!): Activity
  activities(limit: Int = 50): [Activity!]!
  profile(id: ID!): Profile
  blueprint(id: ID!): Blueprint
  blueprints(profileId: ID, limit: Int = 20): [Blueprint!]!
  whyTrail(id: ID!): WhyTrail
  crosswalk(framework: String!, ref: String!): CrosswalkRef
  frameworks: [Framework!]!
  release: Release!
  l8Availability(blockIds: [ID!]): L8Availability!
}

\"\"\"L2 — a registry entry derived from a clause (I3: every one has a why-trail).\"\"\"
type Obligation {
  id: ID!
  stableId: String
  title: String!
  statement: String
  sourceKey: String!
  clauseRef: String!
  jurisdiction: String
  modality: String
  addressee: String
  status: String
  version: Int
  validFrom: String
  validTo: String
  confidence: Float
  requires: [BuildingBlock!]!
  why: WhyTrail
}

\"\"\"L3 — a building block an obligation requires; characteristics are its schema fields.\"\"\"
type BuildingBlock {
  id: ID!
  name: String!
  kind: String
  description: String
  status: String
  version: Int
  validFrom: String
  validTo: String
  characteristics: [Characteristic!]!
  requiredBy: [Obligation!]!
  operatedBy: [Activity!]!
  crosswalk(framework: String): [CrosswalkRow!]!
  why: WhyTrail
}

type Characteristic { key: String!, value: String!, status: String, backingObligationId: String }

\"\"\"L5 — a compliance activity that operates blocks.\"\"\"
type Activity {
  id: ID!
  name: String!
  description: String
  actionType: String
  status: String
  operates: [BuildingBlock!]!
  why: WhyTrail
}

\"\"\"L4 — an organisation profile (predicates), the input to a blueprint.\"\"\"
type Profile { id: ID!, name: String, attributes: JSON, status: String, fingerprint: String }

\"\"\"L6 — the leanest complete program for a profile.\"\"\"
type Blueprint {
  id: ID!
  profileId: ID
  release: String
  status: String
  version: Int
  fingerprint: String
  minimal: Boolean
  engineVersion: String
  coverageSummary: CoverageSummary
  items: [BlueprintItem!]!
  coverage(state: String): [Coverage!]!
  why: WhyTrail
}

type CoverageSummary { covered: Int, gaps: Int, total: Int }

type BlueprintItem {
  itemId: ID
  block: BuildingBlock
  blockId: ID!
  name: String
  kind: String
  basis: String
  explanation: String
  obligationsSatisfied: [Obligation!]!
  loadBearingFor: [ID!]!
  characteristics: [Characteristic!]!
}

type Coverage { obligation: Obligation, obligationId: ID!, state: String!, satisfiedBy: [BuildingBlock!]! }

\"\"\"I3 — why a record exists: reasoning, evidence, agent, inputs hash.\"\"\"
type WhyTrail {
  id: ID!
  layer: String!
  subjectRef: String
  reasoning: String
  evidence: JSON
  agent: String
  skillVersion: String
  inputsHash: String
  confidence: Float
  createdAt: String
}

type Framework { key: String!, name: String!, publisher: String, rights: String, identifiersOnly: Boolean!, url: String }

type CrosswalkRow {
  framework: String!
  ref: String!
  title: String
  relation: String!
  basis: String
  via: CrosswalkVia
  block: BuildingBlock
  blockId: ID!
}

type CrosswalkVia { framework: String!, ref: String!, relation: String! }

type CrosswalkRef {
  framework: String!
  frameworkName: String
  ref: String!
  title: String
  rights: String
  blocks: [CrosswalkRow!]!
  also: [CrosswalkRow!]!
}

type Release { id: String, generatedAt: String, layers: JSON }

\"\"\"L8 metadata (I9): whether fills exist and at which maturity; the content is member-only and not served here.\"\"\"
type L8Availability { count: Int!, blocks: JSON, note: String }

scalar JSON
"""


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


def _lim(n) -> int:
    return max(1, min(50 if n is None else int(n), MAX_LIMIT))


# --------------------------------------------------------------------------- row → object


def _ob(r) -> dict:
    r = dict(r)
    return {"id": r.get("stable_id") or r["id"], "_pk": r["id"], "stableId": r.get("stable_id"), "title": r["title"], "statement": r.get("statement") or None,
            "sourceKey": r["source_key"], "clauseRef": r["clause_ref"], "jurisdiction": r.get("jurisdiction") or None, "modality": r.get("modality") or None,
            "addressee": r.get("addressee") or None, "status": r.get("status"), "version": r.get("version"), "validFrom": _iso(r.get("valid_from")),
            "validTo": _iso(r.get("valid_to")), "confidence": float(r["confidence"]) if r.get("confidence") is not None else None,
            "_why": r.get("why_trail_id")}


def _blk(r) -> dict:
    r = dict(r)
    return {"id": r["id"], "name": r["name"], "kind": r.get("kind"), "description": r.get("purpose") or r.get("description") or None, "status": r.get("status"),
            "version": r.get("version"), "validFrom": _iso(r.get("valid_from")), "validTo": _iso(r.get("valid_to")), "_why": r.get("why_trail_id")}


def _act(r) -> dict:
    r = dict(r)
    return {"id": r["id"], "name": r["name"], "description": r.get("description") or None, "actionType": r.get("action_type") or None,
            "status": r.get("status"), "_why": r.get("why_trail_id")}


def _why(r) -> dict:
    r = dict(r)
    return {"id": r["id"], "layer": r["layer"], "subjectRef": r.get("subject_ref"), "reasoning": r.get("reasoning_summary"), "evidence": _json(r.get("evidence_refs"), []),
            "agent": r.get("agent_id"), "skillVersion": r.get("skill_version"), "inputsHash": r.get("inputs_hash"),
            "confidence": float(r["confidence"]) if r.get("confidence") is not None else None, "createdAt": _iso(r.get("created_at"))}


def _blu(r) -> dict:
    r = dict(r)
    comp = _json(r.get("composition"), {}) or {}
    res = _json(r.get("result"), {}) or {}
    return {"id": r["stable_id"], "profileId": r.get("profile_id"), "release": r.get("release") or None, "status": r.get("status"), "version": r.get("version"),
            "fingerprint": r.get("fingerprint"), "minimal": bool((comp.get("minimality") or {}).get("minimal")) if comp else None,
            "engineVersion": r.get("engine_version") or None, "coverageSummary": res.get("coverage_summary") or comp.get("coverage_summary"),
            "_comp": comp, "_why": r.get("why_trail_id")}


# --------------------------------------------------------------------------- resolvers


def _conn(info) -> Connection:
    return info.context["conn"]


def _obligation_by_id(conn: Connection, oid: str) -> dict | None:
    row = conn.execute(sa.select(obligations_t).where(sa.or_(obligations_t.c.id == oid, obligations_t.c.stable_id == oid))).mappings().first()
    return _ob(row) if row else None


def _block_by_id(conn: Connection, bid: str) -> dict | None:
    row = conn.execute(sa.select(blocks_t).where(blocks_t.c.id == bid)).mappings().first()
    return _blk(row) if row else None


def _why_by_id(conn: Connection, wid: str | None) -> dict | None:
    if not wid:
        return None
    row = conn.execute(sa.select(record.why_trails).where(record.why_trails.c.id == wid)).mappings().first()
    return _why(row) if row else None


def _resolvers() -> dict[str, dict[str, Any]]:
    def q_obligation(_, info, id):
        return _obligation_by_id(_conn(info), id)

    def q_obligations(_, info, jurisdiction=None, sourceKey=None, search=None, limit=50):
        q = sa.select(obligations_t).where(obligations_t.c.valid_to.is_(None)).order_by(obligations_t.c.id).limit(_lim(limit))
        if jurisdiction:
            q = q.where(obligations_t.c.jurisdiction == jurisdiction)
        if sourceKey:
            q = q.where(obligations_t.c.source_key == sourceKey)
        if search:
            q = q.where(sa.func.lower(obligations_t.c.title).like(f"%{search.lower()}%"))
        return [_ob(r) for r in _conn(info).execute(q).mappings()]

    def q_block(_, info, id):
        return _block_by_id(_conn(info), id)

    def q_blocks(_, info, kind=None, search=None, limit=50):
        q = sa.select(blocks_t).where(blocks_t.c.valid_to.is_(None)).order_by(blocks_t.c.id).limit(_lim(limit))
        if kind:
            q = q.where(blocks_t.c.kind == kind)
        if search:
            q = q.where(sa.func.lower(blocks_t.c.name).like(f"%{search.lower()}%"))
        return [_blk(r) for r in _conn(info).execute(q).mappings()]

    def q_activity(_, info, id):
        row = _conn(info).execute(sa.select(activities_t).where(activities_t.c.id == id)).mappings().first()
        return _act(row) if row else None

    def q_activities(_, info, limit=50):
        q = sa.select(activities_t).where(activities_t.c.valid_to.is_(None)).order_by(activities_t.c.id).limit(_lim(limit))
        return [_act(r) for r in _conn(info).execute(q).mappings()]

    def q_profile(_, info, id):
        row = _conn(info).execute(sa.select(profiles_t).where(profiles_t.c.id == id)).mappings().first()
        if not row:
            return None
        r = dict(row)
        return {"id": r["id"], "name": r.get("name") or None, "attributes": _json(r.get("attributes"), {}), "status": r.get("status"), "fingerprint": r.get("fingerprint")}

    def q_blueprint(_, info, id):
        row = _conn(info).execute(sa.select(blueprints_t).where(blueprints_t.c.stable_id == id)).mappings().first()
        return _blu(row) if row else None

    def q_blueprints(_, info, profileId=None, limit=20):
        q = sa.select(blueprints_t).where(blueprints_t.c.stable_id.isnot(None)).order_by(blueprints_t.c.id.desc()).limit(_lim(limit))
        if profileId:
            q = q.where(blueprints_t.c.profile_id == profileId)
        return [_blu(r) for r in _conn(info).execute(q).mappings()]

    def q_why(_, info, id):
        return _why_by_id(_conn(info), id)

    def q_crosswalk(_, info, framework, ref):
        try:
            out = crosswalks.for_ref(_conn(info), framework, ref)
        except crosswalks.UnknownFramework:
            raise GraphQLError(f"unknown framework {framework}; see frameworks {{ key }}")
        out["frameworkName"] = out.pop("framework_name")
        out["blocks"] = [{**b, "blockId": b["block_id"]} for b in out["blocks"]]
        out["also"] = [{**a, "blockId": "", "via": None} for a in out["also"]]
        return out

    def q_frameworks(_, info):
        return [{"key": k, "name": v["name"], "publisher": v.get("publisher"), "rights": v.get("rights"), "identifiersOnly": bool(v.get("identifiers_only")),
                 "url": v.get("url")} for k, v in crosswalks.frameworks().items()]

    def q_release(_, info):
        from app.clhear.releases import get_latest

        man = get_latest(info.context["engine"]) or {}
        return {"id": man.get("release_id") or man.get("id"), "generatedAt": man.get("generated_at"), "layers": man.get("layers")}

    def q_l8(_, info, blockIds=None):
        from app.clhear.l8.fills import availability

        return availability(info.context["engine"], block_ids=blockIds)

    # nested
    def ob_requires(ob, info):
        conn = _conn(info)
        ids = [r[0] for r in conn.execute(sa.select(requires_t.c.block_id).where(requires_t.c.obligation_id == ob["_pk"], requires_t.c.valid_to.is_(None)))]
        return [b for b in (_block_by_id(conn, i) for i in sorted(set(ids))) if b]

    def blk_required_by(b, info):
        conn = _conn(info)
        ids = [r[0] for r in conn.execute(sa.select(requires_t.c.obligation_id).where(requires_t.c.block_id == b["id"], requires_t.c.valid_to.is_(None)))]
        return [o for o in (_obligation_by_id(conn, i) for i in sorted(set(ids))) if o]

    def blk_chars(b, info):
        rows = _conn(info).execute(sa.select(characteristics_t).where(characteristics_t.c.block_id == b["id"], characteristics_t.c.valid_to.is_(None))).mappings()
        return [{"key": c["key"], "value": c["value"], "status": c["status"], "backingObligationId": c["backing_obligation_id"]} for c in rows]

    def blk_operated_by(b, info):
        conn = _conn(info)
        ids = [r[0] for r in conn.execute(sa.select(operates_t.c.activity_id).where(operates_t.c.block_id == b["id"], operates_t.c.valid_to.is_(None)))]
        rows = conn.execute(sa.select(activities_t).where(activities_t.c.id.in_(sorted(set(ids))))).mappings() if ids else []
        return [_act(r) for r in rows]

    def blk_crosswalk(b, info, framework=None):
        rows = crosswalks.for_block(_conn(info), b["id"])
        return [{**r, "blockId": r["block_id"]} for r in rows if not framework or r["framework"] == framework]

    def act_operates(a, info):
        conn = _conn(info)
        ids = [r[0] for r in conn.execute(sa.select(operates_t.c.block_id).where(operates_t.c.activity_id == a["id"], operates_t.c.valid_to.is_(None)))]
        return [b for b in (_block_by_id(conn, i) for i in sorted(set(ids))) if b]

    def node_why(n, info):
        return _why_by_id(_conn(info), n.get("_why"))

    def blu_items(bp, info):
        return [{"itemId": it.get("id"), "blockId": it["block_id"], "name": it.get("name"), "kind": it.get("kind"), "basis": it.get("basis"),
                 "explanation": it.get("explanation"), "_obs": it.get("obligations_satisfied") or [], "loadBearingFor": it.get("load_bearing_for") or [],
                 "characteristics": [{"key": c["key"], "value": c["value"], "status": c.get("status"), "backingObligationId": None}
                                     for c in it.get("characteristics") or [] if c.get("in_profile", True)]}
                for it in bp["_comp"].get("items") or []]

    def blu_coverage(bp, info, state=None):
        return [{"obligationId": c["obligation_id"], "state": c["state"], "_by": c.get("satisfied_by") or []}
                for c in bp["_comp"].get("coverage") or [] if not state or c["state"] == state]

    def item_block(it, info):
        return _block_by_id(_conn(info), it["blockId"])

    def item_obs(it, info):
        conn = _conn(info)
        return [o for o in (_obligation_by_id(conn, i) for i in it["_obs"]) if o]

    def cov_obligation(c, info):
        return _obligation_by_id(_conn(info), c["obligationId"])

    def cov_by(c, info):
        conn = _conn(info)
        return [b for b in (_block_by_id(conn, i) for i in c["_by"]) if b]

    def row_block(r, info):
        return _block_by_id(_conn(info), r["blockId"]) if r.get("blockId") else None

    return {
        "Query": {"obligation": q_obligation, "obligations": q_obligations, "block": q_block, "blocks": q_blocks, "activity": q_activity,
                  "activities": q_activities, "profile": q_profile, "blueprint": q_blueprint, "blueprints": q_blueprints, "whyTrail": q_why,
                  "crosswalk": q_crosswalk, "frameworks": q_frameworks, "release": q_release, "l8Availability": q_l8},
        "Obligation": {"requires": ob_requires, "why": node_why},
        "BuildingBlock": {"requiredBy": blk_required_by, "characteristics": blk_chars, "operatedBy": blk_operated_by, "crosswalk": blk_crosswalk, "why": node_why},
        "Activity": {"operates": act_operates, "why": node_why},
        "Blueprint": {"items": blu_items, "coverage": blu_coverage, "why": node_why},
        "BlueprintItem": {"block": item_block, "obligationsSatisfied": item_obs},
        "Coverage": {"obligation": cov_obligation, "satisfiedBy": cov_by},
        "CrosswalkRow": {"block": row_block},
    }


def _bind(schema, resolvers: dict[str, dict[str, Any]]) -> None:
    for type_name, fields in resolvers.items():
        gtype = schema.get_type(type_name)
        for field_name, fn in fields.items():
            gtype.fields[field_name].resolve = fn


class DepthLimit(ValidationRule):
    """Reject queries nested deeper than MAX_DEPTH (fragments included)."""

    def enter_operation_definition(self, node: OperationDefinitionNode, *_):
        fragments = {d.name.value: d for d in self.context.document.definitions if getattr(d, "kind", "") == "fragment_definition"}

        def depth(sel_set, seen: frozenset) -> int:
            best = 0
            for sel in sel_set.selections:
                if isinstance(sel, FieldNode):
                    best = max(best, 1 + (depth(sel.selection_set, seen) if sel.selection_set else 0))
                elif isinstance(sel, InlineFragmentNode):
                    best = max(best, depth(sel.selection_set, seen))
                elif isinstance(sel, FragmentSpreadNode) and sel.name.value in fragments and sel.name.value not in seen:
                    best = max(best, depth(fragments[sel.name.value].selection_set, seen | {sel.name.value}))
            return best

        d = depth(node.selection_set, frozenset())
        if d > MAX_DEPTH:
            self.report_error(GraphQLError(f"query depth {d} exceeds the limit of {MAX_DEPTH}", node))


_SCHEMA = None


def schema():
    global _SCHEMA
    if _SCHEMA is None:
        s = build_schema(SDL)
        _bind(s, _resolvers())
        _SCHEMA = s
    return _SCHEMA


def _fmt(errors) -> list[dict]:
    return [{"message": e.message, "path": list(e.path) if e.path else None,
             "locations": [{"line": loc.line, "column": loc.column} for loc in (e.locations or [])]} for e in errors]


def execute(engine: Engine, query: str, variables: dict | None = None, operation_name: str | None = None) -> dict:
    try:
        document = parse(query)
    except GraphQLError as exc:
        return {"data": None, "errors": _fmt([exc])}
    problems = validate(schema(), document, rules=(*specified_rules, DepthLimit))
    if problems:
        return {"data": None, "errors": _fmt(problems)}
    with engine.connect() as conn:
        result = execute_sync(schema(), document, variable_values=variables or None, operation_name=operation_name,
                              context_value={"conn": conn, "engine": engine})
    out: dict = {"data": result.data}
    if result.errors:
        out["errors"] = _fmt(result.errors)
    return out


def sdl() -> str:
    return SDL.strip() + "\n"
