from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

import httpx


class DiscordProjectionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DiscordProjectionAdapter(Protocol):
    def ensure_thread(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def set_thread_lifecycle(
        self, daily_thread_id: uuid.UUID, payload: dict[str, Any]
    ) -> dict[str, Any]: ...

    def put_projection(
        self, projection_id: uuid.UUID, payload: dict[str, Any]
    ) -> dict[str, Any]: ...

    def post_system_alert(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def post_calendar_reminder(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class HttpDiscordProjectionAdapter:
    def __init__(self, base_url: str, token: str, *, timeout_seconds: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def _request(self, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                json=payload,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise DiscordProjectionError("discord_transport_error", str(exc)) from exc
        if response.status_code >= 400:
            try:
                body = response.json()
                error = body.get("error", {})
                code = str(error.get("code", "discord_plugin_error"))
                message = str(error.get("message", f"HTTP {response.status_code}"))
            except (ValueError, AttributeError):
                code = "discord_plugin_error"
                message = f"Hermes plugin returned HTTP {response.status_code}"
            raise DiscordProjectionError(code, message)
        try:
            result = response.json()
        except ValueError as exc:
            raise DiscordProjectionError(
                "invalid_discord_ack", "Hermes plugin returned invalid JSON"
            ) from exc
        if not isinstance(result, dict):
            raise DiscordProjectionError(
                "invalid_discord_ack", "Hermes plugin acknowledgement was not an object"
            )
        return result

    def ensure_thread(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/internal/docket/discord/threads/ensure", payload)

    def set_thread_lifecycle(
        self, daily_thread_id: uuid.UUID, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/internal/docket/discord/threads/{daily_thread_id}/lifecycle",
            payload,
        )

    def put_projection(self, projection_id: uuid.UUID, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "PUT", f"/internal/docket/discord/projections/{projection_id}", payload
        )

    def post_system_alert(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/internal/docket/discord/system-alerts", payload)

    def post_calendar_reminder(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/internal/docket/discord/notifications", payload)


@dataclass
class FakeDiscordBackend:
    threads: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    system_messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    notification_messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    next_snowflake: int = 10000000000000000

    def snowflake(self) -> str:
        self.next_snowflake += 1
        return str(self.next_snowflake)


class FakeDiscordProjectionAdapter:
    """Stateful fake whose backend can survive adapter replacement/restart."""

    def __init__(self, backend: FakeDiscordBackend | None = None) -> None:
        self.backend = backend or FakeDiscordBackend()
        self.discard_next_thread_ack = False
        self.discard_next_projection_ack = False
        self.discard_next_notification_ack = False

    @staticmethod
    def _check_request(payload: dict[str, Any]) -> None:
        uuid.UUID(str(payload["request_id"]))

    def ensure_thread(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._check_request(payload)
        local_date = date.fromisoformat(str(payload["local_date"]))
        expected_name = f"{local_date.isoformat()} — {local_date.strftime('%A')}"
        if payload["thread_type"] != "public_thread" or payload["name"] != expected_name:
            raise DiscordProjectionError("invalid_thread_request", "Invalid thread contract")
        key = (payload["guild_id"], payload["channel_id"], payload["local_date"])
        created = key not in self.backend.threads
        if created:
            self.backend.threads[key] = {
                "thread_id": self.backend.snowflake(),
                "name": payload["name"],
                "archived": False,
                "auto_archive_minutes": payload["auto_archive_minutes"],
            }
        thread = self.backend.threads[key]
        unarchived = bool(thread["archived"])
        thread["archived"] = False
        result = {
            "request_id": payload["request_id"],
            "daily_thread_id": payload["daily_thread_id"],
            "guild_id": payload["guild_id"],
            "channel_id": payload["channel_id"],
            "thread_id": thread["thread_id"],
            "created": created,
            "unarchived": unarchived,
            "auto_archive_minutes": thread["auto_archive_minutes"],
            "verified_at": "2026-07-21T12:00:00+00:00",
        }
        if self.discard_next_thread_ack:
            self.discard_next_thread_ack = False
            raise DiscordProjectionError("discarded_ack", "Thread acknowledgement discarded")
        return copy.deepcopy(result)

    def set_thread_lifecycle(
        self, daily_thread_id: uuid.UUID, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._check_request(payload)
        matches = [
            thread
            for thread in self.backend.threads.values()
            if thread["thread_id"] == payload["thread_id"]
        ]
        if len(matches) != 1:
            raise DiscordProjectionError("thread_not_found", "Thread was not found")
        thread = matches[0]
        thread["archived"] = payload["desired_state"] == "archived"
        return {
            "request_id": payload["request_id"],
            "daily_thread_id": str(daily_thread_id),
            "thread_id": thread["thread_id"],
            "archived": thread["archived"],
            "verified_at": "2026-07-21T12:00:00+00:00",
        }

    def put_projection(self, projection_id: uuid.UUID, payload: dict[str, Any]) -> dict[str, Any]:
        self._check_request(payload)
        key = str(projection_id)
        created = key not in self.backend.messages
        if created:
            self.backend.messages[key] = {
                "message_id": self.backend.snowflake(),
                "thread_id": payload["thread_id"],
                "render_sha256": payload["render_sha256"],
                "component_sha256": payload["component_sha256"],
                "embed": copy.deepcopy(payload["embed"]),
                "controls": copy.deepcopy(payload["controls"]),
            }
        else:
            message = self.backend.messages[key]
            if message["thread_id"] != payload["thread_id"]:
                raise DiscordProjectionError(
                    "projection_target_changed", "Projection target changed"
                )
            message["render_sha256"] = payload["render_sha256"]
            message["component_sha256"] = payload["component_sha256"]
            message["embed"] = copy.deepcopy(payload["embed"])
            message["controls"] = copy.deepcopy(payload["controls"])
        message = self.backend.messages[key]
        result = {
            "request_id": payload["request_id"],
            "projection_id": key,
            "guild_id": payload["guild_id"],
            "parent_channel_id": payload["parent_channel_id"],
            "thread_id": payload["thread_id"],
            "message_id": message["message_id"],
            "render_sha256": message["render_sha256"],
            "component_sha256": message["component_sha256"],
            "created": created,
        }
        if self.discard_next_projection_ack:
            self.discard_next_projection_ack = False
            raise DiscordProjectionError("discarded_ack", "Projection acknowledgement discarded")
        return copy.deepcopy(result)

    def post_system_alert(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._check_request(payload)
        key = str(payload["alert_id"])
        created = key not in self.backend.system_messages
        if created:
            self.backend.system_messages[key] = {
                "message_id": self.backend.snowflake(),
                "render_sha256": payload["render_sha256"],
            }
        message = self.backend.system_messages[key]
        message["render_sha256"] = payload["render_sha256"]
        return {
            "request_id": payload["request_id"],
            "alert_id": key,
            "guild_id": payload["guild_id"],
            "channel_id": payload["channel_id"],
            "message_id": message["message_id"],
            "render_sha256": message["render_sha256"],
            "created": created,
        }

    def post_calendar_reminder(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._check_request(payload)
        key = str(payload["notification_id"])
        created = key not in self.backend.notification_messages
        if created:
            self.backend.notification_messages[key] = {
                "message_id": self.backend.snowflake(),
                "render_sha256": payload["render_sha256"],
                "render": copy.deepcopy(payload["render"]),
            }
        message = self.backend.notification_messages[key]
        message["render_sha256"] = payload["render_sha256"]
        message["render"] = copy.deepcopy(payload["render"])
        result = {
            "request_id": payload["request_id"],
            "notification_id": key,
            "guild_id": payload["guild_id"],
            "channel_id": payload["channel_id"],
            "message_id": message["message_id"],
            "render_sha256": message["render_sha256"],
            "created": created,
        }
        if self.discard_next_notification_ack:
            self.discard_next_notification_ack = False
            raise DiscordProjectionError("discarded_ack", "Notification acknowledgement discarded")
        return copy.deepcopy(result)
