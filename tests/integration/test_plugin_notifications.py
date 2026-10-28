import base64
import json
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from docket.agent_auth import AgentCallContext, AgentPrincipal, agent_call_context
from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.models import (
    AuditEvent,
    AuthenticatedRequest,
    MCPEventAttempt,
    MCPEventDelivery,
    MCPEventSubscription,
    OAuthPrincipal,
    OperatorProjection,
    OutboxEvent,
    PluginNotification,
    ProjectionDelivery,
)
from docket.models.base import utc_now
from docket.providers.webhooks import WebhookResponse
from docket.services.event_subscriptions import EventSubscriptionService
from docket.services.notification_delivery import NotificationDispatcher
from docket.services.notifications import NotificationService, route_projection
from docket.services.oauth import OAuthPrincipalService

SECRET = "whsec_" + base64.b64encode(b"synthetic-signing-secret-32-bytes!").decode()
REPLACEMENT = "whsec_" + base64.b64encode(b"replacement-signing-key-32-bytes!!").decode()
URL = "https://receiver.example.com/callback"
EVENT = "docket.attention.created"


class RecordingPoster:
    def __init__(self):
        self.calls = []
        self.statuses = []
        self.echo = True

    def post(self, url, body, headers):
        self.calls.append((url, body, headers))
        value = json.loads(body)
        if value.get("type") == "verification":
            return WebhookResponse(
                200,
                json.dumps(
                    {
                        "challenge": value["challenge"] if self.echo else "wrong",
                    }
                ).encode(),
            )
        return WebhookResponse(self.statuses.pop(0) if self.statuses else 202)


@contextmanager
def as_principal(principal):
    token = agent_call_context.set(AgentCallContext(principal))
    try:
        yield
    finally:
        agent_call_context.reset(token)


@pytest.fixture
def notification_connection(session_factory):
    settings = get_settings().model_copy(
        update={
            "plugin_enabled": True,
            "plugin_oauth_issuer": "https://identity.test/",
            "discord_adapter_enabled": False,
        }
    )
    with session_factory.begin() as session:
        owner = OAuthPrincipalService(session, settings).approve("notification-owner", events=True)
        session.flush()
        principal = AgentPrincipal(
            owner.ref_id,
            owner.operator_ref,
            "read_only",
            frozenset({"read", "events"}),
            integration="oauth",
            grant_epoch=owner.valid_after,
        )
    poster = RecordingPoster()
    service = EventSubscriptionService(session_factory, settings, poster)
    return settings, principal, poster, service


def subscribe(service, **kwargs):
    return service.subscribe(name=EVENT, arguments={}, url=URL, secret=SECRET, **kwargs)


def publish(factory, settings, *, key="test-notice", subject=None):
    with factory.begin() as session:
        return (
            NotificationService(session, settings)
            .publish(
                EVENT,
                subject or new_public_ref("case"),
                key=key,
            )
            .ref_id
        )


def test_subscription_refresh_cache_encryption_rotation_and_purge(
    session_factory,
    notification_connection,
):
    _settings, principal, poster, service = notification_connection
    with as_principal(principal):
        first = subscribe(service)
        assert subscribe(service)["id"] == first["id"]
        assert len(poster.calls) == 1
        with session_factory.begin() as session:
            row = session.scalar(select(MCPEventSubscription))
            row.verified_at = utc_now() - timedelta(minutes=6)
        assert subscribe(service)["id"] == first["id"]
        assert len(poster.calls) == 2
        rotated = service.subscribe(name=EVENT, arguments={}, url=URL, secret=REPLACEMENT)
        assert rotated["id"] == first["id"]
        assert service.subscribe(
            name=EVENT, arguments={}, url=URL, secret=REPLACEMENT,
        )["id"] == first["id"]
        assert len(poster.calls) == 3
        with session_factory() as session:
            row = session.scalar(select(MCPEventSubscription))
            assert SECRET.encode() not in row.configuration
            assert URL.encode() not in row.configuration
            config = service.decrypt(row)
            assert config["secret"] == REPLACEMENT and config["previous_secret"] == SECRET
            audits = session.scalars(select(AuditEvent)).all()
            assert SECRET not in repr([audit.data for audit in audits])
        for _ in range(2):
            assert service.unsubscribe(name=EVENT, arguments={}, url=URL) == {}
        with session_factory() as session:
            row = session.scalar(select(MCPEventSubscription))
            assert row.status == "stopped" and row.configuration == row.nonce == b""
            assert session.scalar(select(func.count()).select_from(AuthenticatedRequest)) == 0


