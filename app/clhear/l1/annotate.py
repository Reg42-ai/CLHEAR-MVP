"""Clause understanding layer (enrichment about the verbatim text — never the
text itself).

Tier 1 — heuristic: deterministic classification of every provision-grain
clause from stable signals (heading keywords, modal verbs, path context) plus
topics inherited from the curated source metadata. Runs inside the ingest
transaction; zero LLM.

Tier 2 — LLM explainer: plain-language summaries via the L0 gateway (fleet
`l1.annotate`, structured output, spend-capped, full model provenance).
Batched, idempotent (only clauses without an llm annotation). The output is
stored in clause_annotations and rendered clearly marked as AI-generated —
it is NOT legal text and never touches doc_nodes/clauses.
"""
import json
import logging
import re

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.l1.models import ANNOTATION_CATEGORIES, clause_annotations, clauses, source_versions, sources

log = logging.getLogger("clhear.l1.annotate")

ANNOTATE_FLEET = "l1.annotate"
BATCH_SIZE = 8

# Heading/path signals (stable, high precision): a clause HEADED
# "Interpretation" is definitions even if its body says "must".
_HEAD_RULES: list[tuple[str, re.Pattern]] = [
    ("definitions", re.compile(r"\b(definition|interpretation|meaning of|glossary)\b", re.I)),
    ("exemption", re.compile(r"\b(exempt|exception|disapplication)\b", re.I)),
    ("enforcement", re.compile(r"\b(offence|offense|penalt|sanction|enforcement|withholding of tax)", re.I)),
    ("scope", re.compile(r"\b(scope|application|applies to|extent|subject[- ]matter|prescribed)\b", re.I)),
    ("procedure", re.compile(r"\b(procedure|appeal|review|registration|notification|filing)\b", re.I)),
]

# Body signals (modal verbs; ordered so "must not" wins over "must").
_BODY_RULES: list[tuple[str, re.Pattern]] = [
    ("prohibition", re.compile(r"\b(must not|shall not|may not|prohibit|is not permitted)\b", re.I)),
    ("enforcement", re.compile(r"\b(commits an offence|liable to a fine|liable on conviction|civil penalty)\b", re.I)),
    ("obligation", re.compile(r"\b(must|shall|is required to|are required to)\b", re.I)),
    ("exemption", re.compile(r"\b(does not apply|is exempt)\b", re.I)),
]


def classify(heading: str, text: str, path: str) -> str:
    """Deterministic category from stable signals; heading outranks body."""
    head = f"{heading} {path}"
    for category, pattern in _HEAD_RULES:
        if pattern.search(head):
            return category
    for category, pattern in _BODY_RULES:
        if pattern.search(text[:2000]):
            return category
    return "administrative"


def heuristics_for_version(conn: Connection, version_id: int, source_topics: list[str]) -> int:
    """Insert origin='heuristic' annotations for every clause of a version."""
    rows = conn.execute(
        sa.select(clauses.c.id, clauses.c.ref, clauses.c.path, clauses.c.text).where(
            clauses.c.source_version_id == version_id
        )
    ).all()
    inserted = 0
    for row in rows:
        heading = row.text.split("\n", 1)[0][:200]
        category = classify(heading, row.text, row.path)
        conn.execute(
            clause_annotations.insert().values(
                clause_id=row.id,
                origin="heuristic",
                summary="",
                category=category,
                topics=source_topics,
            )
        )
        inserted += 1
    return inserted


def _pending_llm_clauses(conn: Connection, limit: int) -> list:
    """Provision clauses of in-force versions without an llm annotation."""
    annotated = sa.select(clause_annotations.c.clause_id).where(clause_annotations.c.origin == "llm")
    return conn.execute(
        sa.select(
            clauses.c.id,
            clauses.c.ref,
            clauses.c.path,
            clauses.c.text,
            sources.c.short_name,
            sources.c.key.label("source_key"),
        )
        .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
        .join(sources, sources.c.id == source_versions.c.source_id)
        .where(source_versions.c.status == "in_force")
        .where(clauses.c.public_ok.is_(True))
        .where(clauses.c.id.not_in(annotated))
        .order_by(clauses.c.id)
        .limit(limit)
    ).all()


