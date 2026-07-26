import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from docket.config import get_settings
from docket.models import (
    Account,
    AuditEvent,
    ConnectorCheckpoint,
    OutboxEvent,
    SourceItem,
)
from docket.providers.google.fake_gmail import FakeGmailProvider
from docket.services.gmail_ingestion import GmailIngestionService


def _settings():
    return get_settings().model_copy(
        update={
            "gmail_ingestion_enabled": True,
            "gmail_scan_interval_seconds": 1800,
            "gmail_scan_lease_seconds": 300,
            "gmail_recovery_overlap_days": 7,
            "gmail_scan_max_pages": 100,
            "gmail_scan_max_messages": 5000,
        }
    )


def _account(session_factory) -> Account:
    with session_factory.begin() as session:
        account = Account(
            provider="google",
            external_account_id="gmail-test",
            capabilities=["gmail"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        session.expunge(account)
        return account


@pytest.mark.integration
def test_paginated_scan_advances_checkpoint_without_duplicate_sources(
    session_factory,
) -> None:
    account = _account(session_factory)
    provider = FakeGmailProvider(page_size=2)
    received = datetime.now(UTC) - timedelta(hours=1)
    for number in range(3):
        provider.add_message(
            message_id=f"message-{number}",
            thread_id="thread-1",
            sender="Registrar <registrar@example.edu>",
            subject=f"Notice {number}",
            body_text="This body must never be staged.",
            received_at=received + timedelta(minutes=number),
        )
    service = GmailIngestionService(session_factory, provider, _settings())

    result = service.run_due_once(force=True)

    assert result.completed
    assert result.pages == 2
    assert result.observed == 3
    assert result.staged == 3
    assert result.recovery
    with session_factory() as session:
        checkpoint = session.scalar(select(ConnectorCheckpoint))
        sources = session.scalars(
            select(SourceItem).order_by(SourceItem.external_object_id)
        ).all()
        assert checkpoint is not None
        assert checkpoint.account_id == account.id
        assert checkpoint.cursor == {"mode": "history", "history_id": "3"}
        assert checkpoint.last_success_at is not None
        assert checkpoint.lease_token is None
        assert [source.external_object_id for source in sources] == [
            "message-0",
            "message-1",
            "message-2",
        ]
        assert all("body" not in source.minimal_headers for source in sources)

    replay = service.run_due_once(force=True)
    assert replay.completed
    assert replay.staged == 0
    with session_factory() as session:
        assert len(session.scalars(select(SourceItem)).all()) == 3


@pytest.mark.integration
def test_invalid_history_cursor_uses_overlap_and_deduplicates(
    session_factory,
) -> None:
    _account(session_factory)
    provider = FakeGmailProvider(page_size=10)
    provider.add_message(message_id="first", received_at=datetime.now(UTC))
    service = GmailIngestionService(session_factory, provider, _settings())
    assert service.run_due_once(force=True).staged == 1

    provider.add_message(message_id="second", received_at=datetime.now(UTC))
    provider.invalidate_next_history_cursor = True
    result = service.run_due_once(force=True)

    assert result.completed
    assert result.recovery
    assert result.staged == 1
    with session_factory() as session:
        assert len(session.scalars(select(SourceItem)).all()) == 2
        recovery = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == "gmail.cursor_recovery_started"
            )
        ).all()
        assert len(recovery) == 1


@pytest.mark.integration
def test_active_scan_lease_prevents_concurrent_claim(session_factory) -> None:
    account = _account(session_factory)
    settings = _settings()
    with session_factory.begin() as session:
        session.add(
            ConnectorCheckpoint(
                account_id=account.id,
                stream="gmail:inbox",
                cursor={},
                lease_token=uuid.uuid4(),
                leased_until=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
    provider = FakeGmailProvider()

    result = GmailIngestionService(
        session_factory,
        provider,
        settings,
    ).run_due_once(force=True)

    assert not result.claimed
    assert provider.scan_calls == 0


@pytest.mark.integration
def test_scan_selects_gmail_capable_account_after_other_google_account(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        session.add(
            Account(
                provider="google",
                external_account_id="calendar-only",
                capabilities=["calendar"],
                enabled=True,
            )
        )
    gmail_account = _account(session_factory)
    provider = FakeGmailProvider()
    provider.add_message(message_id="gmail-capable-account")

    result = GmailIngestionService(
        session_factory,
        provider,
        _settings(),
    ).run_due_once(force=True)

    assert result.completed
    with session_factory() as session:
        checkpoint = session.scalar(select(ConnectorCheckpoint))
        assert checkpoint is not None
        assert checkpoint.account_id == gmail_account.id


@pytest.mark.integration
def test_stale_gmail_checkpoint_emits_one_redacted_alert_per_episode(
    session_factory,
) -> None:
    account = _account(session_factory)
    settings = _settings().model_copy(update={"gmail_stale_seconds": 300})
    stale_success = datetime.now(UTC) - timedelta(hours=1)
    with session_factory.begin() as session:
        session.add(
            ConnectorCheckpoint(
                account_id=account.id,
                stream="gmail:inbox",
                cursor={"mode": "history", "history_id": "10"},
                last_success_at=stale_success,
                last_error_code="gmail_transient",
            )
        )
    service = GmailIngestionService(
        session_factory,
        FakeGmailProvider(),
        settings,
    )

    assert service.evaluate_staleness() == 1
    assert service.evaluate_staleness() == 1

    with session_factory() as session:
        alerts = session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "discord.system_alert.requested"
            )
        ).all()
        assert len(alerts) == 1
        assert alerts[0].payload["error_code"] == "gmail_transient"
        assert "message" not in str(alerts[0].payload).casefold()