def test_failed_callback_and_background_scope_cannot_activate(
    session_factory,
    notification_connection,
):
    _, principal, poster, service = notification_connection
    poster.echo = False
    with as_principal(principal), pytest.raises(DocketError) as rejected:
        subscribe(service)
    assert rejected.value.code == "callback_verification_failed"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(MCPEventSubscription)) == 0
    denied = AgentPrincipal(
        principal.principal_ref,
        principal.operator_ref,
        "read_only",
        frozenset({"read"}),
        integration="oauth",
    )
    with as_principal(denied), pytest.raises(DocketError):
        subscribe(service)
    with as_principal(principal), pytest.raises(DocketError):
        subscribe(service, cursor="invented-replay")


def test_notification_atomicity_dedup_filters_inbox_and_no_discord(
    session_factory,
    notification_connection,
):
    settings, principal, _, service = notification_connection
    subject = new_public_ref("case")
    with as_principal(principal):
        service.subscribe(name=EVENT, arguments={"subject_ref": subject}, url=URL, secret=SECRET)
    with pytest.raises(RuntimeError), session_factory.begin() as session:
        NotificationService(session, settings).publish(EVENT, subject, key="rollback")
        raise RuntimeError("abort canonical transaction")
    first = publish(session_factory, settings, key="first", subject=subject)
    assert publish(session_factory, settings, key="first", subject=subject) == first
    second = publish(session_factory, settings, key="second")
    with session_factory.begin() as session:
        projection = OperatorProjection(
            projection_kind="attention_case",
            operator_ref=principal.operator_ref,
            primary_public_ref=subject,
            semantic_content={},
            visible_text="Untrusted title",
            render_schema_version=1,
            render_sha256="0" * 64,
            component_sha256="0" * 64,
            basis_refs=[subject],
        )
        session.add(projection)
        session.flush()
        assert route_projection(session, settings, projection) is None
    with session_factory() as session, as_principal(principal):
        notices = session.scalars(select(PluginNotification)).all()
        assert len(notices) == 3
        assert all("Untrusted title" not in repr(row.data) for row in notices)
        assert session.scalar(select(func.count()).select_from(MCPEventDelivery)) == 2
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 0
        assert session.scalar(select(func.count()).select_from(ProjectionDelivery)) == 0
        inbox = NotificationService(session, settings)
        page = inbox.list(limit=2)
        rest = inbox.list(limit=2, cursor=page["cursor"])
        assert len(page["items"]) == 2 and len(rest["items"]) == 1
        assert {r["ref"] for r in page["items"] + rest["items"]} >= {first, second}
        assert "not_operator_read" in inbox.get(first)["delivery_semantics"]
        with pytest.raises(DocketError):
            inbox.list(cursor="bad")


@pytest.mark.parametrize("terminal", [202, 410, 413])
def test_delivery_retry_identity_acceptance_and_permanent_rejection(
    session_factory,
    notification_connection,
    terminal,
    monkeypatch,
):
    settings, principal, poster, service = notification_connection
    with as_principal(principal):
        subscribe(service)
    notice = publish(session_factory, settings)
    dispatcher = NotificationDispatcher(session_factory, settings, poster)
    poster.statuses = [503, terminal]
    monkeypatch.setattr("docket.providers.webhooks.time.time", lambda: 1800000000)
    assert dispatcher.run_due_once()
    with session_factory.begin() as session:
        delivery = session.scalar(select(MCPEventDelivery))
        assert delivery.status == "pending"
        delivery.next_attempt_at = utc_now() - timedelta(seconds=1)
    monkeypatch.setattr("docket.providers.webhooks.time.time", lambda: 1800000010)
    assert dispatcher.run_due_once()
    assert not dispatcher.run_due_once()
    first, retry = poster.calls[-2:]
    assert first[1] == retry[1]
    assert json.loads(first[1])["eventId"] == notice == retry[2]["webhook-id"]
    assert first[2]["webhook-signature"] != retry[2]["webhook-signature"]
    with session_factory() as session:
        delivery = session.scalar(select(MCPEventDelivery))
        assert delivery.status == ("accepted" if terminal == 202 else "failed")
        attempts = session.scalars(
            select(MCPEventAttempt).order_by(
                MCPEventAttempt.attempt_number,
            )
        ).all()
        assert [r.status for r in attempts] == ["transient", delivery.status]
        sub = session.scalar(select(MCPEventSubscription))
        assert sub.status == ("stopped" if terminal == 410 else "active")
        if terminal == 410:
            assert sub.configuration == b""
        assert session.scalar(select(func.count()).select_from(PluginNotification)) == 1
        attempts[0].error_code = "rewritten"
        with pytest.raises(ValueError, match="immutable"):
            session.flush()


