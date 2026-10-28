"""Retained notification snapshots and independently recoverable delivery intents."""

import base64
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.config import Settings
from docket.domain.errors import DocketError
from docket.domain.public_refs import is_public_ref
from docket.models import (
    MCPEventDelivery,
    MCPEventSubscription,
    OAuthPrincipal,
    OperatorProjection,
    OutboxEvent,
    PluginNotification,
    ProjectionDelivery,
)
from docket.models.base import utc_now

EVENTS = {
    "docket.system.alert": "Docket has an operational alert.",
    "docket.triage.ready": "A source is ready for bounded non-authoritative triage.",
    "docket.attention.created": "An attention case has a new revision.",
    "docket.brief.ready": "A daily brief is ready.",
    "docket.reminder.due": "A committed reminder is due.",
    "docket.operation.status_changed": "A committed provider operation changed status.",
}


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def identity_lock(session: Session, value: str) -> None:
    if session.get_bind().dialect.name == "postgresql":
        lock = int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big") & ((1 << 63) - 1)
        session.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": lock})


class NotificationService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session, self.settings = session, settings

    def publish(
        self, name: str, subject_ref: str, *, key: str, occurred_at: datetime | None = None,
        summary: str | None = None,
    ) -> PluginNotification | None:
        if not self.settings.plugin_enabled:
            return None
        if name not in EVENTS or not is_public_ref(subject_ref) or len(key) > 255:
            raise ValueError("A supported event, public subject and bounded identity are required")
        if summary is not None and (name != "docket.system.alert" or not 1 <= len(summary) <= 512):
            raise ValueError("Only bounded system alert text can override a summary")
        identity_lock(self.session, "notification:" + key)
        existing = self.session.scalar(
            select(PluginNotification).where(
                PluginNotification.deduplication_key == key,
            )
        )
        if existing is not None:
            if (
                existing.event_name != name
                or existing.subject_ref != subject_ref
                or existing.operator_ref != self.settings.canonical_operator_ref
            ):
                raise ValueError("A notification identity cannot change its meaning")
            return existing
        now = utc_now()
        notice = PluginNotification(
            operator_ref=self.settings.canonical_operator_ref,
            event_name=name,
            subject_ref=subject_ref,
            summary=summary or EVENTS[name],
            deduplication_key=key,
            data={"subject_ref": subject_ref, "summary": summary or EVENTS[name]},
            occurred_at=occurred_at or now,
        )
        self.session.add(notice)
        self.session.flush()
        subscriptions = self.session.scalars(
            select(MCPEventSubscription)
            .join(
                OAuthPrincipal,
                OAuthPrincipal.ref_id == MCPEventSubscription.principal_ref,
            )
            .where(
                MCPEventSubscription.event_name == name,
                MCPEventSubscription.status == "active",
                MCPEventSubscription.expires_at > now,
                OAuthPrincipal.enabled.is_(True),
                OAuthPrincipal.operator_ref == notice.operator_ref,
                OAuthPrincipal.valid_after == MCPEventSubscription.grant_epoch,
            )
        ).all()
        for subscription in subscriptions:
            target = subscription.arguments.get("subject_ref")
            if target is None or target == subject_ref:
                self.session.add(
                    MCPEventDelivery(
                        notification_ref=notice.ref_id,
                        subscription_ref=subscription.ref_id,
                    )
                )
        return notice

    def system_alert(self, subject_ref: str, *, key: str, summary: str,
                     occurred_at: datetime) -> PluginNotification | None:
        """Retain the first observation of an alert episode across later retries."""
        if not self.settings.plugin_enabled:
            return None
        if len(key) > 255:
            raise ValueError("A bounded alert identity is required")
        identity_lock(self.session, "notification:" + key)
        existing = self.session.scalar(select(PluginNotification).where(
            PluginNotification.deduplication_key == key,
        ))
        if existing is not None:
            if (existing.event_name != "docket.system.alert"
                    or existing.operator_ref != self.settings.canonical_operator_ref):
                raise ValueError("An alert identity cannot change its owner or meaning")
            return existing
        return self.publish("docket.system.alert", subject_ref, key=key,
                            summary=summary, occurred_at=occurred_at)

    def _view(self, notice: PluginNotification) -> dict[str, Any]:
        return {
            "ref": notice.ref_id,
            "event_name": notice.event_name,
            "subject_ref": notice.subject_ref,
            "summary": notice.summary,
            "occurred_at": aware(notice.occurred_at).isoformat(),
            "delivery_semantics": "inbox_available; webhook_acceptance_is_not_operator_read",
        }

    def list(self, *, limit: int = 25, cursor: str | None = None) -> dict[str, Any]:
        principal = require_principal("read")
        limit = max(1, min(limit, 100))
        query = select(PluginNotification).where(
            PluginNotification.operator_ref == principal.operator_ref,
        )
        if cursor:
            try:
                if len(cursor) > 256:
                    raise ValueError("cursor")
                stamp, ref = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                before = datetime.fromisoformat(stamp)
                if not is_public_ref(ref) or not ref.startswith("notice_"):
                    raise ValueError("cursor")
            except (ValueError, TypeError, UnicodeError) as exc:
                raise DocketError(code="invalid_cursor", message="Invalid inbox cursor.") from exc
            query = query.where(
                or_(
                    PluginNotification.created_at < before,
                    and_(PluginNotification.created_at == before, PluginNotification.ref_id < ref),
                )
            )
        rows = self.session.scalars(
            query.order_by(
                PluginNotification.created_at.desc(),
                PluginNotification.ref_id.desc(),
            ).limit(limit + 1)
        ).all()
        page: list[PluginNotification] = []
        byte_count = 512  # Reserve room for the envelope and continuation cursor.
        for row in rows[:limit]:
            size = len(json.dumps(self._view(row), separators=(",", ":")).encode())
            if byte_count + size > 12288:
                break
            page.append(row)
            byte_count += size + 1
        truncated = len(rows) > len(page)
        next_cursor = None
        if truncated and page:
            last = page[-1]
            raw = json.dumps([aware(last.created_at).isoformat(), last.ref_id]).encode()
            next_cursor = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        return {
            "ok": True,
            "items": [self._view(row) for row in page],
            "count": len(page),
            "truncated": truncated,
            "cursor": next_cursor,
        }

    def get(self, ref: str) -> dict[str, Any]:
        principal = require_principal("read")
        row = self.session.scalar(
            select(PluginNotification).where(
                PluginNotification.ref_id == ref,
                PluginNotification.operator_ref == principal.operator_ref,
            )
        )
        if row is None:
            raise DocketError(code="not_found", message="Docket notification not found.")
        return {"ok": True, **self._view(row)}


def route_projection(
    session: Session,
    settings: Settings,
    projection: OperatorProjection,
    *,
    deduplication_key: str | None = None,
) -> OutboxEvent | None:
    names = {
        "attention_case": "docket.attention.created",
        "daily_brief": "docket.brief.ready",
        "reminder": "docket.reminder.due",
    }
    if projection.projection_kind in names:
        NotificationService(session, settings).publish(
            names[projection.projection_kind],
            projection.primary_public_ref,
            key=f"projection:{projection.ref_id}",
            occurred_at=projection.created_at,
        )
    if not settings.discord_adapter_enabled:
        return None
    session.add(
        ProjectionDelivery(
            projection_id=projection.id,
            projection_ref=projection.ref_id,
            transport="discord",
            destination_ref=f"discord_conversation:{settings.discord_guild_id}:{settings.queue_channel_id}",
            status="pending",
        )
    )
    outbox = OutboxEvent(
        event_type="discord.projection.requested",
        aggregate_type="operator_projection",
        aggregate_id=projection.id,
        deduplication_key=deduplication_key or f"discord_projection:{projection.ref_id}",
        payload={"projection_ref": projection.ref_id},
        status="pending",
    )
    session.add(outbox)
    return outbox
