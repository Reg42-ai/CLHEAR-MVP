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

    # ARCH: stand-in for reg42-os auth; comma-separated identities with the
    # `maintainer` role. Replace with the existing session/role dependency on merge.
    clhear_maintainers: str = "avner@reg42.ai"

    # Exporter target: local checkout dir and optional remote (public `clhear` repo).
    clhear_public_repo_dir: str = "./clhear-public"
    clhear_public_repo_url: str = ""
    clhear_export_git_token: str = ""

    clhear_artifacts_dir: str = "./artifacts"

    @property
    def maintainer_set(self) -> set[str]:
        return {m.strip() for m in self.clhear_maintainers.split(",") if m.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
