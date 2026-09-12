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
)
