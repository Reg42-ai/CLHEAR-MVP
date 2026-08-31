"""L5 activity mapping — closed-world on BOTH ends.

Every trigger anchor must resolve to a live obligation, and every `when`
condition may reference only attributes in the L4 schema. Unknown attributes
are rejected before write.
"""
from __future__ import annotations

import json
import logging
import re

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.derived_models import activities as activities_t
from app.clhear.derived_models import attribute_schema as attribute_schema_t
from app.clhear.derived_models import obligations
from app.clhear.platform.router import complete

log = logging.getLogger("clhear.l5.map")

MAX_MAP = 10


def _schema_keys(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return {r.key for r in conn.execute(sa.select(attribute_schema_t.c.key))}


def _unmapped(engine: Engine, limit: int) -> list[dict]:
    with engine.connect() as conn:
        acts = [dict(r) for r in conn.execute(sa.select(activities_t)).mappings()]
        obs = [
            dict(r)
            for r in conn.execute(
                sa.select(obligations).where(obligations.c.status.in_(("derived", "validated")))
            ).mappings()
        ]
    covered: set[tuple[str, str]] = set()
    for a in acts:
        for t in a.get("triggers") or []:
            anc = t.get("anchor") or {}
            for ref in anc.get("refs") or []:
                covered.add((anc.get("source_key"), ref))
    out = []
    for o in obs:
        if (o["source_key"], o["clause_ref"]) not in covered:
            out.append(o)
        if len(out) >= limit:
            break
    return out


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def map_activities(engine: Engine, llm, limit: int = MAX_MAP) -> dict:
    keys = _schema_keys(engine)
    written = rejected = 0
    ids: list[str] = []
    for ob in _unmapped(engine, limit):
        prompt = (
            "Map this obligation to a business activity. `when` keys MUST be a subset of "
            f"{sorted(keys)}. trigger refs MUST be this obligation's source_key and clause_ref.\n"
            'JSON: {"activity_name": "", "description": "", "business_owner": "", '
            '"when": {}}\n\n'
            f"{ob['id']} [{ob['jurisdiction']}] {ob['title']}\n{ob['statement'][:400]}"
        )
        try:
            result = complete(
                llm, "l5.activity_map",
                prompt=prompt,
                system="Closed-world mapper. Never invent profile attributes. JSON only.",
                required_keys=["activity_name", "when"],
                max_tokens=400,
            )
            parsed = json.loads(result.text)
        except Exception:
            log.exception("L5 map failed for %s", ob["id"])
            rejected += 1
            continue
        when = parsed.get("when") or {}
        if not isinstance(when, dict) or any(k not in keys for k in when):
            rejected += 1
            continue
        aid = f"ACT-AI-{_slug(str(parsed['activity_name']))}"
        trigger = {
            "anchor": {"source_key": ob["source_key"], "refs": [ob["clause_ref"]]},
            "when": when,
        }
        with engine.begin() as conn:
            exists = conn.execute(sa.select(activities_t).where(activities_t.c.id == aid)).first()
            if exists:
                triggers = exists.triggers if isinstance(exists.triggers, list) else json.loads(exists.triggers or "[]")
                if trigger not in triggers:
                    triggers.append(trigger)
                    conn.execute(activities_t.update().where(activities_t.c.id == aid).values(triggers=triggers))
            else:
                conn.execute(
                    activities_t.insert().values(
                        id=aid,
                        name=str(parsed["activity_name"])[:160],
                        description=str(parsed.get("description", ""))[:400],
                        business_owner=str(parsed.get("business_owner", "compliance"))[:80],
                        triggers=[trigger],
                        status="ai_generated",
                    )
                )
        from app.clhear.governance import mark_generated

        mark_generated(
            engine, layer="L5", subject_ref=aid, generated_by=result.model,
            routing_reason="closed-world activity map",
            detail={"obligation_id": ob["id"], "when": when},
        )
        written += 1
        ids.append(aid)
    try:
        from app.clhear import ai_ops

        ai_ops.record(
            engine, kind="fleet_generation", layer="L5", fleet="l5.map",
            reasoning=f"Cartographer: {written} activity mappings written; {rejected} rejected (schema/anchor)",
            detail={"written": written, "rejected": rejected, "ids": ids},
        )
    except Exception:
        log.exception("L5 ai_ops failed")
    return {"written": written, "rejected": rejected, "ids": ids}
