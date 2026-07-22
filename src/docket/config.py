from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from docket.domain.enums import Environment
from docket.providers.google.oauth import GoogleOAuthStatus, authorized_user_file_status

_DUMMY_PREFIXES = ("dummy", "smoke", "replace-me")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Environment = Field(default=Environment.DEVELOPMENT, alias="DOCKET_ENVIRONMENT")
    database_url: str = Field(
        default="sqlite+pysqlite:///./.runtime/docket.db",
        alias="DOCKET_DATABASE_URL",
    )
    timezone: str = Field(default="America/Los_Angeles", alias="DOCKET_TIMEZONE")
    external_calls_enabled: bool = Field(default=False, alias="DOCKET_EXTERNAL_CALLS_ENABLED")
    auto_create_schema: bool = Field(default=False, alias="DOCKET_AUTO_CREATE_SCHEMA")
    log_level: str = Field(default="INFO", alias="DOCKET_LOG_LEVEL")
    worker_heartbeat_seconds: float = Field(default=1.0, alias="DOCKET_WORKER_HEARTBEAT_SECONDS")
    operation_poll_seconds: float = Field(default=5.0, alias="DOCKET_OPERATION_POLL_SECONDS")
    reconciliation_poll_seconds: float = Field(
        default=300.0, alias="DOCKET_RECONCILIATION_POLL_SECONDS"
    )
    stale_lease_poll_seconds: float = Field(
        default=60.0, alias="DOCKET_STALE_LEASE_POLL_SECONDS"
    )

    operator_discord_user_id: str = Field(
        default="000000000000000001", alias="DOCKET_OPERATOR_DISCORD_USER_ID"
    )
    discord_guild_id: str = Field(default="000000000000000002", alias="DOCKET_DISCORD_GUILD_ID")
    chat_channel_id: str = Field(default="000000000000000003", alias="DOCKET_CHAT_CHANNEL_ID")
    queue_channel_id: str = Field(default="000000000000000004", alias="DOCKET_QUEUE_CHANNEL_ID")
    system_channel_id: str = Field(default="000000000000000005", alias="DOCKET_SYSTEM_CHANNEL_ID")

    docket_to_hermes_token_file: Path = Field(
        default=Path("secrets/smoke/docket_to_hermes_token"),
        alias="DOCKET_TO_HERMES_TOKEN_FILE",
    )
    hermes_to_docket_token_file: Path = Field(
        default=Path("secrets/smoke/hermes_to_docket_token"),
        alias="HERMES_TO_DOCKET_TOKEN_FILE",
    )
    interaction_signing_key_file: Path = Field(
        default=Path("secrets/smoke/interaction_signing_key"),
        alias="DOCKET_INTERACTION_SIGNING_KEY_FILE",
    )
    google_oauth_client_file: Path = Field(
        default=Path("secrets/smoke/google_oauth_client.json"),
        alias="GOOGLE_OAUTH_CLIENT_FILE",
    )
    google_oauth_token_file: Path = Field(
        default=Path("secrets/smoke/google_oauth_token.json"),
        alias="GOOGLE_OAUTH_TOKEN_FILE",
    )
    google_calendar_id: str = Field(default="docket-smoke-calendar", alias="GOOGLE_CALENDAR_ID")
    google_account_external_id: str = Field(
        default="primary", alias="GOOGLE_ACCOUNT_EXTERNAL_ID"
    )

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def protect_production(self) -> "Settings":
        if self.environment is Environment.PRODUCTION:
            if self.auto_create_schema:
                raise ValueError("DOCKET_AUTO_CREATE_SCHEMA must be false in production")
            for identifier in (
                self.operator_discord_user_id,
                self.discord_guild_id,
                self.chat_channel_id,
                self.queue_channel_id,
                self.system_channel_id,
            ):
                if identifier.startswith("000000"):
                    raise ValueError("Production cannot use smoke Discord identifiers")
        return self

    @staticmethod
    def read_secret(path: Path) -> str:
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError(f"Secret file is empty: {path}")
        return value

    def hermes_to_docket_token(self) -> str:
        return self.read_secret(self.hermes_to_docket_token_file)

    def docket_to_hermes_token(self) -> str:
        return self.read_secret(self.docket_to_hermes_token_file)

    def credential_mode(self) -> Literal["dummy", "configured"]:
        token = self.hermes_to_docket_token()
        return "dummy" if token.casefold().startswith(_DUMMY_PREFIXES) else "configured"

    def google_oauth_status(self) -> GoogleOAuthStatus:
        return authorized_user_file_status(self.google_oauth_token_file)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
