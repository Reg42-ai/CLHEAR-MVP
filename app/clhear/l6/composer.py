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

log = logging.getLogger("clhear.l6")

ENGINE_VERSION = "composer-v1"


def when_matches(when: dict, attributes: dict) -> bool:
    """Trigger condition evaluator. "*" = attribute is present/truthy;
    list attributes use containment; scalars use equality."""
    for key, requirement in (when or {}).items():
        value = attributes.get(key)
        if requirement == "*":
            if not value:
                return False
        elif isinstance(value, list):
            if requirement not in value:
                return False
        elif value != requirement:
            return False
    return True


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

    triggered: dict[str, dict] = {}  # obligation id -> {obligation, activities, conditions}
    unresolved_anchors: list[dict] = []
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
            b for b in block_rows if any(_selector_covers(sel, ob) for sel in b["satisfies"])
        ]
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
