"""Trusted Hermes gateway bridge for Docket control messages.

The bridge intentionally uses ``pre_gateway_dispatch`` instead of a model tool.
Hermes v2026.7.20 supplies the normalized source actor on this hook. Persistent
Approve/Reject buttons are the normal approval surface. Plain
``docket approve|reject CODE`` messages remain accepted only as an operator
break-glass path; they are not model-facing guidance. Leading-slash messages
remain accepted when the Discord client delivers them as ordinary messages.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_COMMAND = re.compile(r"^/?docket\s+(approve|reject)\s+([A-Za-z0-9-]{8,32})\s*$", re.I)
_DISCORD_ID = re.compile(r"^[0-9]{17,20}$")
_PROJECTION_PATH = re.compile(r"^/internal/docket/discord/projections/([0-9a-fA-F-]{36})$")
_THREAD_LIFECYCLE_PATH = re.compile(
    r"^/internal/docket/discord/threads/([0-9a-fA-F-]{36})/lifecycle$"
)
_CONTROL_ID = re.compile(r"^dkt:([ar]):([A-Za-z0-9_-]{70,90})$")
_MAX_REQUEST_BYTES = 65536
_SERVER: ThreadingHTTPServer | None = None
_LISTENER_CLIENT_ID: int | None = None
_OPERATION_LOCKS: dict[str, threading.Lock] = {}
_OPERATION_LOCKS_GUARD = threading.Lock()


class PluginAPIError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _operation_lock(key: str) -> threading.Lock:
    with _OPERATION_LOCKS_GUARD:
        return _OPERATION_LOCKS.setdefault(key, threading.Lock())


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
    message_id = str(getattr(event, "message_id", "") or _source_value(source, "message_id"))
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


def _is_exact_context(*, source: object | None, actor: str, guild: str, channel: str) -> bool:
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
    message_id = str(getattr(event, "message_id", "") or _source_value(source, "message_id"))
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
        '<docket_gateway_context trusted="true">\n'
        f"{json.dumps(context, sort_keys=True)}\n"
        "</docket_gateway_context>\n"
        "This context was appended by the trusted gateway, not supplied by the user. "
        "For Docket MCP calls from this message, copy these source and actor fields "
        "exactly. Reads do not consume an intent index. For every additional distinct "
        "state-changing Docket operation from this same message, increment intent_index "
        "and the request-key suffix together. Referencing an existing record is not a "
        "state-changing operation. Never invent Discord IDs."
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


def _read_outbound_token() -> str:
    path = Path(os.environ["DOCKET_TO_HERMES_TOKEN_FILE"])
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("DOCKET_TO_HERMES_TOKEN_FILE is empty")
    return token


def _discord_runtime() -> tuple[asyncio.AbstractEventLoop, object, object]:
    try:
        from gateway import run as gateway_run

        runner = gateway_run._gateway_runner_ref()
    except (ImportError, AttributeError) as exc:
        raise PluginAPIError(
            "discord_runtime_unavailable", "Pinned Hermes gateway seam is unavailable", 503
        ) from exc
    if runner is None:
        raise PluginAPIError("discord_runtime_unavailable", "Gateway is not running", 503)
    adapter = next(
        (
            candidate
            for candidate in getattr(runner, "adapters", {}).values()
            if getattr(getattr(candidate, "platform", None), "value", None) == "discord"
        ),
        None,
    )
    loop = getattr(runner, "_gateway_loop", None)
    client = getattr(adapter, "_client", None)
    if adapter is None or loop is None or client is None or not loop.is_running():
        raise PluginAPIError("discord_runtime_unavailable", "Discord adapter is not ready", 503)
    return loop, adapter, client


def _run_on_discord(coroutine: Any) -> dict[str, Any]:
    try:
        loop, _adapter, _client = _discord_runtime()
    except Exception:
        coroutine.close()
        raise
    future = asyncio.run_coroutine_threadsafe(coroutine, loop)
    try:
        result = future.result(timeout=25)
    except TimeoutError as exc:
        future.cancel()
        raise PluginAPIError("discord_timeout", "Discord operation timed out", 503) from exc
    if not isinstance(result, dict):
        raise PluginAPIError("invalid_plugin_result", "Discord operation returned no result", 500)
    return result


def _configured_identity() -> tuple[str, str, str]:
    return (
        os.environ.get("DOCKET_DISCORD_GUILD_ID", ""),
        os.environ.get("DOCKET_QUEUE_CHANNEL_ID", ""),
        os.environ.get("DOCKET_OPERATOR_DISCORD_USER_ID", ""),
    )


def _require_snowflake(value: object, field: str) -> str:
    normalized = str(value)
    if not _DISCORD_ID.fullmatch(normalized):
        raise PluginAPIError("invalid_discord_id", f"{field} is not a Discord snowflake", 422)
    return normalized


def _require_request_id(payload: dict[str, Any]) -> str:
    try:
        return str(uuid.UUID(str(payload["request_id"])))
    except (KeyError, ValueError) as exc:
        raise PluginAPIError("invalid_request_id", "request_id must be a UUID", 422) from exc


def _validate_target(guild_id: object, channel_id: object) -> tuple[str, str]:
    guild = _require_snowflake(guild_id, "guild_id")
    channel = _require_snowflake(channel_id, "channel_id")
    expected_guild, expected_channel, _operator = _configured_identity()
    if not hmac.compare_digest(guild, expected_guild) or not hmac.compare_digest(
        channel, expected_channel
    ):
        raise PluginAPIError(
            "discord_target_not_allowed", "Target is not the configured Docket queue", 403
        )
    return guild, channel


async def _fetch_queue(client: object, guild_id: str, channel_id: str) -> object:
    import discord

    try:
        channel = await client.fetch_channel(int(channel_id))
    except discord.NotFound as exc:
        raise PluginAPIError("queue_channel_not_found", "Queue channel was not found") from exc
    if not isinstance(channel, discord.TextChannel) or str(channel.guild.id) != guild_id:
        raise PluginAPIError(
            "invalid_queue_channel", "Configured queue is not a text channel in the guild"
        )
    return channel


async def _find_named_threads(queue: object, name: str) -> list[object]:
    matches: dict[int, object] = {
        thread.id: thread for thread in queue.threads if thread.name == name
    }
    async for thread in queue.archived_threads(limit=None, private=False):
        if thread.name == name:
            matches[thread.id] = thread
    return list(matches.values())


async def _ensure_thread(payload: dict[str, Any]) -> dict[str, Any]:
    import discord

    request_id = _require_request_id(payload)
    try:
        daily_thread_id = str(uuid.UUID(str(payload["daily_thread_id"])))
        local_date = date.fromisoformat(str(payload["local_date"]))
    except (KeyError, ValueError) as exc:
        raise PluginAPIError(
            "invalid_thread_request", "Daily thread identity or date is invalid", 422
        ) from exc
    guild_id, channel_id = _validate_target(payload.get("guild_id"), payload.get("channel_id"))
    expected_name = f"{local_date.isoformat()} — {local_date.strftime('%A')}"
    if payload.get("name") != expected_name or payload.get("thread_type") != "public_thread":
        raise PluginAPIError(
            "invalid_thread_request", "Thread name or explicit type is invalid", 422
        )
    try:
        requested_archive = int(payload.get("auto_archive_minutes", 10080))
    except (TypeError, ValueError) as exc:
        raise PluginAPIError(
            "invalid_thread_request", "auto_archive_minutes is invalid", 422
        ) from exc
    _loop, _adapter, client = _discord_runtime()
    queue = await _fetch_queue(client, guild_id, channel_id)
    bot_id = getattr(getattr(client, "user", None), "id", None)
    if bot_id is None:
        raise PluginAPIError("discord_runtime_unavailable", "Discord bot is not ready", 503)

    thread = None
    known_id = payload.get("known_thread_id")
    if known_id is not None:
        known = _require_snowflake(known_id, "known_thread_id")
        try:
            candidate = await client.fetch_channel(int(known))
        except discord.NotFound:
            candidate = None
        if candidate is not None:
            if (
                not isinstance(candidate, discord.Thread)
                or candidate.parent_id != queue.id
                or candidate.name != expected_name
                or candidate.owner_id != bot_id
            ):
                raise PluginAPIError(
                    "stored_thread_binding_mismatch",
                    "Stored daily thread no longer matches its trusted binding",
                )
            thread = candidate

    created = False
    if thread is None:
        matches = await _find_named_threads(queue, expected_name)
        owned = [candidate for candidate in matches if candidate.owner_id == bot_id]
        foreign = [candidate for candidate in matches if candidate.owner_id != bot_id]
        if foreign or len(owned) > 1:
            raise PluginAPIError(
                "daily_thread_name_conflict",
                "The daily thread name is foreign-owned or ambiguous",
            )
        if owned:
            thread = owned[0]
        else:
            durations = [
                value for value in (10080, 4320, 1440, 60) if value <= requested_archive
            ] or [60]
            last_error = None
            for duration in durations:
                try:
                    thread = await queue.create_thread(
                        name=expected_name,
                        type=discord.ChannelType.public_thread,
                        auto_archive_duration=duration,
                        reason="Docket daily queue projection",
                    )
                    created = True
                    break
                except discord.HTTPException as exc:
                    last_error = exc
            if thread is None:
                raise PluginAPIError(
                    "daily_thread_create_failed", "Discord rejected all archive durations"
                ) from last_error

    unarchived = bool(thread.archived)
    if thread.archived:
        thread = await thread.edit(
            archived=False, locked=False, reason="Docket projection delivery"
        )
    return {
        "request_id": request_id,
        "daily_thread_id": daily_thread_id,
        "guild_id": guild_id,
        "channel_id": channel_id,
        "thread_id": str(thread.id),
        "created": created,
        "unarchived": unarchived,
        "auto_archive_minutes": int(thread.auto_archive_duration),
        "verified_at": datetime.now(UTC).isoformat(),
    }


async def _set_thread_lifecycle(
    daily_thread_id: uuid.UUID, payload: dict[str, Any]
) -> dict[str, Any]:
    import discord

    request_id = _require_request_id(payload)
    guild_id, channel_id = _validate_target(
        payload.get("guild_id"), payload.get("parent_channel_id")
    )
    thread_id = _require_snowflake(payload.get("thread_id"), "thread_id")
    desired = payload.get("desired_state")
    if desired not in {"active", "archived"}:
        raise PluginAPIError("invalid_lifecycle_state", "Lifecycle state is invalid", 422)
    _loop, _adapter, client = _discord_runtime()
    try:
        thread = await client.fetch_channel(int(thread_id))
    except discord.NotFound as exc:
        raise PluginAPIError("thread_not_found", "Daily thread was not found") from exc
    if (
        not isinstance(thread, discord.Thread)
        or str(thread.guild.id) != guild_id
        or str(thread.parent_id) != channel_id
        or thread.owner_id != getattr(getattr(client, "user", None), "id", None)
    ):
        raise PluginAPIError("stored_thread_binding_mismatch", "Daily thread binding changed")
    archived = desired == "archived"
    if bool(thread.archived) != archived:
        thread = await thread.edit(
            archived=archived,
            locked=False if not archived else thread.locked,
            reason="Docket daily thread lifecycle",
        )
    return {
        "request_id": request_id,
        "daily_thread_id": str(daily_thread_id),
        "thread_id": str(thread.id),
        "archived": bool(thread.archived),
        "verified_at": datetime.now(UTC).isoformat(),
    }


def _safe_text(value: object, maximum: int, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise PluginAPIError("invalid_embed", f"{field} exceeds its bound", 422)
    return value


def _escaped(value: str, maximum: int) -> str:
    import discord

    escaped = discord.utils.escape_mentions(discord.utils.escape_markdown(value))
    if len(escaped) > maximum:
        return escaped[: maximum - 1] + "…"
    return escaped


def _decode_control(token: str) -> tuple[uuid.UUID, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        if len(raw) != 57 or raw[0] != 2:
            raise ValueError
        return uuid.UUID(bytes=raw[1:17]), uuid.UUID(bytes=raw[17:33])
    except (ValueError, UnicodeEncodeError) as exc:
        raise PluginAPIError("invalid_control", "Approval control token is invalid", 422) from exc


def _render_embed(
    projection_id: uuid.UUID, payload: dict[str, Any]
) -> tuple[object, object | None]:
    import discord

    model = payload.get("embed")
    if not isinstance(model, dict) or set(model) - {"title", "description", "fields", "color"}:
        raise PluginAPIError("invalid_embed", "Embed model contains unsupported fields", 422)
    title = _safe_text(model.get("title"), 256, "title")
    description = _safe_text(model.get("description"), 4096, "description")
    fields = model.get("fields", [])
    if not isinstance(fields, list) or len(fields) > 25:
        raise PluginAPIError("invalid_embed", "Embed field count exceeds its bound", 422)
    escaped_title = _escaped(title, 256)
    escaped_description = _escaped(description, 4096)
    aggregate = len(escaped_title) + len(escaped_description)
    embed = discord.Embed(
        title=escaped_title,
        description=escaped_description,
        color=int(model.get("color", 0xD6A756)),
    )
    for index, field in enumerate(fields):
        if not isinstance(field, dict) or set(field) - {"name", "value", "inline"}:
            raise PluginAPIError("invalid_embed", "Embed field is invalid", 422)
        name = _safe_text(field.get("name"), 256, f"fields[{index}].name")
        value = _safe_text(field.get("value"), 1024, f"fields[{index}].value")
        escaped_name = _escaped(name, 256)
        escaped_value = _escaped(value, 1024)
        aggregate += len(escaped_name) + len(escaped_value)
        embed.add_field(
            name=escaped_name,
            value=escaped_value,
            inline=bool(field.get("inline", False)),
        )
    if aggregate >= 6000:
        raise PluginAPIError("invalid_embed", "Embed aggregate size exceeds its bound", 422)

    controls = payload.get("controls", [])
    if not isinstance(controls, list) or len(controls) not in {0, 2}:
        raise PluginAPIError("invalid_control", "Control set must be empty or Approve/Reject", 422)
    view = None
    if controls:
        decisions: set[str] = set()
        approval_ids: set[uuid.UUID] = set()
        tokens: set[str] = set()
        view = discord.ui.View(timeout=None)
        for control in controls:
            if not isinstance(control, dict) or set(control) != {
                "kind",
                "decision",
                "label",
                "approval_id",
                "token",
            }:
                raise PluginAPIError("invalid_control", "Control descriptor is invalid", 422)
            decision = str(control["decision"])
            if control["kind"] != "approval" or decision not in {"approve", "reject"}:
                raise PluginAPIError("invalid_control", "Control type is not allowed", 422)
            approval_id = uuid.UUID(str(control["approval_id"]))
            token = str(control["token"])
            token_approval, token_projection = _decode_control(token)
            if token_approval != approval_id or token_projection != projection_id:
                raise PluginAPIError("invalid_control", "Control binding does not match", 422)
            decisions.add(decision)
            approval_ids.add(approval_id)
            tokens.add(token)
            label = "Approve" if decision == "approve" else "Reject"
            if control["label"] != label:
                raise PluginAPIError("invalid_control", "Control label is not canonical", 422)
            view.add_item(
                discord.ui.Button(
                    label=label,
                    style=(
                        discord.ButtonStyle.success
                        if decision == "approve"
                        else discord.ButtonStyle.danger
                    ),
                    custom_id=f"dkt:{decision[0]}:{token}",
                )
            )
        if decisions != {"approve", "reject"} or len(approval_ids) != 1 or len(tokens) != 1:
            raise PluginAPIError("invalid_control", "Control pair is inconsistent", 422)
    footer = (
        f"docket-projection:{projection_id} | "
        f"render:{int(payload['projection_version'])}:{payload['render_sha256']} | "
        f"components:{payload['component_sha256']}"
    )
    embed.set_footer(text=footer)
    return embed, view


def _message_marker(message: object) -> str:
    embeds = getattr(message, "embeds", [])
    if len(embeds) != 1 or embeds[0].footer is None:
        return ""
    return str(embeds[0].footer.text or "")


async def _put_projection(projection_id: uuid.UUID, payload: dict[str, Any]) -> dict[str, Any]:
    import discord

    request_id = _require_request_id(payload)
    if str(payload.get("projection_id")) != str(projection_id):
        raise PluginAPIError("invalid_projection", "Projection path and body differ", 422)
    guild_id, parent_channel_id = _validate_target(
        payload.get("guild_id"), payload.get("parent_channel_id")
    )
    thread_id = _require_snowflake(payload.get("thread_id"), "thread_id")
    render_sha256 = str(payload.get("render_sha256", ""))
    component_sha256 = str(payload.get("component_sha256", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", render_sha256) or not re.fullmatch(
        r"[0-9a-f]{64}", component_sha256
    ):
        raise PluginAPIError("invalid_projection", "Projection digest is invalid", 422)
    embed, view = _render_embed(projection_id, payload)
    desired_footer = str(embed.footer.text)
    marker = f"docket-projection:{projection_id}"
    _loop, _adapter, client = _discord_runtime()
    try:
        thread = await client.fetch_channel(int(thread_id))
    except discord.NotFound as exc:
        raise PluginAPIError("thread_not_found", "Projection thread was not found") from exc
    if (
        not isinstance(thread, discord.Thread)
        or str(thread.guild.id) != guild_id
        or str(thread.parent_id) != parent_channel_id
        or thread.owner_id != getattr(getattr(client, "user", None), "id", None)
    ):
        raise PluginAPIError("stored_thread_binding_mismatch", "Projection thread binding changed")
    if thread.archived:
        thread = await thread.edit(
            archived=False, locked=False, reason="Docket projection delivery"
        )

    bot_id = getattr(getattr(client, "user", None), "id", None)
    message = None
    known_id = payload.get("known_message_id")
    if known_id is not None:
        known = _require_snowflake(known_id, "known_message_id")
        try:
            candidate = await thread.fetch_message(int(known))
        except discord.NotFound:
            candidate = None
        if candidate is not None:
            if candidate.author.id != bot_id or marker not in _message_marker(candidate):
                raise PluginAPIError(
                    "stored_projection_binding_mismatch", "Stored projection message changed"
                )
            message = candidate
    if message is None:
        matches = []
        async for candidate in thread.history(limit=None, oldest_first=True):
            if marker in _message_marker(candidate):
                matches.append(candidate)
        if len(matches) > 1 or any(candidate.author.id != bot_id for candidate in matches):
            raise PluginAPIError(
                "projection_marker_conflict", "Projection marker is foreign-owned or ambiguous"
            )
        if matches:
            message = matches[0]

    created = message is None
    allowed_mentions = discord.AllowedMentions.none()
    if message is None:
        message = await thread.send(
            embed=embed, view=view, allowed_mentions=allowed_mentions, silent=True
        )
    elif _message_marker(message) != desired_footer:
        message = await message.edit(
            content=None,
            embed=embed,
            view=view,
            allowed_mentions=allowed_mentions,
        )
    return {
        "request_id": request_id,
        "projection_id": str(projection_id),
        "guild_id": guild_id,
        "parent_channel_id": parent_channel_id,
        "thread_id": str(thread.id),
        "message_id": str(message.id),
        "render_sha256": render_sha256,
        "component_sha256": component_sha256,
        "created": created,
    }


def _post_button_response(payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{os.environ['DOCKET_INTERNAL_URL'].rstrip('/')}/internal/v1/discord/approval-responses",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {_read_token()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc).get("detail", {}).get("message")
        except (ValueError, AttributeError):
            detail = None
        raise PluginAPIError(
            "docket_interaction_rejected", detail or "Docket rejected the interaction", exc.code
        ) from exc
    if not isinstance(body, dict):
        raise PluginAPIError("invalid_docket_response", "Docket returned invalid JSON", 502)
    return body


async def _on_docket_interaction(interaction: object) -> None:
    import discord

    data = getattr(interaction, "data", None)
    custom_id = data.get("custom_id", "") if isinstance(data, dict) else ""
    match = _CONTROL_ID.fullmatch(str(custom_id))
    if match is None:
        return
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        token = match.group(2)
        approval_id, projection_id = _decode_control(token)
        guild_id, queue_channel_id, operator_id = _configured_identity()
        channel = interaction.channel
        parent_id = getattr(channel, "parent_id", None)
        message = interaction.message
        if (
            str(interaction.user.id) != operator_id
            or str(interaction.guild_id) != guild_id
            or str(parent_id) != queue_channel_id
            or not isinstance(channel, discord.Thread)
            or message is None
        ):
            raise PluginAPIError(
                "unauthorized_interaction", "This Docket control is not authorized", 403
            )
        decision = "approve" if match.group(1) == "a" else "reject"
        payload = {
            "request_id": str(uuid.uuid4()),
            "discord_interaction_id": str(interaction.id),
            "approval_id": str(approval_id),
            "approval_token": token,
            "short_code": None,
            "decision": decision,
            "discord_user_id": str(interaction.user.id),
            "guild_id": str(interaction.guild_id),
            "channel_id": str(interaction.channel_id),
            "parent_channel_id": str(parent_id),
            "projection_id": str(projection_id),
            "message_id": str(message.id),
            "responded_at": datetime.now(UTC).isoformat(),
        }
        result = await asyncio.to_thread(_post_button_response, payload)
        operation = result.get("operation_id")
        acknowledgement = (
            "Approved — queued for execution"
            if decision == "approve"
            else "Rejected — no external action queued"
        )
        if operation:
            acknowledgement += f" ({str(operation)[:8]})"
        await interaction.followup.send(acknowledgement, ephemeral=True)
    except PluginAPIError as exc:
        logger.warning("Docket button interaction failed: %s", exc.code)
        if interaction.response.is_done():
            await interaction.followup.send(str(exc), ephemeral=True)
        else:
            await interaction.response.send_message(str(exc), ephemeral=True)
    except Exception:
        logger.exception("Unexpected Docket button interaction failure")
        if interaction.response.is_done():
            await interaction.followup.send(
                "Docket could not record that decision.", ephemeral=True
            )


async def _install_interaction_listener() -> dict[str, Any]:
    global _LISTENER_CLIENT_ID
    _loop, _adapter, client = _discord_runtime()
    client_id = id(client)
    if client_id != _LISTENER_CLIENT_ID:
        client.add_listener(_on_docket_interaction, "on_interaction")
        _LISTENER_CLIENT_ID = client_id
        logger.info("Installed restart-stable Docket Discord interaction listener")
    return {"installed": True}


class _PluginRequestHandler(BaseHTTPRequestHandler):
    server_version = "DocketHermesBridge/0.3"

    def log_message(self, format: str, *args: object) -> None:
        logger.debug("Docket plugin HTTP: " + format, *args)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        authorization = self.headers.get("Authorization", "")
        supplied = authorization.removeprefix("Bearer ").strip()
        try:
            expected = _read_outbound_token()
        except (OSError, RuntimeError):
            return False
        return authorization.startswith("Bearer ") and hmac.compare_digest(supplied, expected)

    def _payload(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise PluginAPIError("invalid_request", "Content-Length is invalid", 400) from exc
        if length <= 0 or length > _MAX_REQUEST_BYTES:
            raise PluginAPIError("invalid_request", "Request body size is invalid", 413)
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PluginAPIError("invalid_json", "Request body is invalid JSON", 400) from exc
        if not isinstance(value, dict):
            raise PluginAPIError("invalid_json", "Request body must be an object", 400)
        return value

    def _handle(self, method: str) -> None:
        if not self._authorized():
            self._json(401, {"error": {"code": "unauthorized", "message": "Invalid token"}})
            return
        try:
            payload = self._payload()
            if method == "POST" and self.path == "/internal/docket/discord/threads/ensure":
                lock = _operation_lock(
                    f"thread:{payload.get('guild_id')}:{payload.get('channel_id')}:"
                    f"{payload.get('local_date')}"
                )
                with lock:
                    result = _run_on_discord(_ensure_thread(payload))
            elif method == "PUT" and (match := _THREAD_LIFECYCLE_PATH.fullmatch(self.path)):
                daily_thread_id = uuid.UUID(match.group(1))
                result = _run_on_discord(_set_thread_lifecycle(daily_thread_id, payload))
            elif method == "PUT" and (match := _PROJECTION_PATH.fullmatch(self.path)):
                projection_id = uuid.UUID(match.group(1))
                with _operation_lock(f"projection:{projection_id}"):
                    result = _run_on_discord(_put_projection(projection_id, payload))
            else:
                raise PluginAPIError("not_found", "Route not found", 404)
        except PluginAPIError as exc:
            self._json(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
            return
        except Exception:
            logger.exception("Unhandled Docket plugin request failure")
            self._json(
                500,
                {"error": {"code": "plugin_internal_error", "message": "Plugin request failed"}},
            )
            return
        self._json(200, result)

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")


def _listener_monitor() -> None:
    while True:
        try:
            _run_on_discord(_install_interaction_listener())
        except PluginAPIError:
            pass
        except Exception:
            logger.exception("Docket interaction-listener monitor failed")
        time.sleep(2)


def _start_projection_server() -> None:
    global _SERVER
    if _SERVER is not None or not os.environ.get("DOCKET_PLUGIN_BIND"):
        return
    bind = os.environ["DOCKET_PLUGIN_BIND"]
    port = int(os.environ.get("DOCKET_PLUGIN_PORT", "8787"))
    _SERVER = ThreadingHTTPServer((bind, port), _PluginRequestHandler)
    threading.Thread(target=_SERVER.serve_forever, name="docket-plugin-http", daemon=True).start()
    threading.Thread(
        target=_listener_monitor, name="docket-interaction-listener", daemon=True
    ).start()
    logger.info("Started private Docket projection listener on %s:%s", bind, port)


def register(ctx: object) -> None:
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
    _start_projection_server()
    skills_dir = Path(__file__).parent / "skills"
    for child in sorted(skills_dir.iterdir()):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            ctx.register_skill(child.name, skill_md)
