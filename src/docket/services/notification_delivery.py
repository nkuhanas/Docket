"""Leased, at-least-once webhook delivery; acceptance never means user visibility."""

import http.client
import json
import uuid
from datetime import datetime, timedelta

from cryptography.exceptions import InvalidTag
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import Settings
from docket.models import (
    MCPEventAttempt,
    MCPEventDelivery,
    MCPEventSubscription,
    OAuthPrincipal,
    PluginNotification,
)
from docket.models.base import utc_now
from docket.providers.webhooks import PinnedWebhookPoster, WebhookPoster, signed_headers
from docket.services.event_subscriptions import EventSubscriptionService
from docket.services.notifications import aware


class NotificationDispatcher:
    def __init__(
        self,
        factory: sessionmaker[Session],
        settings: Settings,
        poster: WebhookPoster | None = None,
    ) -> None:
        self.factory, self.settings = factory, settings
        self.poster = poster or PinnedWebhookPoster()
        self.subscriptions = EventSubscriptionService(factory, settings, self.poster)

    def _permitted(self, session: Session, sub: MCPEventSubscription, now: datetime) -> bool:
        owner = session.scalar(
            select(OAuthPrincipal).where(
                OAuthPrincipal.ref_id == sub.principal_ref,
            )
        )
        return bool(
            sub.status == "active"
            and aware(sub.expires_at) > now
            and owner is not None
            and owner.enabled
            and owner.valid_after == sub.grant_epoch
            and owner.operator_ref == self.settings.canonical_operator_ref
            and self.settings.plugin_read_scope in owner.scopes
            and self.settings.plugin_events_scope in owner.scopes
        )

    def run_due_once(self) -> bool:
        if not self.settings.plugin_enabled:
            return False
        now = utc_now()
        with self.factory.begin() as session:
            subscriptions = session.scalars(
                select(MCPEventSubscription)
                .where(
                    MCPEventSubscription.status == "active",
                )
                .order_by(MCPEventSubscription.expires_at)
                .limit(100)
                .with_for_update(
                    skip_locked=True,
                )
            ).all()
            for subscription in subscriptions:
                if not self._permitted(session, subscription, now):
                    self.subscriptions.stop(
                        subscription,
                        "expired" if (aware(subscription.expires_at) <= now) else "stopped",
                    )
            row = session.scalar(
                select(MCPEventDelivery)
                .where(
                    MCPEventDelivery.status.in_(("pending", "running")),
                    MCPEventDelivery.next_attempt_at <= now,
                    or_(
                        MCPEventDelivery.lease_until.is_(None), MCPEventDelivery.lease_until <= now
                    ),
                )
                .order_by(MCPEventDelivery.next_attempt_at, MCPEventDelivery.ref_id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return False
            sub = session.scalar(
                select(MCPEventSubscription).where(
                    MCPEventSubscription.ref_id == row.subscription_ref,
                )
            )
            notice = session.scalar(
                select(PluginNotification).where(
                    PluginNotification.ref_id == row.notification_ref,
                )
            )
            if (
                sub is None
                or notice is None
                or not self._permitted(session, sub, now)
                or notice.operator_ref != self.settings.canonical_operator_ref
            ):
                row.status, row.error_code = "stopped", "subscription_unavailable"
                return True
            try:
                configuration = self.subscriptions.decrypt(sub)
            except (ValueError, TypeError, InvalidTag):
                row.status, row.error_code = "failed", "subscription_configuration_unavailable"
                return True
            if row.status == "running":
                prior = session.scalar(
                    select(MCPEventAttempt).where(
                        MCPEventAttempt.delivery_ref == row.ref_id,
                        MCPEventAttempt.attempt_number == row.attempts,
                        MCPEventAttempt.status == "running",
                    )
                )
                if prior is not None:
                    prior.status, prior.finished_at = "unknown", now
                    prior.error_code = "delivery_lease_expired"
            if row.attempts >= 8:
                row.status, row.error_code = "failed", "delivery_attempt_limit"
                row.lease_token, row.lease_until = None, None
                return True
            row.status, row.lease_token = "running", uuid.uuid4()
            row.lease_until = now + timedelta(seconds=30)
            row.attempts += 1
            attempt = MCPEventAttempt(
                delivery_ref=row.ref_id,
                attempt_number=row.attempts,
                status="running",
            )
            session.add(attempt)
            session.flush()
            attempt_id = attempt.id
            delivery_id, lease, sub_ref, version = row.id, row.lease_token, sub.ref_id, sub.version
            event: dict[str, object] = {
                "eventId": notice.ref_id,
                "name": notice.event_name,
                "timestamp": aware(notice.occurred_at).isoformat(),
                "data": {"notification_ref": notice.ref_id, **notice.data},
                "cursor": None,
            }
            body = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
            previous = configuration.get("previous_secret")
            if (
                previous is not None
                and datetime.fromisoformat(configuration["previous_until"]) < now
            ):
                previous = None
                self.subscriptions.encrypt(
                    sub,
                    {
                        "url": configuration["url"],
                        "secret": configuration["secret"],
                    },
                )
        response_status = None
        error = None
        try:
            response = self.poster.post(
                configuration["url"],
                body,
                signed_headers(
                    str(event["eventId"]),
                    body,
                    configuration["secret"],
                    sub_ref,
                    previous,
                ),
            )
            response_status = response.status
        except (OSError, ValueError, http.client.HTTPException):
            error = "callback_unavailable"
        with self.factory.begin() as session:
            row = session.scalar(
                select(MCPEventDelivery)
                .where(
                    MCPEventDelivery.id == delivery_id,
                    MCPEventDelivery.lease_token == lease,
                    MCPEventDelivery.status == "running",
                )
                .with_for_update()
            )
            if row is None:
                return True
            row.lease_token, row.lease_until = None, None
            row.response_status = response_status
            completed_attempt = session.get(MCPEventAttempt, attempt_id)
            assert completed_attempt is not None
            completed_attempt.response_status = response_status
            completed_attempt.finished_at = utc_now()
            if response_status is not None and 200 <= response_status < 300:
                row.status, row.error_code = "accepted", None
                completed_attempt.status = "accepted"
            elif (
                response_status is None or response_status == 429 or response_status >= 500
            ) and row.attempts < 8:
                row.status, row.error_code = "pending", error or "callback_transient"
                completed_attempt.status, completed_attempt.error_code = "transient", row.error_code
                row.next_attempt_at = utc_now() + timedelta(
                    seconds=min(5 * 2**row.attempts, 3600),
                )
            else:
                row.status, row.error_code = "failed", error or "callback_rejected"
                completed_attempt.status, completed_attempt.error_code = "failed", row.error_code
                if response_status == 410:
                    sub = session.scalar(
                        select(MCPEventSubscription)
                        .where(
                            MCPEventSubscription.ref_id == sub_ref,
                        )
                        .with_for_update()
                    )
                    if sub is not None and sub.version == version:
                        self.subscriptions.stop(sub)
        return True

    def recover_expired_leases(self) -> int:
        """Release abandoned claims without sending, including during a deploy drain."""
        now = utc_now()
        with self.factory.begin() as session:
            rows = session.scalars(select(MCPEventDelivery).where(
                MCPEventDelivery.status == "running", MCPEventDelivery.lease_until <= now,
            ).with_for_update(skip_locked=True)).all()
            for row in rows:
                prior = session.scalar(select(MCPEventAttempt).where(
                    MCPEventAttempt.delivery_ref == row.ref_id,
                    MCPEventAttempt.attempt_number == row.attempts,
                    MCPEventAttempt.status == "running",
                ))
                if prior is not None:
                    prior.status, prior.finished_at = "unknown", now
                    prior.error_code = "delivery_lease_expired"
                row.status = "pending" if row.attempts < 8 else "failed"
                row.error_code = "delivery_lease_expired"
                row.lease_token, row.lease_until = None, None
                row.next_attempt_at = now
            return len(rows)
