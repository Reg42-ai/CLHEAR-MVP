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

    # LLM gateway (HLD §5): hard caps, alarm handled by CloudWatch on llm spend metric.
    anthropic_api_key: str = ""
    clhear_gateway_fleet_daily_cap_usd: float = 20.0
    clhear_gateway_global_daily_cap_usd: float = 100.0

    # Fidelity gate + repair loop (evals are gates, not reports).
    clhear_fidelity_threshold: float = 0.995
    clhear_ingest_max_attempts: int = 3
    # Max share of tokens dumb salvage may recover as unstructured notes;
    # bigger gaps need typed hints (learned or LLM-proposed) or the run fails.
    clhear_salvage_cap: float = 0.02
    clhear_model_repair: str = "claude-sonnet-4-20250514"

    # ARCH: stand-in for reg42-os auth; comma-separated identities with the
    # `maintainer` role. Replace with the existing session/role dependency on merge.
    clhear_maintainers: str = "avner@reg42.ai"

    # Exporter target: local checkout dir and optional remote (public `clhear` repo).
    clhear_public_repo_dir: str = "./clhear-public"
    clhear_public_repo_url: str = ""
    clhear_export_git_token: str = ""

    clhear_artifacts_dir: str = "./artifacts"

    # Snapshot mode for the scheduled fleet: the corpus SQLite lives in S3
    # (same object the public explorer serves); workers pull it, ingest, and
    # publish it back. Empty = use database_url directly (Aurora / local dev).
    clhear_snapshot_s3_uri: str = ""

    @property
    def maintainer_set(self) -> set[str]:
        return {m.strip() for m in self.clhear_maintainers.split(",") if m.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
