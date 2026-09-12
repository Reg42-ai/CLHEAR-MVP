"""L6 program composer — deterministic set-cover over the REAL L2 registry.

compose(profile) is a pure function of (profile facts, curated catalog,
derived obligation registry): same inputs => same blueprint. Gaps are
surfaced, never silently accepted. Every blueprint is logged for replay.
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.derived_models import activities as activities_t
from app.clhear.derived_models import blocks as blocks_t
from app.clhear.derived_models import blueprints, obligations
from app.clhear.derived_models import requires as requires_t

log = logging.getLogger("clhear.l6")

ENGINE_VERSION = "composer-v1"


def when_matches(when: dict, attributes: dict) -> bool:
    """Trigger condition evaluator — the L4 predicate language shared with
    ``applies_to`` edges and validity rules ("*" = present; list = any-of;
    scalar = equality / containment, case-insensitive)."""
    from app.clhear.l4.ontology import matches

    return matches(when, attributes)


def _l4_applicable(conn, attributes: dict) -> list[dict]:
    """Obligations whose live L4 applies_to edges all match the profile (HLD v2 §4.4)."""
    from app.clhear.l4.predicates import obligations_for_attributes

    try:
        return obligations_for_attributes(conn, attributes)
    except sa.exc.OperationalError:  # pre-m0012 database
        return []


def resolve_anchor(engine: Engine, anchor: dict) -> list[dict]:
    """Anchor {source_key, refs[]} -> derived obligation rows (may be empty:
    the anchor's source may be restricted or its clauses not duty-detected)."""
    query = sa.select(obligations).where(obligations.c.source_key == anchor["source_key"])
    refs = anchor.get("refs")
    if refs:
        query = query.where(obligations.c.clause_ref.in_(refs))
    with engine.connect() as conn:
        rows = conn.execute(query.where(obligations.c.status != "rejected")).mappings().all()
    return [dict(r) for r in rows]


def _live_requires(conn) -> dict[str, list[str]]:
    """obligation id -> block ids with a live L3 ``requires`` edge."""
    out: dict[str, list[str]] = {}
    try:
        rows = conn.execute(
            sa.select(requires_t.c.obligation_id, requires_t.c.block_id).where(requires_t.c.valid_to.is_(None))
        ).all()
    except sa.exc.OperationalError:  # pre-m0011 database
        return out
    for oid, bid in rows:
        out.setdefault(oid, []).append(bid)
    return out


def _selector_covers(selector: dict, obligation: dict) -> bool:
    if selector["source_key"] != obligation["source_key"]:
        return False
    refs = selector.get("refs")
    return not refs or obligation["clause_ref"] in refs


def compose(engine: Engine, profile: dict, requested_by: str = "", release: str = "", log_request: bool = True) -> dict:
    """Profile facts -> tailored blueprint with explicit gaps + provenance."""
    attributes = profile.get("attributes", {})
    wanted_activities = profile.get("activities")  # None = evaluate all curated

    with engine.connect() as conn:
        activity_rows = [dict(r) for r in conn.execute(sa.select(activities_t)).mappings()]
        block_rows = [dict(r) for r in conn.execute(sa.select(blocks_t)).mappings()]
        requires_edges = _live_requires(conn)
        l4_applicable = _l4_applicable(conn, attributes) if wanted_activities is None else []
    blocks_by_id = {b["id"]: b for b in block_rows}

    triggered: dict[str, dict] = {}  # obligation id -> {obligation, activities, conditions}
    unresolved_anchors: list[dict] = []
    # L4 applicability edges trigger directly (every edge matched the profile).
    for item in l4_applicable:
        ob = {"id": item["derivation_key"], "source_key": item["source_key"], "clause_ref": item["clause_ref"],
              "title": item["title"], "status": item["status"], "confidence": item["confidence"]}
        slot = triggered.setdefault(ob["id"], {"obligation": ob, "activities": [], "conditions": []})
        slot["activities"].append("L4:applies_to")
        slot["conditions"].append({k: v for p in item["predicates"] for k, v in p["predicate"].items()})
    for act in activity_rows:
        if wanted_activities is not None and act["id"] not in wanted_activities:
            continue
        for trigger in act["triggers"]:
            if not when_matches(trigger.get("when", {}), attributes):
                continue
            resolved = resolve_anchor(engine, trigger["anchor"])
            if not resolved:
                unresolved_anchors.append(
                    {"activity": act["id"], "anchor": trigger["anchor"],
                     "reason": "no derived obligation at this anchor (restricted source or non-duty clause)"}
                )
            for ob in resolved:
                slot = triggered.setdefault(
                    ob["id"], {"obligation": ob, "activities": [], "conditions": []}
                )
                if act["id"] not in slot["activities"]:
                    slot["activities"].append(act["id"])
                    slot["conditions"].append(trigger.get("when", {}))

    # Set-cover: which curated blocks satisfy the triggered obligations.
    coverage = []
    selected_blocks: dict[str, dict] = {}
    for oid, slot in sorted(triggered.items()):
        ob = slot["obligation"]
        covering = [
            b for b in block_rows if any(_selector_covers(sel, ob) for sel in b["satisfies"] or [])
        ]
        # L3 requires edges (HLD v2 §4.3) cover too; a merged block resolves to its canonical.
        for bid in requires_edges.get(oid, ()):
            b = blocks_by_id.get(bid)
            while b is not None and b.get("canonical_id") and b["canonical_id"] in blocks_by_id:
                b = blocks_by_id[b["canonical_id"]]
            if b is not None and b["id"] not in {c["id"] for c in covering}:
                covering.append(b)
        for b in covering:
            selected_blocks.setdefault(b["id"], b)
        coverage.append(
            {
                "obligation_id": oid,
                "source_key": ob["source_key"],
                "clause_ref": ob["clause_ref"],
                "title": ob["title"],
                "status": ob["status"],
                "confidence": float(ob["confidence"]),
                "triggered_by": slot["activities"],
                "state": "covered" if covering else "gap",
                "covered_by": [b["id"] for b in covering],
            }
        )

    # Honesty sweep: derived obligations in matching jurisdictions that no
    # curated activity anchors yet — the long tail is visible, not hidden.
    jurisdictions = set(attributes.get("jurisdictions", []))
    unmapped_count = 0
    unmapped_sample = []
    if jurisdictions:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.select(obligations.c.id, obligations.c.source_key, obligations.c.clause_ref, obligations.c.title)
                .where(obligations.c.jurisdiction.in_(jurisdictions))
                .where(obligations.c.status.in_(("derived", "validated")))
            ).all()
        for row in rows:
            if row.id not in triggered:
                unmapped_count += 1
                if len(unmapped_sample) < 25:
                    unmapped_sample.append(
                        {"obligation_id": row.id, "source_key": row.source_key,
                         "clause_ref": row.clause_ref, "title": row.title}
                    )

    states = [c["state"] for c in coverage]
    result = {
        "engine_version": ENGINE_VERSION,
        "release": release,
        "profile_attributes": attributes,
        "activities_evaluated": [a["id"] for a in activity_rows if wanted_activities is None or a["id"] in wanted_activities],
        "obligations_triggered": len(coverage),
        "coverage": coverage,
        "blocks": [
            {"id": b["id"], "name": b["name"], "capability": b["capability"],
             "evidence_artifacts": b["evidence_artifacts"]}
            for b in selected_blocks.values()
        ],
        "coverage_summary": {
            "covered": states.count("covered"),
            "gaps": states.count("gap"),
            "total": len(states),
        },
        "unresolved_anchors": unresolved_anchors,
        "unmapped_obligations": {"count": unmapped_count, "sample": unmapped_sample,
                                 "note": "derived obligations in your jurisdictions not yet mapped to any activity — visible by design"},
    }
    if log_request:
        with engine.begin() as conn:
            row = conn.execute(
                blueprints.insert().values(
                    requested_by=requested_by, release=release, profile=profile,
                    result={"coverage_summary": result["coverage_summary"],
                            "obligations_triggered": result["obligations_triggered"],
                            "blocks": [b["id"] for b in result["blocks"]]},
                    engine_version=ENGINE_VERSION,
                )
            )
            result["blueprint_id"] = row.inserted_primary_key[0]
    return result
