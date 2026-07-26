from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from docket.config import get_settings
from docket.models import (
    Account,
    AuditEvent,
    BackupRun,
    ConnectorCheckpoint,
)
from docket.services.soak import SoakService


@pytest.mark.integration
def test_soak_completion_requires_duration_and_durable_operational_gates(
    session_factory,
) -> None:
    settings = get_settings().model_copy(
        update={
            "gmail_ingestion_enabled": True,
            "gmail_writes_enabled": True,
            "external_writes_enabled": True,
            "backup_enabled": True,
            "backup_age_recipient": "age1test",
            "retention_enabled": True,
        }
    )
    service = SoakService(session_factory, settings)
    initial = service.start()
    assert initial.started_at is not None
    assert not initial.ready_to_complete

    now = datetime.now(UTC)
    with session_factory.begin() as session:
        started = session.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "soak.started")
        )
        assert started is not None
        started.created_at = now - timedelta(hours=73)
        account = Account(
            provider="google",
            external_account_id="soak-gmail",
            capabilities=["gmail"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        session.add(
            ConnectorCheckpoint(
                account_id=account.id,
                stream="gmail:inbox",
                cursor={"mode": "history", "history_id": "1"},
                observed_through=now,
                last_attempt_at=now,
                last_success_at=now,
            )
        )
        session.add(
            BackupRun(
                local_date=now.date(),
                status="succeeded",
                started_at=now,
                completed_at=now,
                artifact_name="docket-soak.dump.age",
                manifest_name="docket-soak.manifest.json",
                ciphertext_sha256="a" * 64,
                ciphertext_bytes=1024,
            )
        )
        session.add_all(
            [
                AuditEvent(
                    event_type="backup.restore_succeeded",
                    entity_type="backup_manifest",
                    entity_id=None,
                    actor_type="system",
                    actor_id=None,
                    data={"manifest_name": "docket-soak.manifest.json"},
                ),
                AuditEvent(
                    event_type="retention.cleanup_completed",
                    entity_type="retention",
                    entity_id=None,
                    actor_type="system",
                    actor_id=None,
                    data={"counts": {}},
                ),
            ]
        )

    status = service.status()
    assert status.elapsed_seconds >= 72 * 60 * 60
    assert status.ready_to_complete
    assert all(value == 0 for value in status.checks.values())

    completed = service.complete()
    assert completed.completed_at is not None
    assert not completed.ready_to_complete
    with session_factory() as session:
        completion = session.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "soak.completed")
        )
        assert completion is not None
        assert completion.data["checks"] == status.checks
