"""l1_sources schema (HLD §6.2) as SQLAlchemy Core metadata.

Same pattern as the l0 models: the metadata carries the `l1_sources` schema
name; on SQLite the engine translates the schema away.
"""
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

L1_SCHEMA = "l1_sources"

metadata = sa.MetaData(schema=L1_SCHEMA)

Json = sa.JSON().with_variant(JSONB(), "postgresql")

BigId = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def _uuid() -> str:
    return str(uuid.uuid4())


source_families = sa.Table(
    "source_families",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("key", sa.Text, nullable=False, unique=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("scope_charter", Json, nullable=False, default=dict),
)

sources = sa.Table(
    "sources",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("family_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.source_families.id"), nullable=False),
    sa.Column("key", sa.Text, nullable=False, unique=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column(
        "kind",
        sa.Text,
        sa.CheckConstraint(
            "kind in ('law','regulation','standard','guidance','form','agreement')",
            name="sources_kind_check",
        ),
        nullable=False,
    ),
    sa.Column("issuer", sa.Text, nullable=False, default=""),
    sa.Column("jurisdiction", sa.Text, nullable=False, default=""),
    sa.Column(
        "license",
        sa.Text,
        sa.CheckConstraint("license in ('open','restricted')", name="sources_license_check"),
        nullable=False,
        default="open",
    ),
    sa.Column("license_ref", sa.Text, nullable=False, default=""),
    sa.Column("adapter", sa.Text, nullable=False, default=""),
    sa.Column("canonical_url", sa.Text, nullable=False, default=""),
)

family_members = sa.Table(
    "family_members",
    metadata,
    sa.Column("family_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.source_families.id"), primary_key=True),
    sa.Column("source_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.sources.id"), primary_key=True),
    sa.Column(
        "relation",
        sa.Text,
        sa.CheckConstraint(
            "relation in ('root','amends','consolidates','corrects','supplements','interprets','implements')",
            name="family_members_relation_check",
        ),
        nullable=False,
    ),
    sa.Column(
        "tier",
        sa.Text,
        sa.CheckConstraint("tier in ('binding','guidance','informative')", name="family_members_tier_check"),
        nullable=False,
        default="binding",
    ),
    sa.Column(
        "status",
        sa.Text,
        sa.CheckConstraint("status in ('active','superseded')", name="family_members_status_check"),
        nullable=False,
        default="active",
    ),
    sa.Column(
        "added_via",
        sa.Text,
        sa.CheckConstraint(
            "added_via in ('citator','citation','watchlist','manual')", name="family_members_added_via_check"
        ),
        nullable=False,
        default="manual",
    ),
)

source_versions = sa.Table(
    "source_versions",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("source_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.sources.id"), nullable=False),
    sa.Column("version_label", sa.Text, nullable=False),
    sa.Column("effective_date", sa.Date, nullable=True),
    sa.Column("retrieved_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("s3_uri", sa.Text, nullable=False, default=""),
    sa.Column("content_hash", sa.Text, nullable=False),
    sa.Column(
        "status",
        sa.Text,
        sa.CheckConstraint("status in ('in_force','superseded','revoked')", name="source_versions_status_check"),
        nullable=False,
        default="in_force",
    ),
    sa.UniqueConstraint("source_id", "version_label", name="source_versions_source_version_key"),
)

# ARCH: HLD 6.2 specifies embedding vector(1024) (pgvector on Aurora). Embeddings
# land with the P2 batch job; until Aurora is wired the column is JSON so the
# SQLite fallback keeps dev/tests offline. Swap to Vector(1024) with m000N.
clauses = sa.Table(
    "clauses",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("source_version_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.source_versions.id"), nullable=False),
    sa.Column("ref", sa.Text, nullable=False),
    sa.Column("path", sa.Text, nullable=False, default=""),
    sa.Column("ordering", sa.Integer, nullable=False),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("text_hash", sa.Text, nullable=False),
    sa.Column("public_ok", sa.Boolean, nullable=False, default=False),
    sa.Column("embedding", Json, nullable=True),
    sa.Column("embedding_model", sa.Text, nullable=True),
    sa.Index("clauses_source_version_idx", "source_version_id"),
)

citations = sa.Table(
    "citations",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("from_clause_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.clauses.id"), nullable=False),
    sa.Column("raw_text", sa.Text, nullable=False),
    sa.Column("resolved_source_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.sources.id"), nullable=True),
    sa.Column(
        "disposition",
        sa.Text,
        sa.CheckConstraint(
            "disposition in ('resolved','out_of_scope','open')", name="citations_disposition_check"
        ),
        nullable=False,
        default="open",
    ),
    sa.Column("reason", sa.Text, nullable=False, default=""),
)

# Accept/reject happens via l0.proposals (HLD §6.2).
discovery_candidates = sa.Table(
    "discovery_candidates",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("family_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.source_families.id"), nullable=False),
    sa.Column("url", sa.Text, nullable=False),
    sa.Column("title", sa.Text, nullable=False, default=""),
    sa.Column("found_via", sa.Text, nullable=False, default=""),
    sa.Column("classification", Json, nullable=False, default=dict),
    sa.Column(
        "status",
        sa.Text,
        sa.CheckConstraint(
            "status in ('proposed','accepted','rejected')", name="discovery_candidates_status_check"
        ),
        nullable=False,
        default="proposed",
    ),
)

# clause_refs is JSON (text[] on the HLD's Aurora target) for SQLite parity.
change_events = sa.Table(
    "change_events",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("source_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.sources.id"), nullable=False),
    sa.Column(
        "kind",
        sa.Text,
        sa.CheckConstraint("kind in ('added','amended','revoked')", name="change_events_kind_check"),
        nullable=False,
    ),
    sa.Column("old_version", sa.Text, nullable=True),
    sa.Column("new_version", sa.Text, nullable=False),
    sa.Column("clause_refs", Json, nullable=False, default=list),
    sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("diff_s3_uri", sa.Text, nullable=False, default=""),
)

licenses_held = sa.Table(
    "licenses_held",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("product", sa.Text, nullable=False),
    sa.Column("vendor", sa.Text, nullable=False),
    sa.Column("scope", sa.Text, nullable=False, default=""),
    sa.Column("seats", sa.Integer, nullable=True),
    sa.Column("purchased_at", sa.Date, nullable=True),
    sa.Column("renewal_at", sa.Date, nullable=True),
    sa.Column("notes", sa.Text, nullable=False, default=""),
)

byol_uploads = sa.Table(
    "byol_uploads",
    metadata,
    sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True, default=_uuid),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("source_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.sources.id"), nullable=False),
    sa.Column("content_hash", sa.Text, nullable=False),
    sa.Column("verified", sa.Boolean, nullable=False, default=False),
    sa.Column("s3_uri", sa.Text, nullable=False, default=""),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

ALL_TABLES = (
    source_families,
    sources,
    family_members,
    source_versions,
    clauses,
    citations,
    discovery_candidates,
    change_events,
    licenses_held,
    byol_uploads,
)
