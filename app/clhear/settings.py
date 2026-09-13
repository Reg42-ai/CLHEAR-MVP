"""CLHEAR settings.

# ARCH: reg42-os has its own settings module and conventions; this standalone
# settings object mirrors what the HLD names (REG42_CLHEAR_ENABLED feature
# flag, spend caps, queue/bucket wiring) so it can be folded into the existing
# settings when this package moves into reg42-os.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    reg42_clhear_enabled: bool = True

    # Postgres in AWS (Aurora); sqlite fallback keeps dev/tests offline.
    database_url: str = "sqlite:///./clhear.db"

    aws_region: str = "us-east-1"
    clhear_events_queue_url: str = ""
    clhear_events_dlq_url: str = ""
    clhear_datalake_bucket: str = "reg42-clhear-datalake"

    # Inference (HLD v2 I6): only Reg42 Infer on Bedrock. No vendor keys, no
    # self-hosted runtime. Empty INFER_* = no production provider (loud, not fake).
    infer_base_url: str = ""  # https://infer.reg42.ai/v1
    infer_token: str = ""
    infer_employee_id: str = "clhear"  # X-Employee-Id; fleets override with clhear-l<n>
    infer_data_class: str = "public"  # X-Data-Class for the agnostic store
    clhear_llm_provider: str = ""  # "", "fake", "infer"
    # Hard caps (§5): alarm handled by CloudWatch on the llm spend metric.
    clhear_gateway_fleet_daily_cap_usd: float = 20.0
    clhear_gateway_global_daily_cap_usd: float = 100.0
    clhear_frontier_monthly_cap_usd: float = 50.0  # premium rungs (Opus-class) per month

    # Fidelity gate + repair loop (evals are gates, not reports).
    clhear_fidelity_threshold: float = 0.995
    clhear_ingest_max_attempts: int = 3
    # Max share of tokens dumb salvage may recover as unstructured notes;
    # bigger gaps need typed hints (learned or LLM-proposed) or the run fails.
    clhear_salvage_cap: float = 0.02
    clhear_model_repair: str = ""  # empty = the router's l1_parse ladder decides

    # ARCH: stand-in for reg42-os auth; comma-separated identities with the
    # `maintainer` role. Replace with the existing session/role dependency on merge.
    clhear_maintainers: str = "avner@reg42.ai"

    # Exporter target: local checkout dir and optional remote (public `clhear` repo).
    clhear_public_repo_dir: str = "./clhear-public"
    clhear_public_repo_url: str = ""
    clhear_export_git_token: str = ""
    # HLD v2 §9: no public disclosure before the provisional filing is confirmed.
    # The exporter compiles the public repo locally regardless; it pushes only when
    # this is true (set as a repository variable in the release workflow).
    clhear_public_disclosure_confirmed: bool = False
    # HLD v2 I9: agnostic (public product) | member | instance (runs in a client account).
    clhear_mode: str = "agnostic"
    # L8 benchmark inputs are keyed by HMAC(member id, this secret); rotate = new cohort history.
    clhear_benchmark_hmac_key: str = ""

    # HLD v2 §6 tooling: Discourse forum (link shown on the contribute page) and the
    # beehiiv newsletter that carries the change digest. Empty = hooks are inert.
    clhear_discourse_url: str = ""
    clhear_beehiiv_api_key: str = ""
    clhear_beehiiv_publication_id: str = ""  # pub_…
    clhear_beehiiv_post_status: str = "draft"  # draft | confirmed (confirmed sends immediately)

    clhear_artifacts_dir: str = "./artifacts"

    # --- community accounts & contributions (Phase C) ---
    # HMAC key for session cookies + magic-link tokens. MUST be set in prod
    # (SSM /clhear/SESSION_SECRET); the default keeps dev/tests working.
    clhear_session_secret: str = "dev-only-not-a-secret"
    clhear_auth_debug: bool = False  # dev: return magic links in the response
    clhear_ses_sender: str = "CLHEAR <noreply@clhear.reg42.ai>"
    clhear_public_base_url: str = "https://clhear.reg42.ai"
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    apple_oauth_client_id: str = ""  # Service ID; Apple flow activates when set
    apple_oauth_team_id: str = ""
    apple_oauth_key_id: str = ""
    apple_oauth_private_key: str = ""
    clhear_submissions_daily_limit: int = 10
    # HLD v2 §5 identity: Cognito user pool (Google IdP for Reg42). Empty =
    # Cognito off; magic link + direct Google OAuth keep working.
    clhear_cognito_region: str = ""
    clhear_cognito_user_pool_id: str = ""
    clhear_cognito_client_id: str = ""
    clhear_cognito_domain: str = ""  # hosted UI, https://<prefix>-auth.auth.<region>.amazoncognito.com

    # HLD v2 I7 projections: Neo4j query graph (empty = in-process projection) and
    # the clause embedding index (auto = Infer when configured, else hash-v1).
    clhear_neo4j_uri: str = ""  # bolt://clhear-neo4j.clhear.local:7687
    clhear_neo4j_user: str = "neo4j"
    clhear_neo4j_password: str = ""
    clhear_embedding_provider: str = "auto"  # auto | infer | hash
    clhear_embedding_model: str = "amazon.titan-embed-text-v2:0"

    # Snapshot mode for the scheduled fleet: the corpus SQLite lives in S3
    # (same object the public explorer serves); workers pull it, ingest, and
    # publish it back. Empty = use database_url directly (Aurora / local dev).
    clhear_snapshot_s3_uri: str = ""

    # Consumer API keys: "app_id:secret" or "app_id:secret:read:l1+read:l2"
    clhear_app_keys: str = "os-dev:dev-os-key,safeluance-dev:dev-sl-key,galaxy:galaxy-os-key"

    # Named releases live under this prefix (s3://bucket/releases/...).
    # Empty = local artifacts dir (dev/tests).
    clhear_releases_s3_prefix: str = ""

    @property
    def maintainer_set(self) -> set[str]:
        return {m.strip() for m in self.clhear_maintainers.split(",") if m.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
