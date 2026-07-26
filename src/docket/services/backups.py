from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from docket.config import Settings, get_settings
from docket.domain.enums import OutboxStatus
from docket.models import AuditEvent, BackupRun, OutboxEvent
from docket.models.base import utc_now


class BackupError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class BackupArtifact:
    artifact_name: str
    manifest_name: str
    ciphertext_sha256: str
    ciphertext_bytes: int


class BackupExecutor(Protocol):
    def execute(
        self,
        *,
        local_date: date,
        now: datetime,
        schema_revision: str,
    ) -> BackupArtifact: ...


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _manifest_rows(directory: Path) -> list[tuple[date, datetime, Path, dict[str, object]]]:
    rows: list[tuple[date, datetime, Path, dict[str, object]]] = []
    for path in directory.glob("docket-*.dump.age.manifest.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            local_date = date.fromisoformat(str(payload["local_date"]))
            created_at = datetime.fromisoformat(str(payload["created_at"]))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            artifact = directory / str(payload["artifact_name"])
            if artifact.is_file():
                rows.append((local_date, created_at.astimezone(UTC), path, payload))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
    return sorted(rows, key=lambda row: (row[0], row[1]), reverse=True)


def apply_backup_retention(
    directory: Path,
    *,
    daily_retention: int,
    weekly_retention: int,
) -> list[str]:
    rows = _manifest_rows(directory)
    keep: set[Path] = set()
    seen_days: set[date] = set()
    for local_date, _created_at, manifest, _payload in rows:
        if local_date not in seen_days and len(seen_days) < daily_retention:
            seen_days.add(local_date)
            keep.add(manifest)

    seen_weeks: set[tuple[int, int]] = set()
    for local_date, _created_at, manifest, _payload in rows:
        iso = local_date.isocalendar()
        week = (iso.year, iso.week)
        if week not in seen_weeks and len(seen_weeks) < weekly_retention:
            seen_weeks.add(week)
            keep.add(manifest)

    removed: list[str] = []
    for _local_date, _created_at, manifest, payload in rows:
        if manifest in keep:
            continue
        artifact = directory / str(payload["artifact_name"])
        checksum = artifact.with_suffix(f"{artifact.suffix}.sha256")
        for path in (artifact, checksum, manifest):
            if path.exists():
                path.unlink()
                removed.append(path.name)
    return removed


class EncryptedPostgresBackupExecutor:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def execute(
        self,
        *,
        local_date: date,
        now: datetime,
        schema_revision: str,
    ) -> BackupArtifact:
        recipient = self.settings.backup_age_recipient
        if recipient is None:
            raise BackupError("backup_recipient_missing")
        directory = self.settings.backup_directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)

        timestamp = _aware(now).strftime("%Y%m%dT%H%M%SZ")
        artifact = directory / f"docket-{local_date.isoformat()}-{timestamp}.dump.age"
        manifest = artifact.with_name(f"{artifact.name}.manifest.json")
        checksum = artifact.with_suffix(f"{artifact.suffix}.sha256")
        temporary = artifact.with_name(f".{artifact.name}.{uuid.uuid4().hex}.tmp")
        completed = False

        url = make_url(self.settings.database_url)
        if not url.drivername.startswith("postgresql") or not url.database:
            raise BackupError("backup_database_unsupported")
        environment = os.environ.copy()
        if url.password is not None:
            environment["PGPASSWORD"] = url.password
        dump_command = [
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--host",
            url.host or "postgres",
            "--port",
            str(url.port or 5432),
            "--username",
            url.username or "docket",
            "--dbname",
            url.database,
        ]
        age_command = ["age", "--encrypt", "--recipient", recipient]
        try:
            with temporary.open("xb") as output:
                os.chmod(temporary, 0o600)
                dump = subprocess.Popen(
                    dump_command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=environment,
                )
                assert dump.stdout is not None
                encryption = subprocess.Popen(
                    age_command,
                    stdin=dump.stdout,
                    stdout=output,
                    stderr=subprocess.PIPE,
                )
                dump.stdout.close()
                _age_output, _age_error = encryption.communicate()
                assert dump.stderr is not None
                _dump_error = dump.stderr.read()
                dump_result = dump.wait()
                output.flush()
                os.fsync(output.fileno())
            if dump_result != 0:
                raise BackupError("backup_dump_failed")
            if encryption.returncode != 0:
                raise BackupError("backup_encryption_failed")
            if temporary.stat().st_size <= 0:
                raise BackupError("backup_artifact_empty")
            os.replace(temporary, artifact)
            os.chmod(artifact, 0o600)
            digest = hashlib.sha256()
            with artifact.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            ciphertext_sha256 = digest.hexdigest()
            ciphertext_bytes = artifact.stat().st_size
            checksum.write_text(
                f"{ciphertext_sha256}  {artifact.name}\n",
                encoding="utf-8",
            )
            os.chmod(checksum, 0o600)
            _atomic_json(
                manifest,
                {
                    "artifact_name": artifact.name,
                    "build_revision": os.getenv("DOCKET_BUILD_REVISION", "unknown"),
                    "ciphertext_bytes": ciphertext_bytes,
                    "ciphertext_sha256": ciphertext_sha256,
                    "created_at": _aware(now).isoformat(),
                    "database": url.database,
                    "format": "postgresql-custom+age",
                    "local_date": local_date.isoformat(),
                    "manifest_version": 1,
                    "schema_revision": schema_revision,
                },
            )
            apply_backup_retention(
                directory,
                daily_retention=self.settings.backup_daily_retention,
                weekly_retention=self.settings.backup_weekly_retention,
            )
            completed = True
            return BackupArtifact(
                artifact_name=artifact.name,
                manifest_name=manifest.name,
                ciphertext_sha256=ciphertext_sha256,
                ciphertext_bytes=ciphertext_bytes,
            )
        except BackupError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise BackupError("backup_process_failed") from exc
        finally:
            temporary.unlink(missing_ok=True)
            if not completed:
                manifest.unlink(missing_ok=True)
                checksum.unlink(missing_ok=True)
                artifact.unlink(missing_ok=True)


