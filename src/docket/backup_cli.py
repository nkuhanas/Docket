from __future__ import annotations

import argparse
import hashlib
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from docket.config import get_settings
from docket.database import configure_database, get_session_factory
from docket.domain.enums import OutboxStatus
from docket.models import AuditEvent, BackupRun, OutboxEvent
from docket.services.backups import BackupService


def _latest_run() -> BackupRun | None:
    with get_session_factory()() as session:
        return session.scalar(
            select(BackupRun).order_by(BackupRun.local_date.desc()).limit(1)
        )


def _backup() -> int:
    settings = get_settings()
    if not settings.backup_enabled:
        print("Encrypted backups are disabled.", file=sys.stderr)
        return 2
    processed = BackupService(get_session_factory(), settings).run_due_once(force=True)
    run = _latest_run()
    if run is None:
        print("No backup run was created.", file=sys.stderr)
        return 1
    if run.status != "succeeded":
        print(f"Backup failed with {run.error_code or 'backup_failed'}.", file=sys.stderr)
        return 1
    state = "created" if processed else "already complete"
    print(
        f"Encrypted backup {state}: {run.artifact_name} "
        f"({run.ciphertext_sha256}, {run.ciphertext_bytes} bytes)"
    )
    return 0


def _record_restore(
    *,
    manifest_name: str,
    status: str,
    schema_revision: str | None,
    error_code: str | None,
) -> int:
    if Path(manifest_name).name != manifest_name:
        print("Manifest name must be a basename.", file=sys.stderr)
        return 2
    now = datetime.now(UTC)
    with get_session_factory().begin() as session:
        session.add(
            AuditEvent(
                event_type=f"backup.restore_{status}",
                entity_type="backup_manifest",
                entity_id=None,
                actor_type="system",
                actor_id=None,
                data={
                    "manifest_name": manifest_name,
                    "schema_revision": schema_revision,
                    "error_code": error_code,
                },
            )
        )
        if status == "failed":
            digest = hashlib.sha256(
                f"{manifest_name}:{error_code or 'restore_failed'}".encode()
            ).hexdigest()
            key = f"discord_system_alert:restore_failed:{digest}"
            existing = session.scalar(
                select(OutboxEvent.id).where(OutboxEvent.deduplication_key == key)
            )
            if existing is None:
                session.add(
                    OutboxEvent(
                        id=uuid.uuid5(uuid.NAMESPACE_URL, key),
                        event_type="discord.system_alert.requested",
                        aggregate_type="backup_restore",
                        aggregate_id=uuid.uuid5(uuid.NAMESPACE_URL, manifest_name),
                        deduplication_key=key,
                        payload={
                            "title": "Docket backup restore verification failed",
                            "summary": (
                                "An encrypted backup could not be restored into the "
                                "disposable verification database."
                            ),
                            "error_code": error_code or "restore_failed",
                            "occurred_at": now.isoformat(),
                        },
                        status=OutboxStatus.PENDING.value,
                    )
                )
    print(f"Recorded restore verification: {status}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docket-backup")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("backup")
    restore = subparsers.add_parser("record-restore")
    restore.add_argument("--manifest-name", required=True)
    restore.add_argument("--status", choices=("succeeded", "failed"), required=True)
    restore.add_argument("--schema-revision")
    restore.add_argument("--error-code")
    arguments = parser.parse_args(argv)

    settings = get_settings()
    configure_database(settings.database_url)
    if arguments.command == "backup":
        return _backup()
    return _record_restore(
        manifest_name=arguments.manifest_name,
        status=arguments.status,
        schema_revision=arguments.schema_revision,
        error_code=arguments.error_code,
    )


if __name__ == "__main__":
    raise SystemExit(main())
