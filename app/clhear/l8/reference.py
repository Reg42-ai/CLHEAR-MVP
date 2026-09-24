"""L8 reference benchmark: what regulators found across examined firms.

Not peer data. Each row quotes an in-force L1 clause of a public examination
report and names the L3 block the finding concerns. A curated row whose quote
is not in the current clause text is not emitted. Aggregates over member data
keep the k-anonymity gate (``l8.cohorts.K``); nothing here reads member data.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.curated import load

LABEL = "reference benchmark: regulator examination findings; not peer data"


def _clause_texts(conn, source_keys: set[str]) -> dict[str, dict]:
    from app.clhear.l1.models import clauses, source_versions, sources

    out: dict[str, dict] = {}
    rows = conn.execute(
        sa.select(sources.c.key, sources.c.canonical_url, source_versions.c.version_label, clauses.c.ref, clauses.c.text)
        .join(source_versions, source_versions.c.source_id == sources.c.id)
        .join(clauses, clauses.c.source_version_id == source_versions.c.id)
        .where(sources.c.key.in_(source_keys), source_versions.c.status == "in_force", clauses.c.valid_to.is_(None))
        .order_by(source_versions.c.id)
    ).mappings()
    for row in rows:
        out.setdefault(row["key"], {"url": row["canonical_url"], "version_label": row["version_label"], "clauses": []})
        out[row["key"]]["clauses"].append((row["ref"], row["text"] or ""))
    return out


def reference_rows(engine: Engine, *, blueprint: dict | None = None) -> list[dict]:
    from app.clhear.derived_models import blocks

    curated = load("l8_reference")
    on_blueprint = {item.get("block_id") for item in (blueprint or {}).get("items") or []}
    with engine.connect() as conn:
        texts = _clause_texts(conn, {row["source_key"] for row in curated})
        names = dict(conn.execute(sa.select(blocks.c.id, blocks.c.name).where(
            blocks.c.id.in_([row["block_id"] for row in curated if row.get("block_id")]))).all())
    out = []
    for row in curated:
        source = texts.get(row["source_key"])
        hit = next(((ref, text) for ref, text in (source or {}).get("clauses", []) if row["quote"] in text), None)
        if hit is None:
            continue
        block_id = row.get("block_id")
        out.append({
            "id": row["id"],
            "label": LABEL,
            "finding": row["finding"],
            "practice": row["practice"],
            "quote": row["quote"],
            "source": {"source_key": row["source_key"], "clause_ref": hit[0], "url": source["url"],
                       "version_label": source["version_label"]},
            "block_id": block_id,
            "block_name": names.get(block_id, "") if block_id else "",
            "on_blueprint": (block_id in on_blueprint) if blueprint is not None else None,
            "peer_data": False,
        })
    return out
