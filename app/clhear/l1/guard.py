"""Similarity guard (HLD v2 §7.3, never-list "quoting licensed text beyond the rights
basis"; item 17).

Derived outputs — obligation titles and statements (L2), block descriptions (L3) —
are *facts about* a text, not the text. For sources whose rights basis forbids
republication (``derived_only``, ``byol_only``) a derived output may not reproduce the
clause; for ``licensed`` sources a short attributed quotation is fine but a wholesale
copy is not; public-domain and open-licence text may be reproduced freely.

The measure is word-level: the longest verbatim run shared with the clause, and the
share of the output's 8-word shingles that also occur in the clause (containment).
Both are cheap, deterministic and hard to game with punctuation changes because the
text is normalised first. ``python -m app.clhear.l1.guard`` runs against the store and
exits 1 on any violation; CI runs it on every push (with a self-test on fixtures so an
empty store cannot hide a broken guard).
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.engine import Engine

SHINGLE = 8

# per rights basis: longest verbatim run (words) and shingle containment allowed in a derived output
LIMITS: dict[str, tuple[int | None, float | None]] = {
    "public_domain": (None, None),
    "open_licence": (None, None),
    "licensed": (60, 0.5),        # attributed quotation, not republication
    "byol_only": (12, 0.10),      # hashes only: a derived output must not leak the text
    "derived_only": (12, 0.10),   # facts only, no republication
}
_WORD = re.compile(r"[a-z0-9]+(?:['’][a-z]+)?")


def normalise(text: str) -> list[str]:
    return _WORD.findall((text or "").lower().replace("§", " section "))


def longest_common_run(a: list[str], b: list[str]) -> int:
    """Longest run of consecutive words shared by ``a`` and ``b`` (O(len a · len b) with a rolling row)."""
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def containment(derived: list[str], source: list[str], n: int = SHINGLE) -> float:
    """Share of the derived text's n-word shingles that appear verbatim in the source."""
    if len(derived) < n:
        return 1.0 if derived and longest_common_run(derived, source) == len(derived) else 0.0
    src = {tuple(source[i:i + n]) for i in range(len(source) - n + 1)}
    shingles = [tuple(derived[i:i + n]) for i in range(len(derived) - n + 1)]
    return sum(1 for s in shingles if s in src) / len(shingles)


@dataclass
class Verdict:
    ok: bool
    basis: str
    longest_run: int
    containment: float
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "basis": self.basis, "longest_run": self.longest_run, "containment": round(self.containment, 3), "reasons": self.reasons}


def check(derived_text: str, source_text: str, basis: str) -> Verdict:
    """Is ``derived_text`` within what ``basis`` allows relative to ``source_text``?"""
    if basis not in LIMITS:
        raise ValueError(f"unknown rights basis {basis!r}")
    d, s = normalise(derived_text), normalise(source_text)
    run, cont = longest_common_run(d, s), containment(d, s)
    max_run, max_cont = LIMITS[basis]
    reasons = []
    if max_run is not None and run > max_run:
        reasons.append(f"verbatim run of {run} words exceeds {max_run} allowed under {basis}")
    # containment is a document-level measure: an output no longer than the permitted quotation
    # is judged by the run limit alone, otherwise every short attributed quote would fail it
    if max_cont is not None and cont > max_cont and len(d) > max(SHINGLE, max_run or 0):
        reasons.append(f"{cont:.0%} of the output is copied text; {max_cont:.0%} allowed under {basis}")
    return Verdict(ok=not reasons, basis=basis, longest_run=run, containment=cont, reasons=reasons)


# --------------------------------------------------------------------------- store scan


