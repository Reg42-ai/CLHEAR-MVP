"""l0_platform schema (HLD §6.1) as SQLAlchemy Core metadata.

The metadata carries the `l0_platform` schema name; on SQLite (dev/tests) the
engine translates the schema away so the same statements run on both backends.
"""
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

L0_SCHEMA = "l0_platform"

metadata = sa.MetaData(schema=L0_SCHEMA)

Json = sa.JSON().with_variant(JSONB(), "postgresql")


def _uuid() -> str:
    return str(uuid.uuid4())


schema_migrations = sa.Table(
    "schema_migrations",
    metadata,
    sa.Column("version", sa.Integer, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("applied_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

# Transactional OUTBOX; relay ships unrelayed rows to SQS and stamps relayed_at.
events = sa.Table(
    "events",
    metadata,
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), sa.Identity(), primary_key=True),
    sa.Column("event_id", sa.Uuid(as_uuid=False), nullable=False, unique=True, default=_uuid),
    sa.Column("layer", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("subject_ref", sa.Text, nullable=False),
    sa.Column("payload", Json, nullable=False, default=dict),
    sa.Column("schema_version", sa.Integer, nullable=False, default=1),
    sa.Column("producer", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("relayed_at", sa.DateTime(timezone=True), nullable=True),
)

# ONE proposals table for all layers (HLD §7.1).
proposals = sa.Table(
    "proposals",
    metadata,
    sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True, default=_uuid),
    sa.Column("layer", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("subject_ref", sa.Text, nullable=False),
    sa.Column("draft", Json, nullable=False, default=dict),
    sa.Column("rationale", sa.Text, nullable=False, default=""),
    sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
    sa.Column(
        "status",
        sa.Text,
        sa.CheckConstraint("status in ('proposed','approved','rejected')", name="proposals_status_check"),
        nullable=False,
        default="proposed",
    ),
    sa.Column("approver", sa.Text, nullable=True),
    sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

llm_calls = sa.Table(
    "llm_calls",
    metadata,
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), sa.Identity(), primary_key=True),
    sa.Column("fleet", sa.Text, nullable=False),
    sa.Column("provider", sa.Text, nullable=False),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("prompt_hash", sa.Text, nullable=False),
    sa.Column("input_tokens", sa.Integer, nullable=False),
    sa.Column("output_tokens", sa.Integer, nullable=False),
    sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

eval_runs = sa.Table(
    "eval_runs",
    metadata,
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), sa.Identity(), primary_key=True),
    sa.Column("suite", sa.Text, nullable=False),
    sa.Column("source_key", sa.Text, nullable=True),
    sa.Column("release", sa.Text, nullable=True),
    sa.Column("scores", Json, nullable=False, default=dict),
    sa.Column("passed", sa.Boolean, nullable=False),
    sa.Column("ran_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

# Run ledger: every fleet run recorded (HLD principle 7 — replayability).
runs = sa.Table(
    "runs",
    metadata,
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), sa.Identity(), primary_key=True),
    sa.Column("fleet", sa.Text, nullable=False),
    sa.Column("trigger", sa.Text, nullable=False),
    sa.Column("inputs", Json, nullable=False, default=dict),
    sa.Column("outputs", Json, nullable=False, default=dict),
    sa.Column("duration_ms", sa.Integer, nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)
