"""PostgreSQL claims, retained notification history and lossless downgrade guards."""

import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from docket.agent_auth import AgentCallContext, AgentPrincipal, agent_call_context
from docket.config import get_settings
from docket.domain.public_refs import new_public_ref
from docket.models import MCPEventAttempt, MCPEventDelivery
from docket.providers.webhooks import WebhookResponse
from docket.services.event_subscriptions import EventSubscriptionService
from docket.services.notification_delivery import NotificationDispatcher
from docket.services.notifications import NotificationService
from docket.services.oauth import OAuthPrincipalService


def test_notifications_claim_once_and_retain_postgresql_history(factory) -> None:
    settings = get_settings().model_copy(
        update={
            "plugin_enabled": True,
            "plugin_oauth_issuer": "https://identity.test/",
        }
    )
    reached, release = threading.Event(), threading.Event()

    class Poster:
        deliveries = 0

        def post(self, _url, body, _headers):
            data = json.loads(body)
            if data.get("type") == "verification":
                return WebhookResponse(200, json.dumps({"challenge": data["challenge"]}).encode())
            self.deliveries += 1
            reached.set()
            assert release.wait(timeout=10), "Synthetic callback was not released"
            return WebhookResponse(202)

    poster = Poster()
    with factory.begin() as session:
        owner = OAuthPrincipalService(session, settings).approve("synthetic-events", events=True)
        session.flush()
        principal = AgentPrincipal(
            owner.ref_id,
            owner.operator_ref,
            "read_only",
            frozenset({"read", "events"}),
            integration="oauth",
            grant_epoch=owner.valid_after,
        )
    token = agent_call_context.set(AgentCallContext(principal))
    try:
        sub = EventSubscriptionService(factory, settings, poster).subscribe(
            name="docket.attention.created",
            arguments={},
            url="https://receiver.example.com/callback",
            secret="whsec_" + base64.b64encode(b"s" * 32).decode(),
        )["id"]
    finally:
        agent_call_context.reset(token)
    with factory.begin() as session:
        notice = (
            NotificationService(session, settings)
            .publish(
                "docket.attention.created",
                new_public_ref("case"),
                key="postgres-claim-fixture",
            )
            .ref_id
        )
    dispatcher = NotificationDispatcher(factory, settings, poster)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(dispatcher.run_due_once)
        try:
            assert reached.wait(timeout=10)
            # The first claim committed before network I/O; a second worker skips it.
            assert dispatcher.run_due_once() is False
            with factory() as session:
                delivery = session.scalar(
                    select(MCPEventDelivery).where(
                        MCPEventDelivery.notification_ref == notice,
                    )
                )
                assert delivery.status == "running" and delivery.attempts == 1
                delivery_ref = delivery.ref_id
        finally:
            release.set()
        assert first.result(timeout=10)
    assert poster.deliveries == 1
    with factory() as session:
        attempt = session.scalar(
            select(MCPEventAttempt).where(
                MCPEventAttempt.delivery_ref == delivery_ref,
            )
        )
        assert attempt.status == "accepted"
    for statement, ref in (
        ("UPDATE plugin_notifications SET summary = 'rewrite' WHERE ref_id = :ref", notice),
        ("DELETE FROM plugin_notifications WHERE ref_id = :ref", notice),
        ("UPDATE mcp_event_subscriptions SET event_name = 'other' WHERE ref_id = :ref", sub),
        ("DELETE FROM mcp_event_subscriptions WHERE ref_id = :ref", sub),
        (
            "UPDATE mcp_event_deliveries SET notification_ref = 'other' WHERE ref_id = :ref",
            delivery_ref,
        ),
        (
            "UPDATE mcp_event_attempts SET status = 'running' WHERE delivery_ref = :ref",
            delivery_ref,
        ),
        ("DELETE FROM mcp_event_attempts WHERE delivery_ref = :ref", delivery_ref),
    ):
        try:
            with factory.begin() as session:
                session.execute(text(statement), {"ref": ref})
        except DBAPIError:
            pass
        else:
            raise AssertionError("PostgreSQL discarded notification identity or history")
    migration = ScriptDirectory.from_config(Config("alembic.ini")).get_revision("20261028b2c3")
    assert migration is not None
    try:
        with (
            factory.kw["bind"].begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            migration.module.downgrade()
    except RuntimeError as exc:
        assert "retained" in str(exc)
    else:
        raise AssertionError("Downgrade discarded notification history")
