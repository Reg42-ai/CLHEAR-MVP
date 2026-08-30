"""Nightly consolidation: propose cross-jurisdiction concept groupings.

Deterministic blocking finds candidate groups (same theme, lexically similar
duty text, DIFFERENT jurisdictions); the gateway drafts the representative
name + canonical statement as a structured, capped LLM call; the output is
ALWAYS a proposal in the l0 review queue — a human writes it into the live
concept table by approving (HLD: agents propose, humans ratify).
"""
from __future__ import annotations

import json
import logging
import re

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.derived_models import obligations
from app.clhear.l2.concepts import consolidated_ids, upsert_concept
from app.clhear.models import proposals as proposals_t
from app.clhear.platform import proposals as l0_proposals
from app.clhear.platform.gateway import Gateway

log = logging.getLogger("clhear.l2.consolidate")

MAX_CANDIDATES_PER_RUN = 5
MIN_SIMILARITY = 0.30
MAX_GROUP = 6

_STOP = frozenset(
    "a an and are as at be by for from has have if in is it its may must not of on or "
    "shall should such that the their there this to under where which with within".split()
)


def _tokens(text: str) -> frozenset[str]:
    return frozenset(t for t in re.findall(r"[a-z]{3,}", text.lower()) if t not in _STOP)


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_candidates(engine: Engine, limit: int = MAX_CANDIDATES_PER_RUN) -> list[list[dict]]:
    """Cross-jurisdiction groups of unconsolidated, live obligations."""
    done = consolidated_ids(engine)
    with engine.connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                sa.select(
                    obligations.c.id, obligations.c.title, obligations.c.statement,
                    obligations.c.jurisdiction, obligations.c.themes, obligations.c.source_key,
                ).where(obligations.c.status.in_(("derived", "validated")))
            ).mappings()
        ]
    rows = [r for r in rows if r["id"] not in done and r["jurisdiction"]]
    for r in rows:
        r["_tok"] = _tokens(f"{r['title']} {r['statement']}")
        r["_themes"] = set(r["themes"] if isinstance(r["themes"], list) else [])

    groups: list[list[dict]] = []
    used: set[str] = set()
    # Deterministic order: strongest anchors first (longer statements = richer signal).
    for seed in sorted(rows, key=lambda r: (-len(r["_tok"]), r["id"])):
        if seed["id"] in used or len(groups) >= limit:
            continue
        group = [seed]
        for other in sorted(rows, key=lambda r: r["id"]):
            if other["id"] in used or other["id"] == seed["id"] or len(group) >= MAX_GROUP:
                continue
            if other["jurisdiction"] == seed["jurisdiction"]:
                continue  # consolidation is cross-jurisdiction by definition
            if seed["_themes"] and other["_themes"] and not (seed["_themes"] & other["_themes"]):
                continue
            if _jaccard(seed["_tok"], other["_tok"]) >= MIN_SIMILARITY:
                group.append(other)
        if len({g["jurisdiction"] for g in group}) >= 2:
            groups.append(group)
            used.update(g["id"] for g in group)
    return groups


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def _already_proposed(engine: Engine, member_ids: list[str]) -> bool:
    with engine.connect() as conn:
        for row in conn.execute(
            sa.select(proposals_t.c.draft).where(proposals_t.c.kind == "l2_concept").where(
                proposals_t.c.status == "proposed"
            )
        ):
            draft = row.draft if isinstance(row.draft, dict) else json.loads(row.draft or "{}")
            if set(m["obligation_id"] for m in draft.get("members", [])) & set(member_ids):
                return True
    return False


def draft_and_propose(engine: Engine, gateway: Gateway | None, limit: int = MAX_CANDIDATES_PER_RUN) -> dict:
    proposed = skipped = drafted_llm = 0
    for group in find_candidates(engine, limit=limit):
        member_ids = [g["id"] for g in group]
        if _already_proposed(engine, member_ids):
            skipped += 1
            continue
        name = group[0]["title"]
        canonical = ""
        notes: dict[str, str] = {}
        if gateway is not None:
            try:
                prompt = (
                    "These regulatory obligations from different jurisdictions appear to impose the "
                    "same underlying duty. Draft ONE representative consolidation. Respond with JSON: "
                    '{"name": <=90 chars imperative title, "canonical_statement": <=350 chars neutral '
                    "restatement that is NOT verbatim from any text, \"member_notes\": {obligation_id: "
                    "<=80 chars on what this jurisdiction adds}}.\n\n"
                    + "\n\n".join(
                        f"[{g['id']}] ({g['jurisdiction']}, {g['source_key']})\n{g['title']}\n{g['statement'][:400]}"
                        for g in group
                    )
                )
                result = gateway.call(
                    fleet="l2.consolidate",
                    model="claude-3-5-haiku-latest",
                    prompt=prompt,
                    system="You consolidate legal obligations. JSON only. Never copy sentences verbatim.",
                    required_keys=["name", "canonical_statement", "member_notes"],
                    max_tokens=700,
                )
                parsed = json.loads(result.text)
                name = str(parsed["name"])[:120]
                canonical = str(parsed["canonical_statement"])[:500]
                notes = {str(k): str(v)[:120] for k, v in dict(parsed.get("member_notes", {})).items()}
                drafted_llm += 1
            except Exception:
                log.exception("gateway drafting failed; proposing structure-only candidate")
        concept_id = f"CON:{_slug(name)}"
        draft = {
            "id": concept_id,
            "name": name,
            "canonical_statement": canonical,
            "themes": sorted({t for g in group for t in (g["themes"] if isinstance(g["themes"], list) else [])}),
            "drafted_by": "gateway" if canonical else "candidate-only",
            "members": [
                {"obligation_id": g["id"], "jurisdiction": g["jurisdiction"], "role": "primary",
                 "note": notes.get(g["id"], "")}
                for g in group
            ],
        }
        with engine.begin() as conn:
            l0_proposals.create_proposal(
                conn,
                layer="l2",
                kind="l2_concept",
                subject_ref=concept_id,
                draft=draft,
                rationale=f"cross-jurisdiction consolidation candidate ({len(group)} obligations, "
                f"{len({g['jurisdiction'] for g in group})} jurisdictions)",
                confidence=0.6 if canonical else 0.4,
            )
        proposed += 1
    return {"proposed": proposed, "skipped_already_proposed": skipped, "llm_drafted": drafted_llm}


def apply_approved_concept(engine: Engine, proposal: dict) -> dict:
    """Called when an l2_concept proposal is approved in the review console."""
    draft = proposal.get("draft") or {}
    if isinstance(draft, str):
        draft = json.loads(draft)
    return upsert_concept(
        engine,
        concept_id=draft["id"],
        name=draft["name"],
        canonical_statement=draft.get("canonical_statement", ""),
        themes=draft.get("themes", []),
        members=draft.get("members", []),
        status="curated",
        drafted_by=draft.get("drafted_by", "gateway"),
        approved_by=proposal.get("approver"),
    )
