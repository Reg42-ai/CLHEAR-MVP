"""L4 grounded license registry — RAG, closed-world, no general knowledge.

1. Retrieve authorization-creating provisions from the ingested corpus.
2. The LLM sees ONLY retrieved clause text and must quote an anchor per type.
3. Any output whose anchor does not resolve to a retrieved clause is discarded.
4. authorisations on /v1/blueprint is a closed enum of these grounded types.
"""
from __future__ import annotations

import logging
import re

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.derived_models import license_types
from app.clhear.l1.models import clauses, source_versions, sources
from app.clhear.platform.gateway import parse_json_object
from app.clhear.platform.router import complete

log = logging.getLogger("clhear.l4.licenses")

LICENSE_QUERIES = (
    ("UK", "Part 4A permission authorisation FSMA"),
    ("UK", "electronic money authorisation e-money"),
    ("UK", "payment institution authorisation"),
    ("EU", "CASP authorisation crypto-asset service provider"),
    ("EU", "investment firm authorisation"),
    ("EU", "obliged entities anti-money laundering"),
    ("EU", "financial entities digital operational resilience"),
    ("US", "broker-dealer registration"),
)


def _retrieve(engine: Engine, query: str, limit: int = 8) -> list[dict]:
    try:
        from app.clhear.l1.retrieval import search

        hits = search(engine, query, limit=limit)
    except Exception:
        log.exception("license retrieval failed for %s", query)
        hits = []
    out = []
    for h in hits:
        out.append({
            "source_key": h.get("source_key") or h.get("key"),
            "ref": h.get("ref") or h.get("clause_ref"),
            "text": h.get("text") or h.get("snippet") or "",
            "text_hash": h.get("text_hash") or "",
        })
    # Fallback: LIKE over public clauses when the search index is empty.
    if not out:
        like = f"%{query.split()[0]}%"
        with engine.connect() as conn:
            rows = conn.execute(
                sa.select(sources.c.key, clauses.c.ref, clauses.c.text, clauses.c.text_hash)
                .join(source_versions, source_versions.c.source_id == sources.c.id)
                .join(clauses, clauses.c.source_version_id == source_versions.c.id)
                .where(source_versions.c.status == "in_force")
                .where(sources.c.license == "open")
                .where(clauses.c.public_ok.is_(True))
                .where(clauses.c.text.ilike(like))
                .limit(limit)
            ).all()
        out = [
            {"source_key": r.key, "ref": r.ref, "text": r.text or "", "text_hash": r.text_hash}
            for r in rows
        ]
    return [h for h in out if h.get("source_key") and h.get("ref")]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50]


def anchor_is_live(engine: Engine, source_key: str, ref: str) -> bool:
    """Same resolution the l4_grounding eval uses — in-force clause or discard."""
    if not source_key or not ref:
        return False
    with engine.connect() as conn:
        hit = conn.execute(
            sa.select(clauses.c.id)
            .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
            .join(sources, sources.c.id == source_versions.c.source_id)
            .where(sources.c.key == source_key)
            .where(source_versions.c.status == "in_force")
            .where(clauses.c.ref == ref)
            .limit(1)
        ).first()
    return hit is not None


