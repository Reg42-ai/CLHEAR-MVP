"""The clauses_public / nodes_public discipline (HLD §6.2, working rule 4).

Every code path that can emit clause text, raw_text, or a source_fragment
MUST go through this module (or an explicit BYOL check, P3). On Aurora this
is the `clauses_public` view and the reader role has no grant on the raw
tables; the SQLite fallback enforces the same allow-list in the query
itself. # ARCH: swap to the view when Aurora is wired.
"""
import sqlalchemy as sa

from app.clhear.l1.models import clauses, doc_nodes


def clauses_public_select() -> sa.Select:
    """SELECT over clauses restricted to public_ok rows — the ONLY way to read
    clause text for an external caller."""
    return sa.select(
        clauses.c.id,
        clauses.c.source_version_id,
        clauses.c.doc_node_id,
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
        clauses.c.doc_node_id,
        clauses.c.ref,
        clauses.c.path,
        clauses.c.ordering,
        clauses.c.text_hash,
    )


def nodes_public_select() -> sa.Select:
    """Document reconstruction rows with raw_text (public_ok only)."""
    return sa.select(
        doc_nodes.c.id,
        doc_nodes.c.parent_id,
        doc_nodes.c.seq,
        doc_nodes.c.depth,
        doc_nodes.c.node_type,
        doc_nodes.c.ref,
        doc_nodes.c.label,
        doc_nodes.c.heading,
        doc_nodes.c.raw_text,
        doc_nodes.c.text_hash,
        doc_nodes.c.public_ok,
        doc_nodes.c.source_version_id,
    ).where(doc_nodes.c.public_ok.is_(True))


def nodes_refs_select() -> sa.Select:
    """Structure/refs/hashes only — no raw_text, no source_fragment."""
    return sa.select(
        doc_nodes.c.id,
        doc_nodes.c.parent_id,
        doc_nodes.c.seq,
        doc_nodes.c.depth,
        doc_nodes.c.node_type,
        doc_nodes.c.ref,
        doc_nodes.c.label,
        doc_nodes.c.heading,
        doc_nodes.c.text_hash,
        doc_nodes.c.public_ok,
        doc_nodes.c.source_version_id,
    )
