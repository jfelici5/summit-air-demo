"""Configuration and explicitly fictional Summit Air operating assumptions."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    environment: str = "local"
    database_url: str = "sqlite:///./data/summit.db"
    public_base_url: str = ""
    admin_api_key: str = ""
    retell_api_key: str = ""
    retell_webhook_api_key: str = ""
    retell_phone_number: str = ""
    retell_agent_id: str = ""
    retell_llm_id: str = ""
    retell_voice_id: str = "11labs-Adrian"
    cal_api_key: str = ""
    cal_event_type_id: int | None = None
    cal_webhook_secret: str = ""
    inquiry_email_url: str = ""
    inquiry_email_token: str = ""
    service_timezone: str = "America/New_York"
    service_counties: str = "Nassau,Suffolk,Queens"
    service_state: str = "NY"
    cold_threshold_f: float = 40
    extreme_heat_threshold_f: float = 90
    appointment_minutes: int = 120
    provider_timeout_seconds: float = 6
    weather_enabled: bool = True
    transfer_number: str = ""

    @property
    def counties(self) -> set[str]:
        """Return the configured county names without the 'County' suffix."""
        return {v.strip().lower().removesuffix(" county") for v in self.service_counties.split(",")}

    @property
    def retell_signing_key(self) -> str:
        """Retell signs with the dashboard key carrying the Webhook badge."""
        return self.retell_webhook_api_key or self.retell_api_key

    @property
    def inquiry_email_enabled(self) -> bool:
        return bool(
            self.inquiry_email_token
            and self.inquiry_email_url.startswith("https://script.google.com/macros/s/")
            and self.inquiry_email_url.endswith("/exec")
        )


@lru_cache
def get_settings() -> Settings:
    """Read environment configuration once per process."""
    return Settings()
