from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.models import DiscordDailyThread
from docket.schemas.records import RecordSourceInput


def validate_configured_discord_source(
    session: Session,
    source: RecordSourceInput,
    actor_id: str,
) -> None:
    settings = get_settings()
    metadata = source.metadata
    exact_actor = (
        metadata.guild_id == settings.discord_guild_id
        and metadata.user_id == settings.operator_discord_user_id
        and actor_id == metadata.user_id
    )
    chat_root = (
        metadata.channel_id == settings.chat_channel_id
        and metadata.parent_channel_id is None
    )
    daily_thread = False
    if (
        metadata.parent_channel_id == settings.queue_channel_id
        and metadata.channel_id != settings.queue_channel_id
    ):
        daily_thread = (
            session.scalar(
                select(DiscordDailyThread.id)
                .where(
                    DiscordDailyThread.guild_id == settings.discord_guild_id,
                    DiscordDailyThread.channel_id == settings.queue_channel_id,
                    DiscordDailyThread.thread_id == metadata.channel_id,
                    DiscordDailyThread.status.in_(("active", "archived")),
                )
                .limit(1)
            )
            is not None
        )
    if not exact_actor or not (chat_root or daily_thread):
        raise DocketError(
            code="invalid_source_context",
            message=(
                "Discord source does not match the configured operator chat or "
                "a Docket-owned daily thread."
            ),
        )