@pytest.mark.parametrize("reason", ["revoked", "expired", "new_grant"])
def test_lifetime_revocation_stops_future_delivery_and_purges_secret(
    session_factory,
    notification_connection,
    reason,
):
    settings, principal, poster, service = notification_connection
    with as_principal(principal):
        subscribe(service)
    publish(session_factory, settings)
    with session_factory.begin() as session:
        if reason == "expired":
            sub = session.scalar(select(MCPEventSubscription))
            sub.expires_at = utc_now() - timedelta(seconds=1)
        else:
            owner = session.scalar(select(OAuthPrincipal))
            if reason == "revoked":
                owner.enabled = False
            else:
                owner.valid_after += 1
    dispatcher = NotificationDispatcher(session_factory, settings, poster)
    assert dispatcher.run_due_once()
    assert len(poster.calls) == 1  # verification only
    with session_factory() as session:
        sub = session.scalar(select(MCPEventSubscription))
        assert sub.status != "active" and sub.configuration == b""
        assert session.scalar(select(MCPEventDelivery)).status == "stopped"


def test_expired_delivery_lease_retains_unknown_attempt_and_replays_same_event(
    session_factory,
    notification_connection,
):
    settings, principal, poster, service = notification_connection
    with as_principal(principal):
        subscribe(service)
    notice = publish(session_factory, settings)
    with session_factory.begin() as session:
        delivery = session.scalar(select(MCPEventDelivery))
        delivery.status, delivery.attempts = "running", 1
        delivery.lease_until = utc_now() - timedelta(seconds=1)
        session.add(
            MCPEventAttempt(
                delivery_ref=delivery.ref_id,
                attempt_number=1,
                status="running",
            )
        )
    dispatcher = NotificationDispatcher(session_factory, settings, poster)
    assert dispatcher.recover_expired_leases() == 1
    assert len(poster.calls) == 1
    assert dispatcher.recover_expired_leases() == 0
    assert dispatcher.run_due_once()
    assert json.loads(poster.calls[-1][1])["eventId"] == notice
    with session_factory() as session:
        assert [
            r.status
            for r in session.scalars(
                select(MCPEventAttempt).order_by(
                    MCPEventAttempt.attempt_number,
                )
            )
        ] == ["unknown", "accepted"]


def test_inbox_pages_large_alerts_by_bytes_without_skipping_records(
    session_factory, notification_connection,
):
    settings, principal, _, _ = notification_connection
    expected = set()
    with session_factory.begin() as session:
        for index in range(60):
            notice = NotificationService(session, settings).system_alert(
                new_public_ref("aud"), key=f"large-alert:{index}", summary="é" * 512,
                occurred_at=utc_now(),
            )
            expected.add(notice.ref_id)
    seen = []
    cursor = None
    with session_factory() as session, as_principal(principal):
        for _ in range(61):
            page = NotificationService(session, settings).list(limit=100, cursor=cursor)
            assert len(json.dumps(page, separators=(",", ":")).encode()) <= 12288
            seen.extend(item["ref"] for item in page["items"])
            cursor = page["cursor"]
            if not page["truncated"]:
                break
            assert cursor and page["items"]
        else:
            pytest.fail("Inbox pagination did not terminate")
    assert len(seen) == len(set(seen)) == 60
    assert set(seen) == expected
