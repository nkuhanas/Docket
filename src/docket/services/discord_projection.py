from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import Settings
from docket.domain.canonical import sha256_json
from docket.domain.enums import OutboxStatus
from docket.models import (
    ConversationalToolTrace,
    DiscordDailyThread,
    OperatorProjection,
    OutboxEvent,
    PersistedSemanticOption,
    ProjectionDelivery,
)
from docket.models.base import utc_now
from docket.providers.discord import DiscordProjectionAdapter, DiscordProjectionError
from docket.security import issue_semantic_option_token

_SUPPORTED_EVENTS = {
    "discord.projection.requested",
    "discord.semantic_prompt.requested",
    "discord.mcp_trace.requested",
    "discord.system_alert.requested",
    "discord.system_log.requested",
}


class DiscordProjectionRunner:
    """Deliver clean immutable OperatorProjections and operational projections."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        adapter: DiscordProjectionAdapter,
        settings: Settings,
        *,
        lease_seconds: int = 30,
    ) -> None:
        self.session_factory = session_factory
        self.adapter = adapter
        self.settings = settings
        self.lease_seconds = lease_seconds

    def _lease_one(self) -> tuple[uuid.UUID, uuid.UUID] | None:
        now = utc_now()
        with self.session_factory.begin() as session:
            event = session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.event_type.in_(_SUPPORTED_EVENTS),
                    OutboxEvent.status == OutboxStatus.PENDING.value,
                    or_(
                        OutboxEvent.next_attempt_at.is_(None),
                        OutboxEvent.next_attempt_at <= now,
                    ),
                )
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if event is None:
                return None
            token = uuid.uuid4()
            event.status = OutboxStatus.DELIVERING.value
            event.lease_token = token
            event.leased_until = now + timedelta(seconds=self.lease_seconds)
            event.attempt_count += 1
            return event.id, token

    def _event(self, session: Session, event_id: uuid.UUID, lease_token: uuid.UUID) -> OutboxEvent:
        event = session.get(OutboxEvent, event_id)
        if (
            event is None
            or event.status != OutboxStatus.DELIVERING.value
            or event.lease_token != lease_token
        ):
            raise DiscordProjectionError("delivery_lease_lost", "Outbox lease was lost")
        return event

    @staticmethod
    def _thread_name(local_date: datetime) -> str:
        value = local_date.date()
        return f"{value.isoformat()} — {value.strftime('%A')}"

    def _ensure_thread(self, event_id: uuid.UUID, lease_token: uuid.UUID) -> str:
        now = utc_now().astimezone(ZoneInfo(self.settings.timezone))
        with self.session_factory.begin() as session:
            event = self._event(session, event_id, lease_token)
            thread = session.scalar(
                select(DiscordDailyThread).where(
                    DiscordDailyThread.guild_id == self.settings.discord_guild_id,
                    DiscordDailyThread.channel_id == self.settings.queue_channel_id,
                    DiscordDailyThread.local_date == now.date(),
                )
            )
            if thread is None:
                thread = DiscordDailyThread(
                    guild_id=self.settings.discord_guild_id,
                    channel_id=self.settings.queue_channel_id,
                    local_date=now.date(),
                    thread_name=self._thread_name(now),
                    status="pending",
                )
                session.add(thread)
                session.flush()
            request = {
                "request_id": str(event.id),
                "daily_thread_id": str(thread.id),
                "known_thread_id": thread.thread_id,
                "guild_id": thread.guild_id,
                "channel_id": thread.channel_id,
                "operator_user_id": self.settings.operator_discord_user_id,
                "local_date": thread.local_date.isoformat(),
                "name": thread.thread_name,
                "thread_type": "public_thread",
                "auto_archive_minutes": 10080,
            }
            thread_id = thread.id
        ack = self.adapter.ensure_thread(request)
        if (
            ack.get("request_id") != request["request_id"]
            or ack.get("daily_thread_id") != request["daily_thread_id"]
            or not str(ack.get("thread_id", "")).isdigit()
            or ack.get("operator_joined") is not True
        ):
            raise DiscordProjectionError(
                "invalid_discord_ack", "Thread acknowledgement did not echo its binding"
            )
        with self.session_factory.begin() as session:
            self._event(session, event_id, lease_token)
            thread = session.get(DiscordDailyThread, thread_id)
            assert thread is not None
            thread.thread_id = str(ack["thread_id"])
            thread.status = "active"
            thread.auto_archive_minutes = int(ack["auto_archive_minutes"])
            thread.last_verified_at = utc_now()
            thread.last_error_code = None
            return str(thread.thread_id)

    def _complete(
        self,
        event_id: uuid.UUID,
        lease_token: uuid.UUID,
        *,
        message_id: str | None = None,
        projection_ref: str | None = None,
    ) -> None:
        with self.session_factory.begin() as session:
            event = self._event(session, event_id, lease_token)
            event.status = OutboxStatus.DELIVERED.value
            event.lease_token = None
            event.leased_until = None
            event.next_attempt_at = None
            event.last_error_code = None
            if projection_ref is not None:
                delivery = session.scalar(
                    select(ProjectionDelivery).where(
                        ProjectionDelivery.projection_ref == projection_ref,
                        ProjectionDelivery.transport == "discord",
                    )
                )
                if delivery is not None:
                    delivery.status = "delivered"
                    delivery.external_message_ref = message_id
                    delivery.delivered_at = utc_now()
                    delivery.claim_token = None
                    delivery.claimed_until = None
                    delivery.last_error_code = None

    def _deliver_projection(self, event_id: uuid.UUID, lease_token: uuid.UUID) -> None:
        thread_id = self._ensure_thread(event_id, lease_token)
        with self.session_factory() as session:
            event = self._event(session, event_id, lease_token)
            projection_ref = str(event.payload.get("projection_ref", ""))
            projection = session.scalar(
                select(OperatorProjection).where(OperatorProjection.ref_id == projection_ref)
            )
            if projection is None:
                raise DiscordProjectionError("projection_missing", "OperatorProjection is missing")
            delivery = session.scalar(
                select(ProjectionDelivery).where(
                    ProjectionDelivery.projection_ref == projection.ref_id,
                    ProjectionDelivery.transport == "discord",
                )
            )
            payload = {
                "request_id": str(event.id),
                "projection_id": str(projection.id),
                "known_message_id": (
                    delivery.external_message_ref if delivery is not None else None
                ),
                "guild_id": self.settings.discord_guild_id,
                "parent_channel_id": self.settings.queue_channel_id,
                "thread_id": thread_id,
                "projection_version": 1,
                "render_sha256": projection.render_sha256,
                "component_sha256": projection.component_sha256,
                "embed": {
                    "title": projection.semantic_content.get("title")
                    or projection.visible_text.split("\n", 1)[0],
                    "description": projection.visible_text,
                    "color": 0xD6A756,
                    "footer": (
                        f"Docket · {projection.primary_public_ref}"
                        + (
                            f" · {projection.primary_revision_ref}"
                            if projection.primary_revision_ref
                            else ""
                        )
                    ),
                },
                "controls": [],
            }
            projection_id = projection.id
        ack = self.adapter.put_projection(projection_id, payload)
        if (
            ack.get("request_id") != payload["request_id"]
            or ack.get("projection_id") != payload["projection_id"]
            or ack.get("render_sha256") != payload["render_sha256"]
            or not str(ack.get("message_id", "")).isdigit()
        ):
            raise DiscordProjectionError(
                "invalid_discord_ack", "Projection acknowledgement did not echo its binding"
            )
        self._complete(
            event_id,
            lease_token,
            message_id=str(ack["message_id"]),
            projection_ref=projection_ref,
        )

    def _deliver_semantic_prompt(self, event_id: uuid.UUID, lease_token: uuid.UUID) -> None:
        with self.session_factory() as session:
            event = self._event(session, event_id, lease_token)
            projection_ref = str(event.payload.get("projection_ref", ""))
            projection = session.scalar(
                select(OperatorProjection).where(
                    OperatorProjection.ref_id == projection_ref,
                    OperatorProjection.projection_kind == "clarification",
                )
            )
            if projection is None:
                raise DiscordProjectionError(
                    "semantic_prompt_missing", "Clarification projection is missing"
                )
            options = list(
                session.scalars(
                    select(PersistedSemanticOption)
                    .where(PersistedSemanticOption.projection_ref == projection.ref_id)
                    .order_by(PersistedSemanticOption.created_at)
                )
            )
            if not 1 <= len(options) <= 4:
                raise DiscordProjectionError(
                    "semantic_option_count_invalid", "Prompt requires one through four options"
                )
            signing_key = self.settings.read_secret(
                self.settings.interaction_signing_key_file
            ).encode()
            controls = [
                {
                    "label": f"Select {index}",
                    "custom_id": "dkt:s:"
                    + issue_semantic_option_token(
                        option_row_id=option.id,
                        actor_id=self.settings.operator_discord_user_id,
                        expires_at=utc_now() + timedelta(days=30),
                        signing_key=signing_key,
                    ),
                }
                for index, option in enumerate(options, start=1)
            ]
            render = dict(projection.semantic_content["render"])
            component_binding = dict(projection.semantic_content["component_binding"])
            delivery = session.scalar(
                select(ProjectionDelivery).where(
                    ProjectionDelivery.projection_ref == projection.ref_id,
                    ProjectionDelivery.transport == "discord",
                )
            )
            destination = delivery.destination_ref if delivery is not None else ""
            parts = destination.split(":")
            if len(parts) != 3 or parts[0] != "discord_conversation":
                raise DiscordProjectionError(
                    "semantic_prompt_target_invalid", "Prompt destination is invalid"
                )
            payload = {
                "request_id": str(event.id),
                "projection_id": str(projection.id),
                "projection_ref": projection.ref_id,
                "projection_version": 1,
                "guild_id": parts[1],
                "channel_id": parts[2],
                "parent_channel_id": (
                    self.settings.queue_channel_id
                    if parts[2] != self.settings.chat_channel_id
                    else None
                ),
                "source_message_id": (
                    delivery.source_message_ref if delivery is not None else None
                ),
                "known_message_id": (
                    delivery.external_message_ref if delivery is not None else None
                ),
                "operator_user_id": self.settings.operator_discord_user_id,
                "render_sha256": projection.render_sha256,
                "component_sha256": projection.component_sha256,
                "render": render,
                "controls": controls,
                "component_binding": component_binding,
            }
            projection_id = projection.id
        ack = self.adapter.put_semantic_prompt(projection_id, payload)
        if (
            ack.get("request_id") != payload["request_id"]
            or ack.get("projection_ref") != payload["projection_ref"]
            or ack.get("render_sha256") != payload["render_sha256"]
            or not str(ack.get("message_id", "")).isdigit()
        ):
            raise DiscordProjectionError(
                "invalid_discord_ack", "Prompt acknowledgement did not echo its binding"
            )
        self._complete(
            event_id,
            lease_token,
            message_id=str(ack["message_id"]),
            projection_ref=projection_ref,
        )

    @staticmethod
    def _bounded(value: object, limit: int) -> str:
        return str(value)[:limit]

    def _deliver_trace(self, event_id: uuid.UUID, lease_token: uuid.UUID) -> None:
        with self.session_factory() as session:
            event = self._event(session, event_id, lease_token)
            trace = session.get(ConversationalToolTrace, event.aggregate_id)
            if trace is None:
                raise DiscordProjectionError("mcp_trace_missing", "MCP trace is missing")
            calls = [
                {
                    "ordinal": int(call["ordinal"]),
                    "tool_name": self._bounded(call["tool_name"], 128),
                    "transport_state": str(call.get("transport_state", "completed")),
                    "domain_state": str(call.get("domain_state", "unknown")),
                    "outcome": self._bounded(
                        call.get("disposition") or call.get("domain_error_code") or "unknown",
                        128,
                    ),
                    "elapsed_ms": min(max(int(call.get("elapsed_ms", 0)), 0), 600000),
                    "tool_call_ref": self._bounded(call.get("tool_call_ref") or "unreconciled", 40),
                    "argument_preview": self._bounded(call.get("argument_preview", "{}"), 768),
                }
                for call in trace.calls[:20]
            ]
            render = {
                "title": "Docket tool activity",
                "summary": f"{trace.last_ordinal} authenticated Docket calls",
                "status": trace.status.title(),
                "calls": calls,
                "overflow_count": max(0, trace.last_ordinal - len(calls)),
                "updated_at": (trace.completed_at or trace.updated_at).astimezone(UTC).isoformat(),
            }
            payload = {
                "request_id": str(event.id),
                "trace_ref": trace.ref_id,
                "guild_id": self.settings.discord_guild_id,
                "channel_id": self.settings.system_channel_id,
                "render": render,
                "render_sha256": sha256_json(render),
            }
            trace_ref = trace.ref_id
        ack = self.adapter.put_mcp_trace(trace_ref, payload)
        if (
            ack.get("request_id") != payload["request_id"]
            or ack.get("trace_ref") != trace_ref
            or not str(ack.get("message_id", "")).isdigit()
        ):
            raise DiscordProjectionError(
                "invalid_discord_ack", "Trace acknowledgement did not echo its binding"
            )
        self._complete(event_id, lease_token)

    def _deliver_operational(
        self, event_id: uuid.UUID, lease_token: uuid.UUID, *, log: bool
    ) -> None:
        with self.session_factory() as session:
            event = self._event(session, event_id, lease_token)
            render = {
                "title": self._bounded(event.payload.get("title", "Docket update"), 256),
                "summary": self._bounded(event.payload.get("summary", ""), 2000),
                "occurred_at": str(event.payload.get("occurred_at", event.created_at.isoformat())),
            }
            payload = {
                "request_id": str(event.id),
                ("log_id" if log else "alert_id"): str(event.aggregate_id),
                "guild_id": self.settings.discord_guild_id,
                "channel_id": self.settings.system_channel_id,
                "render_sha256": sha256_json(render),
                **({"render": render} if log else render),
            }
        ack = (
            self.adapter.post_system_log(payload)
            if log
            else self.adapter.post_system_alert(payload)
        )
        if not str(ack.get("message_id", "")).isdigit():
            raise DiscordProjectionError("invalid_discord_ack", "Operational ack is invalid")
        self._complete(event_id, lease_token)

    def _retry(self, event_id: uuid.UUID, lease_token: uuid.UUID, code: str) -> None:
        with self.session_factory.begin() as session:
            event = self._event(session, event_id, lease_token)
            event.status = OutboxStatus.PENDING.value
            event.lease_token = None
            event.leased_until = None
            event.next_attempt_at = utc_now() + timedelta(
                seconds=min(300, 2 ** min(event.attempt_count, 8))
            )
            event.last_error_code = code[:128]

    def run_due_once(self) -> bool:
        lease = self._lease_one()
        if lease is None:
            return False
        event_id, token = lease
        try:
            with self.session_factory() as session:
                event_type = self._event(session, event_id, token).event_type
            if event_type == "discord.projection.requested":
                self._deliver_projection(event_id, token)
            elif event_type == "discord.semantic_prompt.requested":
                self._deliver_semantic_prompt(event_id, token)
            elif event_type == "discord.mcp_trace.requested":
                self._deliver_trace(event_id, token)
            elif event_type == "discord.system_log.requested":
                self._deliver_operational(event_id, token, log=True)
            else:
                self._deliver_operational(event_id, token, log=False)
        except DiscordProjectionError as exc:
            self._retry(event_id, token, exc.code)
        return True

    def recover_expired_leases(self) -> int:
        now = utc_now()
        recovered = 0
        with self.session_factory.begin() as session:
            for event in session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.event_type.in_(_SUPPORTED_EVENTS),
                    OutboxEvent.status == OutboxStatus.DELIVERING.value,
                    OutboxEvent.leased_until < now,
                )
            ):
                event.status = OutboxStatus.PENDING.value
                event.lease_token = None
                event.leased_until = None
                event.next_attempt_at = now
                event.last_error_code = "delivery_lease_expired"
                recovered += 1
        return recovered

    def enqueue_stale_projection_repairs(self, *, limit: int = 100) -> int:
        """Clean projections are immutable and their delivery rows already retry."""
        del limit
        return 0