class BackupService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings | None = None,
        *,
        executor: BackupExecutor | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self.executor = executor or EncryptedPostgresBackupExecutor(self.settings)
        self.clock = clock

    def _claim(self, *, force: bool) -> tuple[uuid.UUID, uuid.UUID, date, str] | None:
        now = _aware(self.clock())
        local_now = now.astimezone(ZoneInfo(self.settings.timezone))
        if not force and local_now.hour < self.settings.backup_hour:
            return None
        local_date = local_now.date()
        lease_token = uuid.uuid4()
        with self.session_factory.begin() as session:
            run = session.scalar(
                select(BackupRun)
                .where(BackupRun.local_date == local_date)
                .with_for_update()
            )
            if run is not None:
                if run.status == "succeeded":
                    return None
                if (
                    run.status == "running"
                    and run.leased_until is not None
                    and _aware(run.leased_until) > now
                ):
                    return None
                if (
                    not force
                    and run.next_attempt_at is not None
                    and _aware(run.next_attempt_at) > now
                ):
                    return None
            else:
                run = BackupRun(
                    local_date=local_date,
                    status="running",
                    attempt_count=0,
                    started_at=now,
                )
                session.add(run)
                session.flush()
            run.status = "running"
            run.attempt_count += 1
            run.started_at = now
            run.completed_at = None
            run.error_code = None
            run.next_attempt_at = None
            run.lease_token = lease_token
            run.leased_until = now + timedelta(seconds=self.settings.backup_lease_seconds)
            schema_revision = str(
                session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            )
            return run.id, lease_token, local_date, schema_revision

    def _mark_success(
        self,
        run_id: uuid.UUID,
        lease_token: uuid.UUID,
        artifact: BackupArtifact,
    ) -> None:
        now = _aware(self.clock())
        with self.session_factory.begin() as session:
            run = session.get(BackupRun, run_id)
            if run is None or run.lease_token != lease_token:
                return
            run.status = "succeeded"
            run.completed_at = now
            run.artifact_name = artifact.artifact_name
            run.manifest_name = artifact.manifest_name
            run.ciphertext_sha256 = artifact.ciphertext_sha256
            run.ciphertext_bytes = artifact.ciphertext_bytes
            run.error_code = None
            run.next_attempt_at = None
            run.lease_token = None
            run.leased_until = None
            session.add(
                AuditEvent(
                    event_type="backup.succeeded",
                    entity_type="backup_run",
                    entity_id=run.id,
                    actor_type="system",
                    actor_id=None,
                    data={
                        "local_date": run.local_date.isoformat(),
                        "artifact_name": artifact.artifact_name,
                        "manifest_name": artifact.manifest_name,
                        "ciphertext_sha256": artifact.ciphertext_sha256,
                        "ciphertext_bytes": artifact.ciphertext_bytes,
                    },
                )
            )

    def _mark_failed(
        self,
        run_id: uuid.UUID,
        lease_token: uuid.UUID,
        error_code: str,
    ) -> None:
        now = _aware(self.clock())
        with self.session_factory.begin() as session:
            run = session.get(BackupRun, run_id)
            if run is None or run.lease_token != lease_token:
                return
            run.status = "failed"
            run.completed_at = now
            run.error_code = error_code[:128]
            run.next_attempt_at = now + timedelta(seconds=self.settings.backup_retry_seconds)
            run.lease_token = None
            run.leased_until = None
            session.add(
                AuditEvent(
                    event_type="backup.failed",
                    entity_type="backup_run",
                    entity_id=run.id,
                    actor_type="system",
                    actor_id=None,
                    data={
                        "local_date": run.local_date.isoformat(),
                        "error_code": run.error_code,
                    },
                )
            )
            key = f"discord_system_alert:backup_failed:{run.local_date.isoformat()}"
            if (
                session.scalar(
                    select(OutboxEvent).where(OutboxEvent.deduplication_key == key)
                )
                is None
            ):
                session.add(
                    OutboxEvent(
                        id=uuid.uuid5(uuid.NAMESPACE_URL, key),
                        event_type="discord.system_alert.requested",
                        aggregate_type="backup_run",
                        aggregate_id=run.id,
                        deduplication_key=key,
                        payload={
                            "title": "Docket encrypted backup failed",
                            "summary": (
                                "The daily encrypted database backup did not complete. "
                                "Docket will retry without replacing an earlier artifact."
                            ),
                            "error_code": run.error_code,
                            "occurred_at": now.isoformat(),
                        },
                        status=OutboxStatus.PENDING.value,
                    )
                )

    def run_due_once(self, *, force: bool = False) -> bool:
        if not self.settings.backup_enabled:
            return False
        claim = self._claim(force=force)
        if claim is None:
            return False
        run_id, lease_token, local_date, schema_revision = claim
        try:
            artifact = self.executor.execute(
                local_date=local_date,
                now=_aware(self.clock()),
                schema_revision=schema_revision,
            )
        except BackupError as exc:
            self._mark_failed(run_id, lease_token, exc.code)
        except Exception:
            self._mark_failed(run_id, lease_token, "backup_unexpected")
            raise
        else:
            self._mark_success(run_id, lease_token, artifact)
        return True