def extract_licenses(engine: Engine, llm) -> dict:
    written = discarded = 0
    ids: list[str] = []
    coverage_gaps: list[str] = []
    for jur, query in LICENSE_QUERIES:
        retrieved = [h for h in _retrieve(engine, query) if anchor_is_live(engine, h["source_key"], h["ref"])]
        if not retrieved:
            coverage_gaps.append(f"{jur}:{query}")
            continue
        allowed = {(h["source_key"], h["ref"]) for h in retrieved}
        corpus = "\n\n".join(
            f"[{h['source_key']}#{h['ref']}]\n{h['text'][:800]}" for h in retrieved
        )
        prompt = (
            f"Extract authorization / license TYPES that this {jur} text creates. "
            "You may ONLY use the clauses below. Every type MUST quote source_key and ref "
            "from the brackets. Do not use general knowledge.\n"
            'JSON: {"license_types": [{"name": "", "issuing_regime": "", '
            '"source_key": "", "ref": ""}]}\n\n'
            + corpus
        )
        try:
            result = complete(
                llm, "l4.license_extract",
                prompt=prompt,
                system="Extractive only. If a type is not in the text, omit it. JSON only.",
                required_keys=["license_types"],
                max_tokens=800,
            )
            parsed = parse_json_object(result.text)
        except Exception:
            log.exception("license extract failed for %s", query)
            discarded += 1
            continue
        for item in parsed.get("license_types") or []:
            if not isinstance(item, dict):
                discarded += 1
                continue
            key, ref = item.get("source_key"), item.get("ref")
            if (key, ref) not in allowed:
                discarded += 1
                continue
            hit = next(h for h in retrieved if h["source_key"] == key and h["ref"] == ref)
            name = str(item.get("name") or "").strip()
            if not name:
                discarded += 1
                continue
            lid = f"LIC:{jur}:{_slug(name)}"
            anchors = [{"source_key": key, "ref": ref, "text_hash": hit["text_hash"]}]
            with engine.begin() as conn:
                exists = conn.execute(sa.select(license_types.c.id).where(license_types.c.id == lid)).first()
                values = dict(
                    jurisdiction=jur, name=name,
                    issuing_regime=str(item.get("issuing_regime") or "")[:200],
                    clause_anchors=anchors, status="ai_generated",
                    generated_by=result.model,
                )
                if exists:
                    conn.execute(license_types.update().where(license_types.c.id == lid).values(**values))
                else:
                    conn.execute(license_types.insert().values(id=lid, **values))
            from app.clhear.governance import mark_generated

            mark_generated(
                engine, layer="L4", subject_ref=lid, generated_by=result.model,
                routing_reason="closed-world license RAG", detail={"anchors": anchors},
            )
            written += 1
            ids.append(lid)
    try:
        from app.clhear import ai_ops

        ai_ops.record(
            engine, kind="fleet_generation", layer="L4", fleet="l4.licenses",
            reasoning=f"Surveyor: {written} grounded license types; {discarded} ungrounded discarded; "
            f"{len(coverage_gaps)} coverage gaps (incomplete, never invented)",
            detail={"written": written, "discarded": discarded, "coverage_gaps": coverage_gaps, "ids": ids},
        )
    except Exception:
        log.exception("L4 ai_ops failed")
    return {"written": written, "discarded": discarded, "coverage_gaps": coverage_gaps, "ids": ids}


def list_license_types(engine: Engine, jurisdiction: str | None = None) -> list[dict]:
    stmt = sa.select(license_types)
    if jurisdiction:
        stmt = stmt.where(license_types.c.jurisdiction == jurisdiction)
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings()]


def grounded_enum(engine: Engine) -> dict[str, list[str]]:
    """jurisdiction → list of grounded license type names."""
    by: dict[str, list[str]] = {}
    for row in list_license_types(engine):
        by.setdefault(row["jurisdiction"], []).append(row["name"])
    return by


def validate_authorisations(engine: Engine, attributes: dict) -> None:
    """Raise ValueError if any authorisation is not in the grounded registry.

    Incomplete registry is allowed (no types for a jurisdiction → that
    jurisdiction cannot claim invented licenses). Empty authorisations always OK.
    """
    wanted = attributes.get("authorisations") or []
    if not wanted:
        return
    if not isinstance(wanted, list):
        raise ValueError("attributes.authorisations must be a list of grounded license types")
    known = {row["name"] for row in list_license_types(engine)}
    known |= {row["id"] for row in list_license_types(engine)}
    if not known:
        # Incomplete registry: do not invent a block and do not invent types.
        # Free-text authorisations stay unchecked until the fleet grounds some.
        return
    unknown = [a for a in wanted if a not in known]
    if unknown:
        raise ValueError(f"ungrounded authorisations (not in L4 license registry): {unknown}")
