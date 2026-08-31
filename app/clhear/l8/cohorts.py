"""L8 cohort engine — k≥5 aggregates over accumulated blueprint requests.

Publishes real aggregates only when a cohort reaches k. Until then a clearly
labeled synthetic demo cohort is the only public row. Zero LLM.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.derived_models import blueprints
from app.clhear.models import cohorts

log = logging.getLogger("clhear.l8.cohorts")

K = 5
SYNTHETIC = {
    "id": "COH:demo-uk-emi",
    "label": "Synthetic demo — UK EMI-shaped (not real peers)",
    "n": 0,
    "k_threshold": K,
    "synthetic": True,
    "published": True,
    "aggregates": {
        "mean_coverage_ratio": None,
        "note": "k-anonymity not met — this row is a labeled synthetic placeholder, not a peer benchmark",
    },
}


def _jurisdiction_key(profile: dict) -> str:
    attrs = profile.get("attributes") or profile
    jurs = attrs.get("jurisdictions") if isinstance(attrs, dict) else None
    if isinstance(jurs, list) and jurs:
        return "+".join(sorted(str(j) for j in jurs))
    return "unspecified"


def refresh_cohorts(engine: Engine, k: int = K) -> dict:
    buckets: dict[str, list[dict]] = {}
    with engine.connect() as conn:
        rows = conn.execute(sa.select(blueprints)).all()
    for row in rows:
        profile = row.profile if isinstance(row.profile, dict) else {}
        result = row.result if isinstance(row.result, dict) else {}
        summary = result.get("coverage_summary") or {}
        total = summary.get("total") or 0
        covered = summary.get("covered") or 0
        ratio = (covered / total) if total else None
        buckets.setdefault(_jurisdiction_key(profile), []).append({"ratio": ratio, "gaps": summary.get("gaps") or 0})

    published = 0
    synthetic = 0
    now = datetime.now(timezone.utc)
    # Always upsert the labeled synthetic so the UI is never empty-and-lying.
    with engine.begin() as conn:
        exists = conn.execute(sa.select(cohorts.c.id).where(cohorts.c.id == SYNTHETIC["id"])).first()
        values = {k: SYNTHETIC[k] for k in ("label", "n", "k_threshold", "synthetic", "published", "aggregates")}
        values["updated_at"] = now
        if exists:
            conn.execute(cohorts.update().where(cohorts.c.id == SYNTHETIC["id"]).values(**values))
        else:
            conn.execute(cohorts.insert().values(id=SYNTHETIC["id"], **values))
        synthetic = 1
        for key, items in buckets.items():
            ratios = [i["ratio"] for i in items if i["ratio"] is not None]
            meet = len(items) >= k
            cid = f"COH:{key}"
            agg = {
                "mean_coverage_ratio": round(sum(ratios) / len(ratios), 3) if meet and ratios else None,
                "mean_gaps": round(sum(i["gaps"] for i in items) / len(items), 2) if meet else None,
                "n": len(items),
            }
            rowv = dict(
                label=f"{key} peer cohort",
                n=len(items),
                k_threshold=k,
                synthetic=False,
                published=meet,
                aggregates=agg,
                updated_at=now,
            )
            exists = conn.execute(sa.select(cohorts.c.id).where(cohorts.c.id == cid)).first()
            if exists:
                conn.execute(cohorts.update().where(cohorts.c.id == cid).values(**rowv))
            else:
                conn.execute(cohorts.insert().values(id=cid, **rowv))
            if meet:
                published += 1
    try:
        from app.clhear import ai_ops

        ai_ops.record(
            engine, kind="fleet_generation", layer="L8", fleet="l8.cohorts",
            reasoning=f"Registrar: {published} k≥{k} cohorts published; synthetic demo labeled until peers exist",
            detail={"published": published, "synthetic": synthetic, "buckets": {k: len(v) for k, v in buckets.items()}},
        )
    except Exception:
        log.exception("L8 ai_ops failed")
    return {"published": published, "synthetic": synthetic, "buckets": {k: len(v) for k, v in buckets.items()}}


def list_cohorts(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(sa.select(cohorts)).mappings()]
    for r in rows:
        r["updated_at"] = str(r.get("updated_at"))
    return rows


def k_anonymity_ok(engine: Engine, k: int = K) -> tuple[bool, dict]:
    rows = list_cohorts(engine)
    real = [r for r in rows if not r.get("synthetic")]
    leaked = [r["id"] for r in real if r.get("published") and r.get("n", 0) < k]
    return not leaked, {"published_under_k": leaked, "k": k, "real_cohorts": len(real)}
