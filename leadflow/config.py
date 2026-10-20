"""Environment settings. Every value can be set in `.env` or as an env var."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./leadflow.db"
    # Shared secret Zapier must send in the X-API-Key header.
    api_key: str = ""
    rules_file: str = "rules.yaml"
    # True = never call external services; log what would have been sent.
    dry_run: bool = False
    # Public URL of this service (used for Twilio signatures and status callbacks).
    # On Render it is filled in automatically from RENDER_EXTERNAL_URL.
    public_base_url: str = ""
    render_external_url: str = ""
    # Run follow-ups, campaigns, reconciliation and the weekly report inside the server.
    scheduler_enabled: bool = False

    # Claude (reply classification)
    anthropic_api_key: str = ""
    classifier_model: str = "claude-opus-5-5"
    classifier_effort: str = "low"

    # Lofty CRM
    lofty_api_key: str = ""
    lofty_base_url: str = "https://api.lofty.com/v1.0"

    # SMS provider (Twilio)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    twilio_lookup_enabled: bool = True

    # BeenVerified (or any enrichment API that takes name + address)
    beenverified_api_key: str = ""
    beenverified_api_url: str = ""

    # Alerts: a Slack incoming webhook or a Zapier catch hook (which can email).
    alert_webhook_url: str = ""

    agent_name: str = "Daniel"
    agent_phone: str = ""

    @property
    def base_url(self) -> str:
        return (self.public_base_url or self.render_external_url).rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
