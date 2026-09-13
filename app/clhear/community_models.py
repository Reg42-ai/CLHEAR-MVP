"""Community schema: contributor accounts, submissions ("cases"), and
validation votes on derived obligations.

Participation is the credibility engine (build plan): anyone can join with
email / Google / Apple, open cases (missing data, corrections, new sources,
output validation, product suggestions), and confirm/dispute derived
obligations. Every submission mirrors into the l0 proposals queue so the
named-human gate stays singular.
"""
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

COMMUNITY_SCHEMA = "community"

metadata = sa.MetaData()
Json = sa.JSON().with_variant(JSONB(), "postgresql")
BigId = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def _uuid() -> str:
    return str(uuid.uuid4())


users = sa.Table(
    "users",
    metadata,
    sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True, default=_uuid),
    sa.Column("email", sa.Text, nullable=False, unique=True),
    sa.Column("display_name", sa.Text, nullable=False, default=""),
    sa.Column("provider", sa.Text, nullable=False, default="email"),  # email | google | apple
    sa.Column("provider_sub", sa.Text, nullable=False, default=""),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    schema=COMMUNITY_SCHEMA,
)

SUBMISSION_KINDS = ("missing_data", "correction", "new_source", "output_validation", "product_suggestion")
SUBMISSION_STATUSES = ("new", "triaged", "accepted", "rejected", "shipped")

submissions = sa.Table(
    "submissions",
    metadata,
    sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True, default=_uuid),
    sa.Column("kind", sa.Text, sa.CheckConstraint(f"kind in {SUBMISSION_KINDS}", name="submissions_kind_check"), nullable=False),
    sa.Column("target_layer", sa.Text, nullable=False, default=""),
    sa.Column("target_id", sa.Text, nullable=False, default=""),  # obligation id, source key, clause permalink…
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("body", sa.Text, nullable=False, default=""),
    sa.Column("evidence_url", sa.Text, nullable=False, default=""),
    sa.Column(
        "status",
        sa.Text,
        sa.CheckConstraint(f"status in {SUBMISSION_STATUSES}", name="submissions_status_check"),
        nullable=False,
        default="new",
    ),
    sa.Column("submitter_id", sa.Uuid(as_uuid=False), nullable=False),
    sa.Column("proposal_id", sa.Uuid(as_uuid=False), nullable=True),  # l0 mirror
    sa.Column("resolution", sa.Text, nullable=False, default=""),
    sa.Column("decided_by", sa.Text, nullable=True),
    sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    schema=COMMUNITY_SCHEMA,
)

votes = sa.Table(
    "votes",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("obligation_id", sa.Text, nullable=False),
    sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=False),
    sa.Column("vote", sa.Text, sa.CheckConstraint("vote in ('confirm','dispute')", name="votes_vote_check"), nullable=False),
    sa.Column("comment", sa.Text, nullable=False, default=""),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.UniqueConstraint("obligation_id", "user_id", name="votes_one_per_user"),
    schema=COMMUNITY_SCHEMA,
)

# HLD v2 §5 "Build on it": API keys a signed-in user issues in one click. The
# secret is shown once; only its sha256 is stored. Revocation sets revoked_at
# (I2: never deleted).
api_keys = sa.Table(
    "api_keys",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # KEY-000001
    sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=False, index=True),
    sa.Column("app_id", sa.Text, nullable=False, unique=True),  # the X-App-Id the caller sends
    sa.Column("label", sa.Text, nullable=False, default=""),
    sa.Column("secret_hash", sa.Text, nullable=False),
    sa.Column("prefix", sa.Text, nullable=False, default=""),  # first 8 chars, for display
    sa.Column("scopes", Json, nullable=False, default=list),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    schema=COMMUNITY_SCHEMA,
)

# HLD v2 §5 "Learn": progress on the learning path (tour completed, quiz
# passed, playground used). Anonymous learners carry a client-generated
# learner id; signed-in users carry their user id.
learning_progress = sa.Table(
    "learning_progress",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("learner_id", sa.Text, nullable=False, index=True),
    sa.Column("step_id", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False, default="tour"),  # tour | quiz | playground
    sa.Column("score", sa.Numeric(4, 3), nullable=True),
    sa.Column("detail", Json, nullable=False, default=dict),
    sa.Column("completed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.UniqueConstraint("learner_id", "step_id", name="learning_progress_one_per_step"),
    schema=COMMUNITY_SCHEMA,
)

# HLD v2 §5 "Watch": a watcher follows an L4 profile (their own or a public
# sample) so the digest can say what changed for profiles like theirs.
profile_watches = sa.Table(
    "profile_watches",
    metadata,
    sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True, default=_uuid),
    sa.Column("watcher_id", sa.Text, nullable=False, index=True),
    sa.Column("profile_id", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
    sa.UniqueConstraint("watcher_id", "profile_id", name="profile_watches_watcher_profile"),
    schema=COMMUNITY_SCHEMA,
)

COMMUNITY_TABLES = (users, submissions, votes, api_keys, learning_progress, profile_watches)
