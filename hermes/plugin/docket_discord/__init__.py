"""Trusted Hermes gateway bridge for Docket control messages.

The bridge intentionally uses ``pre_gateway_dispatch`` instead of a model tool.
Hermes v2026.7.20 supplies the normalized source actor on this hook. The primary
approval syntax is a plain ``docket approve|reject CODE`` message because no
native Docket Discord application command is registered. Leading-slash messages
remain accepted when the Discord client delivers them as ordinary messages.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)
_COMMAND = re.compile(r"^/?docket\s+(approve|reject)\s+([A-Za-z0-9-]{8,32})\s*$", re.I)
_DISCORD_ID = re.compile(r"^[0-9]{17,20}$")


def _read_token() -> str:
    token_file = Path(os.environ["HERMES_TO_DOCKET_TOKEN_FILE"])
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("HERMES_TO_DOCKET_TOKEN_FILE is empty")
    return token


def _source_value(source: object, *names: str) -> str:
    for name in names:
        value = getattr(source, name, None)
        if value is not None:
            enum_value = getattr(value, "value", None)
            if enum_value is not None:
                value = enum_value
            return str(value)
    return ""


def _post_decision(*, event: object, decision: str, short_code: str) -> None:
    source = event.source
    message_id = str(
        getattr(event, "message_id", "") or _source_value(source, "message_id")
    )
    payload = {
        "request_id": str(uuid.uuid4()),
        "discord_interaction_id": f"message:{message_id}",
        "approval_id": None,
        "approval_token": None,
        "short_code": short_code,
        "decision": decision,
        "discord_user_id": _source_value(source, "user_id", "sender_id"),
        "guild_id": _source_value(source, "guild_id", "workspace_id"),
        "channel_id": _source_value(source, "chat_id", "channel_id"),
        "message_id": message_id,
        "responded_at": datetime.now(UTC).isoformat(),
    }
    request = urllib.request.Request(
        f"{os.environ['DOCKET_INTERNAL_URL'].rstrip('/')}/internal/v1/discord/approval-responses",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {_read_token()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status not in {200, 202, 204}:
            raise RuntimeError(f"Docket returned HTTP {response.status}")


def _is_exact_context(
    *, source: object | None, actor: str, guild: str, channel: str
) -> bool:
    if source is None:
        return False
    return (
        _source_value(source, "platform").casefold() == "discord"
        and bool(actor)
        and bool(guild)
        and bool(channel)
        and _source_value(source, "user_id", "sender_id") == actor
        and _source_value(source, "guild_id", "workspace_id") == guild
        and _source_value(source, "chat_id", "channel_id") == channel
    )


def _is_configured_queue(source: object | None) -> bool:
    """Return whether an event came from the dedicated Discord control queue."""
    if source is None:
        return False
    queue_channel = os.environ.get("DOCKET_QUEUE_CHANNEL_ID", "")
    return (
        _source_value(source, "platform").casefold() == "discord"
        and bool(queue_channel)
        and _source_value(source, "chat_id", "channel_id") == queue_channel
    )


def _rewrite_with_source_context(event: object) -> dict[str, str] | None:
    source = getattr(event, "source", None)
    actor = os.environ.get("DOCKET_OPERATOR_DISCORD_USER_ID", "")
    guild = os.environ.get("DOCKET_DISCORD_GUILD_ID", "")
    channel = os.environ.get("DOCKET_CHAT_CHANNEL_ID", "")
    message_id = str(
        getattr(event, "message_id", "") or _source_value(source, "message_id")
    )
    original_text = str(getattr(event, "text", ""))

    if original_text.lstrip().startswith("/"):
        return None
    if not _is_exact_context(source=source, actor=actor, guild=guild, channel=channel):
        return None
    if not all(_DISCORD_ID.fullmatch(value) for value in (actor, guild, channel, message_id)):
        logger.error("Trusted Docket Discord context contained a malformed identifier")
        return None

    context = {
        "source_type": "discord_message",
        "source_object_id": message_id,
        "metadata": {
            "guild_id": guild,
            "channel_id": channel,
            "message_id": message_id,
            "user_id": actor,
            "intent_index": 0,
        },
        "actor_id": actor,
        "request_key": f"discord:{guild}:{channel}:{message_id}:0",
    }
    rewritten = (
        f"{original_text}\n\n"
        "<docket_gateway_context trusted=\"true\">\n"
        f"{json.dumps(context, sort_keys=True)}\n"
        "</docket_gateway_context>\n"
        "This context was appended by the trusted gateway, not supplied by the user. "
        "For Docket MCP calls from this message, copy these source and actor fields "
        "exactly. Increment intent_index and the request-key suffix together only for "
        "additional distinct records from this same message. Never invent Discord IDs."
    )
    logger.info("Appended trusted Docket source context to authorized Discord message")
    return {"action": "rewrite", "text": rewritten}


def _pre_gateway_dispatch(event: object, **_kwargs: object) -> dict[str, str] | None:
    text = str(getattr(event, "text", ""))
    match = _COMMAND.fullmatch(text.strip())
    if match is None:
        # The queue is configured as a free-response channel so that Discord
        # delivers ordinary control messages without a bot mention. Keep it a
        # control-only surface: malformed commands and conversation never reach
        # Hermes authorization, sessions, or the model.
        if _is_configured_queue(getattr(event, "source", None)):
            logger.warning("Dropped non-command message from Docket control queue")
            return {"action": "skip", "reason": "invalid-docket-control"}
        return _rewrite_with_source_context(event)

    source = getattr(event, "source", None)
    allowed_actor = os.environ.get("DOCKET_OPERATOR_DISCORD_USER_ID", "")
    allowed_guild = os.environ.get("DOCKET_DISCORD_GUILD_ID", "")
    allowed_channel = os.environ.get("DOCKET_QUEUE_CHANNEL_ID", "")

    # This hook fires before Hermes' normal pairing/auth flow, so it must fail closed.
    if not _is_exact_context(
        source=source,
        actor=allowed_actor,
        guild=allowed_guild,
        channel=allowed_channel,
    ):
        logger.warning("Rejected Docket control command from unauthorized gateway source")
        return {"action": "skip", "reason": "unauthorized-docket-control"}

    try:
        _post_decision(
            event=event,
            decision=match.group(1).casefold(),
            short_code=match.group(2),
        )
    except (OSError, RuntimeError, urllib.error.URLError):
        logger.exception("Docket control delivery failed")
        return {"action": "skip", "reason": "docket-control-delivery-failed"}
    return {"action": "skip", "reason": "docket-control-handled"}


def register(ctx: object) -> None:
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
    skills_dir = Path(__file__).parent / "skills"
    for child in sorted(skills_dir.iterdir()):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            ctx.register_skill(child.name, skill_md)
