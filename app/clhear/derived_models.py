"""Derived-layer tables (L2 obligations, L3/L5 curated catalog, L6 blueprints).

Layer schemas per HLD §2 / m0003 reservations. L2 rows are MACHINE-DERIVED
from L1 clauses (deterministic extractor, app/clhear/l2/extract.py) and carry
status `derived` until a maintainer promotes them to `validated`. L3/L5/L4
rows are CURATED policy content seeded from reviewed JSON and editable only
through the proposals queue. L6 blueprints are computed per request and
logged for replayability.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

L2_SCHEMA = "l2_obligations"
L3_SCHEMA = "l3_building_blocks"
L4_SCHEMA = "l4_profiles"
L5_SCHEMA = "l5_activities"
L6_SCHEMA = "l6_composer"

metadata = sa.MetaData()

Json = sa.JSON().with_variant(JSONB(), "postgresql")
BigId = sa.BigInteger().with_variant(sa.Integer, "sqlite")

# ------------------------------------------------------------------------ L2

obligations = sa.Table(
    "obligations",
    metadata,
    # Deterministic id: "OBL:{source_key}#{clause_ref}" — same inputs, same id.
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("source_key", sa.Text, nullable=False, index=True),
    sa.Column("clause_ref", sa.Text, nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("statement", sa.Text, nullable=False, default=""),  # empty for restricted sources
    sa.Column("addressee", sa.Text, nullable=False, default=""),
    sa.Column("modality", sa.Text, nullable=False, default=""),  # must | must-not | shall | ...
    sa.Column("jurisdiction", sa.Text, nullable=False, default=""),
    sa.Column("themes", Json, nullable=False, default=list),
    sa.Column("confidence", sa.Numeric(4, 3), nullable=False, default=0),
    sa.Column(
        "status",
        sa.Text,
        sa.CheckConstraint(
            "status in ('derived','validated','rejected','stale')", name="obligations_status_check"
        ),
        nullable=False,
        default="derived",
    ),
    sa.Column("method", sa.Text, nullable=False, default="deterministic-v1"),
    sa.Column("text_hash", sa.Text, nullable=False),  # basis clause hash at derivation time
    sa.Column("source_version_label", sa.Text, nullable=False, default=""),
    sa.Column("derived_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("validated_by", sa.Text, nullable=True),
    sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
    # HLD v2 §4.2 registry fields. ``stable_id`` is the public ``OBL-000001``
    # identifier (I11, never reused); ``id`` stays the deterministic derivation
    # key so the same corpus always derives the same registry.
    sa.Column("stable_id", sa.Text, nullable=True, unique=True),
    sa.Column("determination", sa.Text, nullable=False, default="", server_default=""),
    sa.Column("subject", sa.Text, nullable=False, default="", server_default=""),
    sa.Column("action", sa.Text, nullable=False, default="", server_default=""),
    sa.Column("condition", sa.Text, nullable=False, default="", server_default=""),
    sa.Column("object", sa.Text, nullable=False, default="", server_default=""),
    sa.Column("regulator", sa.Text, nullable=False, default="", server_default=""),
    sa.Column("obligation_type", sa.Text, nullable=False, default="", server_default=""),
    sa.Column("effective_from", sa.Date, nullable=True),
    sa.Column("effective_to", sa.Date, nullable=True),
    # Canonical obligation this row was deduplicated into (NULL = canonical itself).
    sa.Column("canonical_id", sa.Text, nullable=True),
    sa.Column("review_confidence", sa.Numeric(4, 3), nullable=True),
    schema=L2_SCHEMA,
)

# --------------------------------------------------------- L2 registry edges

OBLIGATION_TYPES = (
    "conduct", "disclosure", "reporting", "record_keeping", "governance",
    "prudential", "prohibition", "authorisation", "consumer_protection", "other",
)
ASSERT_STRENGTHS = ("explicit", "implied")
L2_CHANGE_KINDS = ("added", "updated", "revoked")

asserts = sa.Table(
    "asserts",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # AST-000001
    sa.Column("obligation_id", sa.Text, nullable=False, index=True),
    sa.Column("clause_id", BigId, nullable=False, index=True),
    sa.Column("source_key", sa.Text, nullable=False, default=""),
    sa.Column("clause_ref", sa.Text, nullable=False, default=""),
    sa.Column("span_start", sa.Integer, nullable=True),  # offsets into clauses.text
    sa.Column("span_end", sa.Integer, nullable=True),
    sa.Column(
        "strength",
        sa.Text,
        sa.CheckConstraint("strength in ('explicit','implied')", name="asserts_strength_check"),
        nullable=False,
        default="explicit",
    ),
    sa.Column("text_hash", sa.Text, nullable=False, default=""),
    schema=L2_SCHEMA,
)

equivalences = sa.Table(
    "equivalences",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # EQV-000001
    sa.Column("obligation_a", sa.Text, nullable=False, index=True),
    sa.Column("obligation_b", sa.Text, nullable=False, index=True),
    sa.Column("basis", sa.Text, nullable=False, default="lexical"),  # lexical | concept | model | human
    sa.Column("concept_id", sa.Text, nullable=True),
    sa.Column("similarity", sa.Numeric(4, 3), nullable=True),
    sa.Column("method", sa.Text, nullable=False, default=""),
    sa.UniqueConstraint("obligation_a", "obligation_b", name="equivalences_pair_unique"),
    schema=L2_SCHEMA,
)

supersessions = sa.Table(
    "supersessions",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # SUP-000001
    sa.Column("old_obligation_id", sa.Text, nullable=False, index=True),
    sa.Column("new_obligation_id", sa.Text, nullable=False, index=True),
    sa.Column("cause_change_event_id", sa.Text, nullable=True),
    sa.Column("effective_date", sa.Date, nullable=True),
    sa.Column("note", sa.Text, nullable=False, default=""),
    schema=L2_SCHEMA,
)

l2_change_events = sa.Table(
    "l2_change_events",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # CHG-000001
    sa.Column("obligation_id", sa.Text, nullable=False, index=True),
    sa.Column(
        "kind",
        sa.Text,
        sa.CheckConstraint("kind in ('added','updated','revoked')", name="l2_change_events_kind_check"),
        nullable=False,
    ),
    sa.Column("cause_clause_ids", Json, nullable=False, default=list),
    sa.Column("cause_l1_change_event_id", BigId, nullable=True, index=True),
    sa.Column("source_key", sa.Text, nullable=False, default=""),
    sa.Column("old_text_hash", sa.Text, nullable=False, default=""),
    sa.Column("new_text_hash", sa.Text, nullable=False, default=""),
    sa.Column("effective_date", sa.Date, nullable=True),
    sa.Column("effective_date_basis", sa.Text, nullable=False, default=""),
    sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("detail", Json, nullable=False, default=dict),
    schema=L2_SCHEMA,
)

obligation_reviews = sa.Table(
    "obligation_reviews",
    metadata,
    sa.Column("id", BigId, primary_key=True, autoincrement=True),
    sa.Column("obligation_id", sa.Text, nullable=False, index=True),
    sa.Column("reviewer_kind", sa.Text, nullable=False, default="model"),  # model | expert
    sa.Column("reviewer", sa.Text, nullable=False, default=""),  # model id or panel member handle
    sa.Column(
        "verdict",
        sa.Text,
        sa.CheckConstraint("verdict in ('correct','incorrect','unsure')", name="obligation_reviews_verdict_check"),
        nullable=False,
    ),
    sa.Column("text_hash", sa.Text, nullable=False, default=""),  # basis hash the verdict was given on
    sa.Column("notes", sa.Text, nullable=False, default=""),
    sa.Column("reviewed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    schema=L2_SCHEMA,
)

# --------------------------------------------------------------------- L3/L5

blocks = sa.Table(
    "blocks",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("description", sa.Text, nullable=False, default=""),
    sa.Column("capability", sa.Text, nullable=False, default=""),
    sa.Column("evidence_artifacts", Json, nullable=False, default=list),
    # Selectors {source_key, refs[]} resolved to derived obligation ids at read time.
    sa.Column("satisfies", Json, nullable=False, default=list),
    sa.Column("implements_controls", Json, nullable=False, default=list),
    sa.Column("status", sa.Text, nullable=False, default="curated"),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    schema=L3_SCHEMA,
)

activities = sa.Table(
    "activities",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("description", sa.Text, nullable=False, default=""),
    sa.Column("business_owner", sa.Text, nullable=False, default=""),
    # [{"anchor": {"source_key": ..., "refs": [...]}, "when": {attr: requirement}}]
    sa.Column("triggers", Json, nullable=False, default=list),
    sa.Column("status", sa.Text, nullable=False, default="curated"),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    schema=L5_SCHEMA,
)

# ------------------------------------------------------------------------ L4

attribute_schema = sa.Table(
    "attribute_schema",
    metadata,
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("type", sa.Text, nullable=False),  # list | bool | text
    sa.Column("description", sa.Text, nullable=False, default=""),
    # Why the attribute exists: the obligation anchors whose scope reads it.
    sa.Column("read_by", Json, nullable=False, default=list),
    schema=L4_SCHEMA,
)

sample_profiles = sa.Table(
    "sample_profiles",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("description", sa.Text, nullable=False, default=""),
    sa.Column("attributes", Json, nullable=False, default=dict),
    sa.Column("activities", Json, nullable=False, default=list),
    sa.Column("status", sa.Text, nullable=False, default="sample"),
    schema=L4_SCHEMA,
)

# Grounded license registry: every row quotes a retrieved L1 clause. The model
# never invents a permission type from general knowledge (L4 closed-world RAG).
license_types = sa.Table(
    "license_types",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # LIC:{jurisdiction}:{slug}
    sa.Column("jurisdiction", sa.Text, nullable=False, index=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("issuing_regime", sa.Text, nullable=False, default=""),
    sa.Column("clause_anchors", Json, nullable=False, default=list),  # [{source_key, ref, text_hash}]
    sa.Column("status", sa.Text, nullable=False, default="ai_generated"),
    sa.Column("generated_by", sa.Text, nullable=False, default=""),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    schema=L4_SCHEMA,
)

# ------------------------------------------------------------------------ L6

blueprints = sa.Table(
    "blueprints",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("requested_by", sa.Text, nullable=False, default=""),
    sa.Column("release", sa.Text, nullable=False, default=""),
    sa.Column("profile", Json, nullable=False, default=dict),
    sa.Column("result", Json, nullable=False, default=dict),
    sa.Column("engine_version", sa.Text, nullable=False, default=""),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    schema=L6_SCHEMA,
)

# --------------------------------------------------- L2 concepts (m0006)
# A concept is ONE representative "CLHEAR obligation" consolidating clause-
# anchored obligations across jurisdictions. It never replaces them: it is a
# resolution overlay, parameterized by the profile's jurisdiction set.

concepts = sa.Table(
    "concepts",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # "CON:<slug>"
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("canonical_statement", sa.Text, nullable=False, default=""),
    sa.Column("themes", Json, nullable=False, default=list),
    sa.Column(
        "status",
        sa.Text,
        sa.CheckConstraint("status in ('proposed','curated','flagged')", name="concepts_status_check"),
        nullable=False,
        default="proposed",
    ),
    sa.Column("drafted_by", sa.Text, nullable=False, default="human"),  # human | gateway
    sa.Column("approved_by", sa.Text, nullable=True),
    sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("flag_reason", sa.Text, nullable=False, default=""),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    schema=L2_SCHEMA,
)

concept_members = sa.Table(
    "concept_members",
    metadata,
    sa.Column("concept_id", sa.Text, nullable=False, primary_key=True),
    sa.Column("obligation_id", sa.Text, nullable=False, primary_key=True),
    sa.Column("jurisdiction", sa.Text, nullable=False, default=""),
    sa.Column(
        "role",
        sa.Text,
        sa.CheckConstraint("role in ('primary','supplementary')", name="concept_members_role_check"),
        nullable=False,
        default="primary",
    ),
    sa.Column("note", sa.Text, nullable=False, default=""),
    schema=L2_SCHEMA,
)

DERIVED_TABLES = (
    obligations, blocks, activities, attribute_schema, sample_profiles,
    blueprints, concepts, concept_members, license_types,
    asserts, equivalences, supersessions, l2_change_events, obligation_reviews,
)

from app.clhear.platform.shared_schema import attach_shared_columns as _attach  # noqa: E402

_attach(*DERIVED_TABLES)
