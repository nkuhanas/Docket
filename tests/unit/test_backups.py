import json
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select, text

from docket.config import get_settings
from docket.models import AuditEvent, BackupRun, OutboxEvent
from docket.services.backups import (
    BackupArtifact,
    BackupError,
    BackupService,
    apply_backup_retention,
)


class FakeBackupExecutor:
    def __init__(self, *, error_code: str | None = None) -> None:
        self.error_code = error_code
        self.calls: list[tuple[date, datetime, str]] = []

    def execute(
        self,
        *,
        local_date: date,
        now: datetime,
        schema_revision: str,
    ) -> BackupArtifact:
        self.calls.append((local_date, now, schema_revision))
        if self.error_code is not None:
            raise BackupError(self.error_code)
        return BackupArtifact(
            artifact_name=f"docket-{local_date}.dump.age",
            manifest_name=f"docket-{local_date}.dump.age.manifest.json",
            ciphertext_sha256="a" * 64,
            ciphertext_bytes=123,
        )


def _version_table(session_factory) -> None:
    with session_factory.begin() as session:
        session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        session.execute(text("INSERT INTO alembic_version VALUES ('0014')"))


def _settings(tmp_path: Path):
    return get_settings().model_copy(
        update={
            "backup_enabled": True,
            "backup_directory": tmp_path,
            "backup_age_recipient": "age1testrecipient",
            "backup_hour": 3,
            "backup_retry_seconds": 60,
        }
    )


def test_daily_backup_is_durable_and_idempotent(session_factory, tmp_path) -> None:
    _version_table(session_factory)

    def clock() -> datetime:
        return datetime(2026, 7, 26, 12, tzinfo=UTC)

    executor = FakeBackupExecutor()
    service = BackupService(
        session_factory,
        _settings(tmp_path),
        executor=executor,
        clock=clock,
    )

    assert service.run_due_once()
    assert not service.run_due_once()
    assert service.run_due_once(force=True)
    assert executor.calls == [
        (date(2026, 7, 26), clock(), "0014"),
        (date(2026, 7, 26), clock(), "0014"),
    ]

    with session_factory() as session:
        run = session.scalar(select(BackupRun))
        audits = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "backup.succeeded")
        ).all()
        assert run is not None
        assert run.status == "succeeded"
        assert run.attempt_count == 2
        assert run.ciphertext_sha256 == "a" * 64
        assert run.lease_token is None
        assert len(audits) == 2


def test_backup_failure_retries_and_emits_one_alert(session_factory, tmp_path) -> None:
    _version_table(session_factory)

    def clock() -> datetime:
        return datetime(2026, 7, 26, 12, tzinfo=UTC)

    executor = FakeBackupExecutor(error_code="backup_dump_failed")
    service = BackupService(
        session_factory,
        _settings(tmp_path),
        executor=executor,
        clock=clock,
    )

    assert service.run_due_once()
    assert not service.run_due_once()
    assert service.run_due_once(force=True)

    with session_factory() as session:
        run = session.scalar(select(BackupRun))
        alerts = session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "discord.system_alert.requested"
            )
        ).all()
        failures = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "backup.failed")
        ).all()
        assert run is not None
        assert run.status == "failed"
        assert run.attempt_count == 2
        assert run.error_code == "backup_dump_failed"
        assert len(alerts) == 1
        assert len(failures) == 2


def _write_artifact(directory: Path, local_date: date) -> Path:
    artifact = directory / f"docket-{local_date}-120000.dump.age"
    artifact.write_bytes(b"encrypted")
    artifact.with_suffix(f"{artifact.suffix}.sha256").write_text(
        f"{'a' * 64}  {artifact.name}\n",
        encoding="utf-8",
    )
    manifest = artifact.with_name(f"{artifact.name}.manifest.json")
    manifest.write_text(
        json.dumps(
            {
                "artifact_name": artifact.name,
                "created_at": f"{local_date.isoformat()}T12:00:00+00:00",
                "local_date": local_date.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    return manifest


def test_retention_keeps_daily_and_weekly_union(tmp_path) -> None:
    manifests = [
        _write_artifact(tmp_path, date(2026, 7, day))
        for day in range(1, 15)
    ]

    removed = apply_backup_retention(
        tmp_path,
        daily_retention=3,
        weekly_retention=2,
    )

    retained_dates = {
        date.fromisoformat(json.loads(path.read_text())["local_date"])
        for path in manifests
        if path.exists()
    }
    assert {date(2026, 7, 12), date(2026, 7, 13), date(2026, 7, 14)} <= retained_dates
    assert len(retained_dates) <= 5
    assert removed
