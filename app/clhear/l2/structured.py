"""Structured extractor (task ``l2.extract``, HLD v2 §4.2).

The deterministic parser fills subject / action / condition / object for the
common "<subject> must <action> [<condition>]" shape. Clauses it cannot
split (empty subject or action) go to the model with a strict JSON schema.
Grounding contract: ≥ 80 % of the content words in every returned field must
occur in the clause text — the model may re-arrange the clause, never add to
it. Ungrounded answers are discarded and the row keeps its deterministic
fields.
"""
from __future__ import annotations

import logging
import re

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.derived_models import OBLIGATION_TYPES, asserts, obligations
from app.clhear.l1.models import clauses
from app.clhear.l2 import registry
from app.clhear.platform.gateway import ALTERNATIVES_KEY, parse_json_object
from app.clhear.platform.router import complete

log = logging.getLogger("clhear.l2.structured")

MAX_PER_RUN = 25
GROUNDING_MIN = 0.8
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset({"the", "a", "an", "of", "to", "and", "or", "in", "on", "for", "by", "with", "that", "which",
                   "its", "their", "such", "any", "all", "as", "at", "be", "is", "are", "it", "this"})

FIELDS = ("subject", "action", "condition", "object")


def grounded(field_text: str, clause_text: str, minimum: float = GROUNDING_MIN) -> bool:
    words = [w for w in _WORD.findall((field_text or "").lower()) if w not in _STOP]
    if not words:
        return True  # empty field is allowed (e.g. no condition)
    hay = set(_WORD.findall((clause_text or "").lower()))
    return sum(1 for w in words if w in hay) / len(words) >= minimum


def _pick(parsed: dict, statement: str) -> dict:
    """When the model enumerated several duties, keep the one closest to the statement."""
    options = parsed.get(ALTERNATIVES_KEY)
    if not isinstance(options, list) or len(options) < 2:
        return parsed
    target = set(_WORD.findall((statement or "").lower())) - _STOP

    def overlap(o: dict) -> int:
        text = " ".join(str(o.get(k) or "") for k in FIELDS).lower()
        return len(target & set(_WORD.findall(text)))

    return max((o for o in options if isinstance(o, dict)), key=overlap)


def _candidates(conn, limit: int) -> list[dict]:
    out = []
    for ob in conn.execute(
        sa.select(obligations)
        .where(obligations.c.status.in_(("derived", "validated")))
        .where(sa.or_(obligations.c.subject == "", obligations.c.action == ""))
        .where(obligations.c.statement != "")
        .order_by(obligations.c.id)
        .limit(limit)
    ).mappings():
        clause = conn.execute(
            sa.select(clauses.c.text)
            .join(asserts, asserts.c.clause_id == clauses.c.id)
            .where(asserts.c.obligation_id == ob["id"])
            .where(asserts.c.valid_to.is_(None))
            .limit(1)
        ).scalar()
        out.append({**dict(ob), "clause_text": clause or ob["statement"]})
    return out


def refine_structured(engine: Engine, llm, limit: int = MAX_PER_RUN) -> dict:
    refined = rejected = 0
    with engine.connect() as conn:
        batch = _candidates(conn, limit)
    for ob in batch:
        prompt = (
            "Restructure ONE obligation from this regulatory clause. The clause may contain several duties; "
            "describe only the one matching the STATEMENT. Use ONLY words from the clause. "
            "Return exactly one JSON object, never an array: "
            '{"subject": "who is bound", "action": "what they must do", "condition": "when/if (or empty)", '
            '"object": "what the action is about (or empty)", "obligation_type": one of '
            + "|".join(OBLIGATION_TYPES) + "}\n\nSTATEMENT:\n" + (ob["statement"] or "")[:600]
            + "\n\nCLAUSE:\n" + (ob["clause_text"] or "")[:3000]
        )
        try:
            result = complete(
                llm, "l2.extract", prompt=prompt,
                system="You restructure legal text without adding to it. JSON only.",
                required_keys=["subject", "action"], max_tokens=400,
            )
            parsed = _pick(parse_json_object(result.text), ob["statement"])
        except Exception:
            log.exception("structured extraction failed for %s", ob["id"])
            rejected += 1
            continue
        fields = {k: " ".join(str(parsed.get(k) or "").split())[:400] for k in FIELDS}
        if not fields["subject"] or not fields["action"]:
            rejected += 1
            continue
        if not all(grounded(fields[k], ob["clause_text"]) for k in FIELDS):
            rejected += 1
            continue
        otype = str(parsed.get("obligation_type") or "").lower()
        if otype not in OBLIGATION_TYPES:
            otype = ob["obligation_type"] or registry.classify_type(ob["clause_text"], ob["modality"])
        structure = {**fields, "modal": ob["modality"].replace("-", " ") if ob["modality"] else "must"}
        determination = registry.determination_text(structure, fallback=ob["statement"])
        with engine.begin() as conn:
            why = registry.why_for(
                ob["id"], clause_id=None, text_hash=ob["text_hash"], method="l2.extract.structured",
                confidence=float(ob["confidence"] or 0) or None,
                summary=f"structured split by {result.model}; every field grounded ≥ {int(GROUNDING_MIN * 100)}% in the clause",
                model_manifest={"model": result.model, "task": "l2.extract"},
            )
            trail = why.write(conn)
            conn.execute(
                obligations.update().where(obligations.c.id == ob["id"]).values(
                    **fields, obligation_type=otype, determination=determination, why_trail_id=trail,
                    model_manifest={"model": result.model, "task": "l2.extract"},
                )
            )
        refined += 1
    return {"refined": refined, "rejected": rejected, "examined": len(batch)}


__all__ = ["grounded", "refine_structured"]
