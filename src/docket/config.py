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
    calendar_reads_enabled: bool = Field(default=False, alias="DOCKET_CALENDAR_READS_ENABLED")
    external_writes_enabled: bool = Field(default=False, alias="DOCKET_EXTERNAL_WRITES_ENABLED")
    auto_create_schema: bool = Field(default=False, alias="DOCKET_AUTO_CREATE_SCHEMA")
    log_level: str = Field(default="INFO", alias="DOCKET_LOG_LEVEL")
    worker_heartbeat_seconds: float = Field(default=1.0, alias="DOCKET_WORKER_HEARTBEAT_SECONDS")
    operation_poll_seconds: float = Field(default=5.0, alias="DOCKET_OPERATION_POLL_SECONDS")
    reconciliation_poll_seconds: float = Field(
        default=300.0, alias="DOCKET_RECONCILIATION_POLL_SECONDS"
    )
    stale_lease_poll_seconds: float = Field(default=60.0, alias="DOCKET_STALE_LEASE_POLL_SECONDS")
    discord_projection_enabled: bool = Field(
        default=False, alias="DOCKET_DISCORD_PROJECTION_ENABLED"
    )
    discord_projection_url: str = Field(
        default="http://hermes:8787", alias="DOCKET_DISCORD_PROJECTION_URL"
    )
    discord_projection_poll_seconds: float = Field(
        default=5.0, gt=0, alias="DOCKET_DISCORD_PROJECTION_POLL_SECONDS"
    )
    discord_projection_lease_seconds: int = Field(
        default=30, alias="DOCKET_DISCORD_PROJECTION_LEASE_SECONDS"
    )
    discord_projection_max_attempts: int = Field(
        default=10, ge=1, alias="DOCKET_DISCORD_PROJECTION_MAX_ATTEMPTS"
    )
    approval_ttl_seconds: int = Field(default=86400, ge=60, alias="DOCKET_APPROVAL_TTL_SECONDS")
    daily_rollover_poll_seconds: float = Field(
        default=60.0, gt=0, alias="DOCKET_DAILY_ROLLOVER_POLL_SECONDS"
    )
    daily_rollover_hour: int = Field(default=7, ge=0, le=23, alias="DOCKET_DAILY_ROLLOVER_HOUR")
    local_action_ttl_seconds: int = Field(
        default=86400, ge=60, alias="DOCKET_LOCAL_ACTION_TTL_SECONDS"
    )
    calendar_sync_poll_seconds: float = Field(
        default=60.0, gt=0, alias="DOCKET_CALENDAR_SYNC_POLL_SECONDS"
    )
    calendar_sync_interval_seconds: int = Field(
        default=300, ge=30, alias="DOCKET_CALENDAR_SYNC_INTERVAL_SECONDS"
    )
    calendar_sync_lease_seconds: int = Field(
        default=120, ge=30, alias="DOCKET_CALENDAR_SYNC_LEASE_SECONDS"
    )
    calendar_sync_past_days: int = Field(
        default=30, ge=0, le=366, alias="DOCKET_CALENDAR_SYNC_PAST_DAYS"
    )
    calendar_sync_future_days: int = Field(
        default=400, ge=1, le=730, alias="DOCKET_CALENDAR_SYNC_FUTURE_DAYS"
    )
    calendar_stale_seconds: int = Field(default=900, ge=60, alias="DOCKET_CALENDAR_STALE_SECONDS")
    calendar_snapshot_max_pages: int = Field(
        default=100, ge=1, le=1000, alias="DOCKET_CALENDAR_SNAPSHOT_MAX_PAGES"
    )
    calendar_snapshot_max_events: int = Field(
        default=10000, ge=1, le=100000, alias="DOCKET_CALENDAR_SNAPSHOT_MAX_EVENTS"
    )
    calendar_require_fresh_wait_seconds: float = Field(
        default=10.0, gt=0, le=10, alias="DOCKET_CALENDAR_REQUIRE_FRESH_WAIT_SECONDS"
    )
    reminder_dispatch_interval_seconds: float = Field(
        default=30.0, gt=0, alias="DOCKET_REMINDER_DISPATCH_INTERVAL_SECONDS"
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
    google_account_external_id: str = Field(default="primary", alias="GOOGLE_ACCOUNT_EXTERNAL_ID")

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def protect_production(self) -> "Settings":
        channel_ids = {
            self.chat_channel_id,
            self.queue_channel_id,
            self.system_channel_id,
        }
        if len(channel_ids) != 3:
            raise ValueError("Docket chat, queue, and system channel IDs must be distinct")
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

    def calendar_write_mode(self) -> Literal["google", "fake", "disabled"]:
        if self.external_writes_enabled:
            return "google"
        if self.environment is Environment.PRODUCTION:
            return "disabled"
        return "fake"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
