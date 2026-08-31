"""Team view — AI fleet personas + human contributors around each layer."""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.community_models import submissions, users
from app.clhear.models import llm_calls, runs
from app.clhear.platform.router import TASKS, last_decisions

# Abstract geometric marks — no fake human faces.
PERSONAS = (
    {"id": "scout", "name": "Scout", "layer": "L1", "fleet": "l1.ingest", "task_id": None,
     "mission": "Fetches official texts from authorities nightly", "tools": ["adapters", "S3", "citator"]},
    {"id": "scribe", "name": "Scribe", "layer": "L1", "fleet": "l1.annotate", "task_id": "l1.annotate",
     "mission": "Grounded clause annotation — origin=llm, never replaces text", "tools": ["gateway"]},
    {"id": "miner", "name": "Miner", "layer": "L2", "fleet": "l2.extract", "task_id": None,
     "mission": "Deterministic duty extraction — proudly no LLM", "tools": ["extractor"]},
    {"id": "weaver", "name": "Weaver", "layer": "L2", "fleet": "l2.consolidate", "task_id": "l2.consolidate",
     "mission": "Cross-jurisdiction concept consolidation, closed-world OBL members", "tools": ["gateway", "GPU"]},
    {"id": "mason", "name": "Mason", "layer": "L3", "fleet": "l3.generate", "task_id": "l3.block_generate",
     "mission": "Builds control blocks grounded on live obligations", "tools": ["gateway", "GPU"]},
    {"id": "surveyor", "name": "Surveyor", "layer": "L4", "fleet": "l4.licenses", "task_id": "l4.license_extract",
     "mission": "Grounded license RAG — never invents a permission type", "tools": ["retrieval", "gateway"]},
    {"id": "cartographer", "name": "Cartographer", "layer": "L5", "fleet": "l5.map", "task_id": "l5.activity_map",
     "mission": "Maps obligations to activities, L4 schema only", "tools": ["gateway"]},
    {"id": "composer", "name": "Composer", "layer": "L6", "fleet": "l6.narrate", "task_id": "l6.rationale",
     "mission": "Set-cover math + citation-checked rationale", "tools": ["composer"]},
    {"id": "actuary", "name": "Actuary", "layer": "L7", "fleet": "l7.narrate", "task_id": "l7.narrative",
     "mission": "Formula scores + number-echo commentary", "tools": ["risk-v1", "facts-file"]},
    {"id": "registrar", "name": "Registrar", "layer": "L8", "fleet": "l8.cohorts", "task_id": None,
     "mission": "k≥5 cohort aggregates — zero LLM", "tools": ["k-anonymity"]},
    {"id": "auditor", "name": "Auditor", "layer": "L0", "fleet": "evals", "task_id": None,
     "mission": "Eval suites as gates, not reports", "tools": ["evals"]},
    {"id": "router", "name": "Router", "layer": "L0", "fleet": "router", "task_id": None,
     "mission": "Inference optimizer — cheapest sufficient model", "tools": ["gateway", "GPU", "quality-table"]},
    {"id": "referee", "name": "Referee", "layer": "L0", "fleet": "l0.referee", "task_id": "l0.revalidate",
     "mission": "Correction revalidation judge", "tools": ["gateway"]},
)


def _last_run(engine: Engine, fleet: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(runs).where(runs.c.fleet == fleet).order_by(runs.c.id.desc()).limit(1)
        ).first()
    if row is None:
        # fleets are also recorded under ai.nightly — fall back to any run mentioning the fleet.
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(runs).where(runs.c.fleet.like(f"%{fleet.split('.')[-1]}%")).order_by(runs.c.id.desc()).limit(1)
            ).first()
    if row is None:
        return None
    outputs = row.outputs if isinstance(row.outputs, dict) else {}
    return {
        "status": outputs.get("status") or "succeeded",
        "ts": str(row.created_at),
        "reasoning": row.reasoning,
        "duration_ms": row.duration_ms,
    }


def _model_for(engine: Engine, task_id: str | None) -> dict | None:
    if not task_id:
        return None
    for d in last_decisions(engine, limit=80):
        if d["task_id"] == task_id:
            return {"model": d["model"], "tier": d["tier"], "reason": d["routing_reason"]}
    spec = TASKS.get(task_id)
    if spec:
        return {"model": "(not yet routed)", "tier": None, "reason": spec.description}
    return None


def _humans(engine: Engine, layer: str | None = None) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(users.c.display_name, users.c.email, sa.func.count().label("n"))
            .select_from(submissions.join(users, users.c.id == submissions.c.submitter_id))
            .where(submissions.c.status.in_(("accepted", "shipped")))
            .group_by(users.c.display_name, users.c.email)
            .order_by(sa.desc("n"))
            .limit(8)
        ).all()
    maintainer = {
        "name": "Avner Yoffe", "role": "maintainer", "kind": "human",
        "accepted": None, "email_domain": "reg42.ai",
    }
    out = [maintainer]
    for r in rows:
        out.append({
            "name": r.display_name or "contributor",
            "role": "contributor",
            "kind": "human",
            "accepted": int(r.n),
            "email_domain": (r.email or "").split("@")[-1],
        })
    return out


def persona_cards(engine: Engine, layer: str | None = None) -> list[dict]:
    cards = []
    for p in PERSONAS:
        if layer and p["layer"] != layer:
            continue
        last = _last_run(engine, p["fleet"])
        model = _model_for(engine, p["task_id"])
        led = "idle"
        if last:
            led = "ok" if last.get("status") not in ("failed", "failure") else "bad"
        cards.append({
            **p,
            "kind": "fleet",
            "model": model,
            "last_run": last,
            "led": led,
        })
    return cards


def team_view(engine: Engine, layer: str | None = None) -> dict:
    from app.clhear import ai_ops

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "layer": layer,
        "fleets": persona_cards(engine, layer),
        "humans": _humans(engine, layer),
        "activity": ai_ops.activity_items(engine, layer=layer, limit=20),
    }