def explain_batch(gateway, model: str, batch) -> list[dict]:
    """One gateway call explaining up to BATCH_SIZE clauses. Structured output."""
    items = [
        {"ref": row.ref, "regulation": row.short_name or row.source_key, "path": row.path, "text": row.text[:2400]}
        for row in batch
    ]
    prompt = (
        "For each clause below, explain in 1-2 plain-English sentences what it means in practice "
        "(who must do what / what it defines), classify it, and tag topics.\n"
        f"category must be one of: {', '.join(ANNOTATION_CATEGORIES)}.\n"
        'Respond with JSON only: {"annotations": [{"ref": "...", "summary": "...", '
        '"category": "...", "topics": ["..."]}]} — one entry per input clause, same refs.\n\n'
        f"{json.dumps(items, ensure_ascii=False)}"
    )
    result = gateway.call(
        fleet=ANNOTATE_FLEET,
        model=model,
        prompt=prompt,
        system=(
            "You write neutral plain-language explainers of legal/technical clauses. "
            "Never invent requirements not present in the text. JSON only."
        ),
        max_tokens=2400,
        required_keys=["annotations"],
    )
    parsed = json.loads(result.text).get("annotations", [])
    out = []
    for entry in parsed:
        if not isinstance(entry, dict) or not entry.get("ref") or not entry.get("summary"):
            continue
        category = entry.get("category", "")
        out.append(
            {
                "ref": str(entry["ref"]),
                "summary": str(entry["summary"])[:1200],
                "category": category if category in ANNOTATION_CATEGORIES else "administrative",
                "topics": [str(t) for t in entry.get("topics", [])][:8],
                "model": result.model,
                "prompt_hash": "",
            }
        )
    return out


def annotate_llm(engine: Engine, gateway, *, model: str | None = None, max_clauses: int | None = None) -> dict:
    """Batch the un-annotated corpus through the gateway. Idempotent."""
    import hashlib

    model = model or "claude-3-5-haiku-latest"
    done = 0
    batches = 0
    while True:
        with engine.connect() as conn:
            remaining = max_clauses - done if max_clauses is not None else BATCH_SIZE
            batch = _pending_llm_clauses(conn, min(BATCH_SIZE, max(0, remaining)))
        if not batch:
            break
        by_ref = {row.ref: row for row in batch}
        annotations = explain_batch(gateway, model, batch)
        batches += 1
        with engine.begin() as conn:
            for entry in annotations:
                row = by_ref.get(entry["ref"])
                if row is None:
                    continue
                conn.execute(
                    clause_annotations.insert().values(
                        clause_id=row.id,
                        origin="llm",
                        summary=entry["summary"],
                        category=entry["category"],
                        topics=entry["topics"],
                        model=entry["model"],
                        prompt_hash=hashlib.sha256(row.text.encode()).hexdigest(),
                    )
                )
                done += 1
            # Any clause the model skipped gets a placeholder-free retry next
            # run (we insert nothing for it), but we must not loop forever on
            # a clause the model refuses: mark unexplained after the batch.
            for row in batch:
                if not any(e["ref"] == row.ref for e in annotations):
                    conn.execute(
                        clause_annotations.insert().values(
                            clause_id=row.id,
                            origin="llm",
                            summary="",
                            category="administrative",
                            topics=[],
                            model=model,
                            prompt_hash="unexplained",
                        )
                    )
        if max_clauses is not None and done >= max_clauses:
            break
    log.info("llm annotation: %d clauses in %d gateway calls", done, batches)
    return {"annotated": done, "batches": batches, "model": model}
