"""L2 obligation registry schema (HLD v2 §4.2).

The tables live in ``app.clhear.derived_models`` (one metadata for every
derived layer); this module is the L2-facing import surface:

    obligations         OBL- rows: determination text, subject / action /
                        condition / object, jurisdictions, regulator, type,
                        effective dates; ``canonical_id`` after dedupe
    asserts             obligation <- clause edges with span + strength
    equivalences        obligation ~ obligation across jurisdictions
    supersessions       old obligation -> new obligation (+ cause)
    l2_change_events    added / updated / revoked with cause clause ids + dates
    obligation_reviews  second-model / expert verdicts feeding precision
"""
from app.clhear.derived_models import (
    ASSERT_STRENGTHS,
    L2_CHANGE_KINDS,
    L2_SCHEMA,
    OBLIGATION_TYPES,
    asserts,
    concept_members,
    concepts,
    equivalences,
    l2_change_events,
    obligation_reviews,
    obligations,
    supersessions,
)

L2_TABLES = (obligations, asserts, equivalences, supersessions, l2_change_events, obligation_reviews)

__all__ = [
    "ASSERT_STRENGTHS",
    "L2_CHANGE_KINDS",
    "L2_SCHEMA",
    "L2_TABLES",
    "OBLIGATION_TYPES",
    "asserts",
    "concept_members",
    "concepts",
    "equivalences",
    "l2_change_events",
    "obligation_reviews",
    "obligations",
    "supersessions",
]
