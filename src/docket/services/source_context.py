from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.schemas.records import RecordSourceInput


def validate_configured_discord_source(source: RecordSourceInput, actor_id: str) -> None:
    settings = get_settings()
    metadata = source.metadata
    expected = {
        "guild_id": settings.discord_guild_id,
        "channel_id": settings.chat_channel_id,
        "user_id": settings.operator_discord_user_id,
    }
    actual = {
        "guild_id": metadata.guild_id,
        "channel_id": metadata.channel_id,
        "user_id": metadata.user_id,
    }
    if actual != expected or actor_id != metadata.user_id:
        raise DocketError(
            code="invalid_source_context",
            message="Discord source does not match the configured operator context.",
        )