def scan(engine: Engine, *, limit: int | None = None) -> dict:
    """Every live L2 obligation and L3 block against the clause text it was derived from,
    for sources whose basis restricts republication. Returns counts and violations."""
    from app.clhear.derived_models import asserts, blocks, obligations, requires
    from app.clhear.l1.models import clauses, source_versions, sources

    restricted = [b for b, (r, c) in LIMITS.items() if r is not None]
    checked, violations = 0, []
    with engine.connect() as conn:
        q = (sa.select(obligations.c.id, obligations.c.title, obligations.c.statement, sources.c.key, sources.c.rights_basis, clauses.c.text, clauses.c.id.label("clause_id"))
             .select_from(asserts.join(obligations, obligations.c.id == asserts.c.obligation_id)
                          .join(clauses, clauses.c.id == asserts.c.clause_id)
                          .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
                          .join(sources, sources.c.id == source_versions.c.source_id))
             .where(sources.c.rights_basis.in_(restricted), obligations.c.valid_to.is_(None)))
        if limit:
            q = q.limit(limit)
        clause_text_by_obligation: dict[str, tuple[str, str, str]] = {}
        for r in conn.execute(q).mappings():
            checked += 1
            clause_text_by_obligation.setdefault(r["id"], (r["text"] or "", r["rights_basis"], r["key"]))
            for field_name in ("title", "statement"):
                v = check(r[field_name] or "", r["text"] or "", r["rights_basis"])
                if not v.ok:
                    violations.append({"layer": "L2", "id": r["id"], "field": field_name, "source": r["key"], "clause_id": r["clause_id"], **v.as_dict()})
        # L3: a block's description against every restricted clause behind an obligation that requires it
        bq = (sa.select(blocks.c.id, blocks.c.description, blocks.c.purpose, requires.c.obligation_id)
              .select_from(requires.join(blocks, blocks.c.id == requires.c.block_id))
              .where(requires.c.valid_to.is_(None), requires.c.obligation_id.in_(list(clause_text_by_obligation) or [""])))
        for r in conn.execute(bq).mappings():
            text, basis, key = clause_text_by_obligation[r["obligation_id"]]
            checked += 1
            for field_name in ("description", "purpose"):
                v = check(r[field_name] or "", text, basis)
                if not v.ok:
                    violations.append({"layer": "L3", "id": r["id"], "field": field_name, "source": key, "via": r["obligation_id"], **v.as_dict()})
    return {"checked": checked, "violations": violations, "ok": not violations}


SELF_TEST = [
    # (derived, source, basis, expected ok)
    ("Firms must apply customer due diligence before establishing a business relationship.",
     "A relevant person must apply customer due diligence measures when the person establishes a business relationship.", "derived_only", True),
    ("A relevant person must apply customer due diligence measures when the person establishes a business relationship or carries out an occasional transaction.",
     "A relevant person must apply customer due diligence measures when the person establishes a business relationship or carries out an occasional transaction.", "derived_only", False),
    ("A relevant person must apply customer due diligence measures when the person establishes a business relationship or carries out an occasional transaction.",
     "A relevant person must apply customer due diligence measures when the person establishes a business relationship or carries out an occasional transaction.", "public_domain", True),
    ("Quoting: 'must apply customer due diligence measures when the person establishes a business relationship' (FCA).",
     "A relevant person must apply customer due diligence measures when the person establishes a business relationship or carries out an occasional transaction.", "licensed", True),
]


def self_test() -> list[str]:
    problems = []
    for derived, source, basis, expected in SELF_TEST:
        v = check(derived, source, basis)
        if v.ok != expected:
            problems.append(f"self-test: expected ok={expected} for basis={basis}: {v.reasons or 'no reasons'}")
    return problems


def main(argv: list[str] | None = None) -> int:
    import json

    argv = list(sys.argv[1:] if argv is None else argv)
    problems = self_test()
    if problems:
        print("\n".join(problems))
        return 1
    if "--self-test" in argv:
        print("similarity guard self-test OK")
        return 0
    from app.clhear.db import get_engine, run_migrations

    engine = get_engine()
    run_migrations(engine)
    result = scan(engine)
    print(json.dumps({"checked": result["checked"], "violations": len(result["violations"])}, indent=2))
    for v in result["violations"]:
        print(json.dumps(v, default=str))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
