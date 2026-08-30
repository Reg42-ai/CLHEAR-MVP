"""L2 obligation extraction — deterministic, clause-anchored (HLD principle 2).

One obligation per duty-bearing clause, id = "OBL:{source_key}#{ref}" so the
same corpus always derives the same registry. No LLM in this path: duty
detection is lexical + structural; anything the rules cannot decide simply is
NOT an obligation yet (community/maintainer review can add it later via the
proposals queue). Restricted sources contribute refs + hashes, never text.

Every row stores the basis clause hash; when L1 detects a change on the basis
clause, the obligation is re-derived (or marked stale) on the nightly run.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.derived_models import obligations
from app.clhear.l1.models import clauses, family_members, source_versions, sources

log = logging.getLogger("clhear.l2")

EXTRACTOR_VERSION = "deterministic-v1"

# Duty modality patterns, strongest first. Case-insensitive, matched against
# the clause text. Deliberately conservative: high precision over recall.
MODALITY_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("must-not", re.compile(r"\b(?:must not|shall not|may not)\b", re.I)),
    ("must", re.compile(r"\bmust\b", re.I)),
    ("shall", re.compile(r"\bshall\b", re.I)),
    ("required", re.compile(r"\b(?:is|are) (?:required|obliged|obligated) to\b", re.I)),
    ("ensure", re.compile(r"\b(?:is|are) responsible for ensuring\b", re.I)),
)

# Clauses that carry structure, not duties.
NON_DUTY_HEADINGS = re.compile(
    r"\b(?:interpretation|definitions?|citation|commencement|extent|title|scope|"
    r"subject[- ]matter|entry into force|transitional|amendments? to|repeals?|"
    r"short title|signature|annex|recital)\b",
    re.I,
)

# Addressee: the noun phrase directly before the first modal.
ADDRESSEE = re.compile(
    r"(?:^|\.\s+)(?:\d+[\.\)]\s*)?(?:\([\w\d]+\)\s*)*(?:each|every|an?|the)\s+"
    r"([A-Za-z][\w\s\-,']{2,80}?)\s+(?:must|shall|may not|is required|are required)",
    re.I,
)

MAX_STATEMENT = 480


@dataclass
class Candidate:
    source_key: str
    ref: str
    title: str
    statement: str
    addressee: str
    modality: str
    confidence: float
    text_hash: str
    public: bool


def _title_from(text: str, ref: str) -> str:
    first = text.strip().split("\n", 1)[0].strip()
    first = re.sub(r"\s+", " ", first)
    if len(first) > 110:
        first = first[:107].rsplit(" ", 1)[0] + "…"
    return first or ref


def detect_duty(text: str, ref: str, heading: str = "") -> tuple[str, float] | None:
    """Return (modality, confidence) when the clause imposes a duty."""
    if not text or len(text.strip()) < 40:
        return None
    probe = f"{heading} {ref}"
    if NON_DUTY_HEADINGS.search(probe) or NON_DUTY_HEADINGS.search(text[:120]):
        return None
    for modality, pattern in MODALITY_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        confidence = 0.85 if modality in ("must", "must-not") else 0.75
        # Duty stated early in the clause is a stronger signal than one buried
        # in a proviso; definitions sneak modals into subordinate positions.
        if match.start() > len(text) * 0.6:
            confidence -= 0.15
        if len(text) < 120:
            confidence -= 0.1
        return modality, round(confidence, 2)
    return None


def extract_source(engine: Engine, source_row, version_row) -> list[Candidate]:
    """Candidates for one in-force source version. Binding tier only."""
    open_source = source_row.license == "open"
    out: list[Candidate] = []
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(clauses)
            .where(clauses.c.source_version_id == version_row.id)
            .order_by(clauses.c.ordering)
        ).all()
    for row in rows:
        text = row.text or ""
        if not open_source or not row.public_ok:
            # Restricted: we cannot inspect text; no machine derivation.
            continue
        duty = detect_duty(text, row.ref or "", "")
        if duty is None:
            continue
        modality, confidence = duty
        addressee_match = ADDRESSEE.search(text)
        statement = re.sub(r"\s+", " ", text).strip()
        if len(statement) > MAX_STATEMENT:
            statement = statement[: MAX_STATEMENT - 1].rsplit(" ", 1)[0] + "…"
        out.append(
            Candidate(
                source_key=source_row.key,
                ref=row.ref or f"clause-{row.ordering}",
                title=_title_from(text, row.ref or ""),
                statement=statement,
                addressee=(addressee_match.group(1).strip() if addressee_match else ""),
                modality=modality,
                confidence=confidence,
                text_hash=row.text_hash,
                public=True,
            )
        )
    return out


def obligation_id(source_key: str, ref: str) -> str:
    return f"OBL:{source_key}#{ref}"


def run_extraction(engine: Engine, source_key: str | None = None) -> dict:
    """(Re-)derive the obligation registry. Idempotent: deterministic ids;
    unchanged basis hash + same extractor version = untouched row (validated
    rows keep their status); changed basis = re-derived + status reset to
    `derived`; vanished basis = status `stale`."""
    themes_by_source: dict[str, list] = {}
    inserted = updated = unchanged = staled = 0
    with engine.connect() as conn:
        src_q = sa.select(sources)
        if source_key:
            src_q = src_q.where(sources.c.key == source_key)
        source_rows = conn.execute(src_q).all()
        binding = {
            row.source_id
            for row in conn.execute(sa.select(family_members).where(family_members.c.tier == "binding"))
        }
        versions = {
            v.source_id: v
            for v in conn.execute(
                sa.select(source_versions).where(source_versions.c.status == "in_force").order_by(source_versions.c.id)
            )
        }
        for s in source_rows:
            themes_by_source[s.key] = s.topics if isinstance(s.topics, list) else []

    all_candidates: list[Candidate] = []
    scoped_keys: list[str] = []
    for s in source_rows:
        if s.id not in binding or s.id not in versions:
            continue
        scoped_keys.append(s.key)
        all_candidates.extend(extract_source(engine, s, versions[s.id]))

    jurisdictions = {s.key: s.jurisdiction for s in source_rows}
    version_labels = {s.key: versions[s.id].version_label for s in source_rows if s.id in versions}

    with engine.begin() as conn:
        existing = {
            row.id: row
            for row in conn.execute(
                sa.select(obligations).where(obligations.c.source_key.in_(scoped_keys))
                if scoped_keys
                else sa.select(obligations)
            )
        }
        seen: set[str] = set()
        for cand in all_candidates:
            oid = obligation_id(cand.source_key, cand.ref)
            seen.add(oid)
            row = existing.get(oid)
            values = dict(
                source_key=cand.source_key,
                clause_ref=cand.ref,
                title=cand.title,
                statement=cand.statement,
                addressee=cand.addressee,
                modality=cand.modality,
                jurisdiction=jurisdictions.get(cand.source_key, ""),
                themes=themes_by_source.get(cand.source_key, []),
                confidence=cand.confidence,
                method=EXTRACTOR_VERSION,
                text_hash=cand.text_hash,
                source_version_label=version_labels.get(cand.source_key, ""),
            )
            if row is None:
                conn.execute(obligations.insert().values(id=oid, status="derived", **values))
                inserted += 1
            elif row.text_hash != cand.text_hash or row.method != EXTRACTOR_VERSION:
                # Basis clause changed (or extractor upgraded): re-derive.
                conn.execute(
                    obligations.update()
                    .where(obligations.c.id == oid)
                    .values(status="derived", validated_by=None, validated_at=None, **values)
                )
                updated += 1
            else:
                unchanged += 1
        for oid, row in existing.items():
            if oid not in seen and row.status != "stale":
                conn.execute(obligations.update().where(obligations.c.id == oid).values(status="stale"))
                staled += 1
    summary = {
        "extractor": EXTRACTOR_VERSION,
        "sources_scanned": len(scoped_keys),
        "candidates": len(all_candidates),
        "inserted": inserted,
        "re_derived": updated,
        "unchanged": unchanged,
        "stale": staled,
    }
    log.info("L2 extraction: %s", summary)
    return summary
