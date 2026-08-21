"""The clauses_public discipline (HLD §6.2, working rule 4).

Every code path that can emit clause text MUST go through this module (or an
explicit BYOL check, P3). On Aurora this is the `clauses_public` view and the
reader role has no grant on `clauses`; the SQLite fallback enforces the same
allow-list in the query itself. # ARCH: swap to the view when Aurora is wired.
"""
import sqlalchemy as sa

from app.clhear.l1.models import clauses


def clauses_public_select() -> sa.Select:
    """SELECT over clauses restricted to public_ok rows — the ONLY way to read
    clause text for an external caller."""
    return sa.select(
        clauses.c.id,
        clauses.c.source_version_id,
        clauses.c.ref,
        clauses.c.path,
        clauses.c.ordering,
        clauses.c.text,
        clauses.c.text_hash,
    ).where(clauses.c.public_ok.is_(True))


def clause_refs_select() -> sa.Select:
    """Refs/paths/hashes only — safe for restricted sources (no text column)."""
    return sa.select(
        clauses.c.id,
        clauses.c.source_version_id,
        clauses.c.ref,
        clauses.c.path,
        clauses.c.ordering,
        clauses.c.text_hash,
    )
