from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import Settings, get_settings
from docket.models import (
    Account,
    Action,
    ActionRevision,
    Approval,
    AuditEvent,
    BackupRun,
    ConnectorCheckpoint,
    ExecutionAttempt,
    Operation,
    OutboxEvent,
    ScheduledNotification,
    SourceItem,
)
from docket.models.base import utc_now

SOAK_DURATION = timedelta(hours=72)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class SoakStatus:
    started_at: datetime | None
    completed_at: datetime | None
    elapsed_seconds: int
    required_seconds: int
    remaining_seconds: int
    checks: dict[str, int]
    incidents: dict[str, int]
    ready_to_complete: bool

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["started_at"] = self.started_at.isoformat() if self.started_at else None
        value["completed_at"] = (
            self.completed_at.isoformat() if self.completed_at else None
        )
        return value


class SoakService:
    """Measure the personal-production soak from durable state."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()

    @staticmethod
    def _latest_event(session: Session, event_type: str) -> AuditEvent | None:
        return session.scalar(
            select(AuditEvent)
            .where(AuditEvent.event_type == event_type)
            .order_by(AuditEvent.created_at.desc())
            .limit(1)
        )

    def start(self) -> SoakStatus:
        with self.session_factory.begin() as session:
            started = self._latest_event(session, "soak.started")
            completed = self._latest_event(session, "soak.completed")
            if started is None or (
                completed is not None
                and _aware(completed.created_at) >= _aware(started.created_at)
            ):
                session.add(
                    AuditEvent(
                        event_type="soak.started",
                        entity_type="deployment",
                        entity_id=None,
                        actor_type="operator",
                        actor_id=self.settings.operator_discord_user_id,
                        data={
                            "required_seconds": int(SOAK_DURATION.total_seconds()),
                            "gmail_ingestion_enabled": (
                                self.settings.gmail_ingestion_enabled
                            ),
                            "gmail_writes_enabled": self.settings.gmail_writes_enabled,
                            "external_writes_enabled": (
                                self.settings.external_writes_enabled
                            ),
                            "backup_enabled": self.settings.backup_enabled,
                            "retention_enabled": self.settings.retention_enabled,
                        },
                    )
                )
        return self.status()

    @staticmethod
    def _count(session: Session, statement: Select[tuple[int]]) -> int:
        return int(session.scalar(statement) or 0)

    def status(self) -> SoakStatus:
        now = utc_now()
        with self.session_factory() as session:
            started = self._latest_event(session, "soak.started")
            completed = self._latest_event(session, "soak.completed")
            if started is None:
                return SoakStatus(
                    started_at=None,
                    completed_at=None,
                    elapsed_seconds=0,
                    required_seconds=int(SOAK_DURATION.total_seconds()),
                    remaining_seconds=int(SOAK_DURATION.total_seconds()),
                    checks={"soak_not_started": 1},
                    incidents={},
                    ready_to_complete=False,
                )
            started_at = _aware(started.created_at)
            completed_at = (
                _aware(completed.created_at)
                if completed is not None
                and _aware(completed.created_at) >= started_at
                else None
            )
            elapsed = max(0, int((now - started_at).total_seconds()))
            required = int(SOAK_DURATION.total_seconds())
            source_stale_cutoff = now - timedelta(
                seconds=self.settings.gmail_stale_seconds
            )

            duplicate_operation_keys = session.execute(
                select(Operation.idempotency_key)
                .where(Operation.created_at >= started_at)
                .group_by(Operation.idempotency_key)
                .having(func.count(Operation.id) > 1)
            ).all()
            unapproved_operations = self._count(
                session,
                select(func.count(Operation.id)).where(
                    Operation.created_at >= started_at,
                    Operation.approval_id.is_(None),
                ),
            )
            invalid_approval_operations = self._count(
                session,
                select(func.count(Operation.id))
                .join(Approval, Approval.id == Operation.approval_id)
                .where(
                    Operation.created_at >= started_at,
                    Approval.status != "consumed",
                ),
            )
            unresolved_operations = self._count(
                session,
                select(func.count(Operation.id)).where(
                    Operation.created_at >= started_at,
                    Operation.status.in_(
                        ("pending", "running", "reconciliation_required")
                    ),
                ),
            )
            unresolved_outbox = self._count(
                session,
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.created_at >= started_at,
                    OutboxEvent.status.in_(("pending", "delivering")),
                ),
            )
            failed_outbox = self._count(
                session,
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.created_at >= started_at,
                    OutboxEvent.status == "failed",
                ),
            )
            expired_claims = self._count(
                session,
                select(func.count(SourceItem.id)).where(
                    SourceItem.status == "claimed",
                    SourceItem.claimed_until < now,
                ),
            )
            aged_source_work = self._count(
                session,
                select(func.count(SourceItem.id)).where(
                    SourceItem.status.in_(("staged", "claimed", "failed")),
                    SourceItem.created_at < source_stale_cutoff,
                ),
            )
            overdue_notifications = self._count(
                session,
                select(func.count(ScheduledNotification.id)).where(
                    ScheduledNotification.status == "pending",
                    ScheduledNotification.scheduled_for
                    < now - timedelta(minutes=5),
                ),
            )

            enabled_gmail_accounts = [
                account
                for account in session.scalars(
                    select(Account).where(
                        Account.provider == "google",
                        Account.enabled.is_(True),
                    )
                ).all()
                if "gmail" in account.capabilities
            ]
            stale_gmail_connectors = 0
            for account in enabled_gmail_accounts:
                checkpoint = session.scalar(
                    select(ConnectorCheckpoint).where(
                        ConnectorCheckpoint.account_id == account.id,
                        ConnectorCheckpoint.stream == "gmail:inbox",
                    )
                )
                if (
                    checkpoint is None
                    or checkpoint.last_success_at is None
                    or _aware(checkpoint.last_success_at) < source_stale_cutoff
                ):
                    stale_gmail_connectors += 1

            terminal_operations = session.execute(
                select(Operation.id, Action.id)
                .join(
                    ActionRevision,
                    ActionRevision.id == Operation.action_revision_id,
                )
                .join(Action, Action.id == ActionRevision.action_id)
                .where(
                    Operation.created_at >= started_at,
                    Operation.status.in_(("failed", "partial_failed")),
                )
            ).all()
            unreported_terminal_operations = 0
            for _operation_id, action_id in terminal_operations:
                reported = session.scalar(
                    select(OutboxEvent.id).where(
                        OutboxEvent.event_type == "discord.system_log.requested",
                        OutboxEvent.aggregate_type == "action",
                        OutboxEvent.aggregate_id == action_id,
                    )
                )
                if reported is None:
                    unreported_terminal_operations += 1

            latest_backup = session.scalar(
                select(BackupRun)
                .where(BackupRun.status == "succeeded")
                .order_by(BackupRun.local_date.desc())
                .limit(1)
            )
            backup_missing = int(
                latest_backup is None or latest_backup.manifest_name is None
            )
            backup_not_during_soak = int(
                latest_backup is None
                or latest_backup.completed_at is None
                or _aware(latest_backup.completed_at) < started_at
            )
            backup_stale = int(
                latest_backup is None
                or latest_backup.completed_at is None
                or _aware(latest_backup.completed_at)
                < now - timedelta(hours=36)
            )
            latest_backup_not_restore_verified = backup_missing
            if latest_backup is not None and latest_backup.manifest_name is not None:
                restore_events = session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.event_type == "backup.restore_succeeded")
                    .order_by(AuditEvent.created_at.desc())
                ).all()
                latest_backup_not_restore_verified = int(
                    not any(
                        event.data.get("manifest_name")
                        == latest_backup.manifest_name
                        for event in restore_events
                    )
                )
            retention_missing = int(
                session.scalar(
                    select(AuditEvent.id).where(
                        AuditEvent.event_type == "retention.cleanup_completed",
                        AuditEvent.created_at >= started_at,
                    )
                )
                is None
            )

            checks = {
                "gmail_ingestion_disabled": int(
                    not self.settings.gmail_ingestion_enabled
                ),
                "gmail_writes_disabled": int(not self.settings.gmail_writes_enabled),
                "external_writes_disabled": int(
                    not self.settings.external_writes_enabled
                ),
                "backup_disabled": int(not self.settings.backup_enabled),
                "retention_disabled": int(not self.settings.retention_enabled),
                "duplicate_operation_keys": len(duplicate_operation_keys),
                "unapproved_operations": unapproved_operations,
                "invalid_approval_operations": invalid_approval_operations,
                "unresolved_operations": unresolved_operations,
                "unresolved_outbox": unresolved_outbox,
                "failed_outbox": failed_outbox,
                "expired_source_claims": expired_claims,
                "aged_source_work": aged_source_work,
                "overdue_notifications": overdue_notifications,
                "gmail_account_missing": int(not enabled_gmail_accounts),
                "stale_gmail_connectors": stale_gmail_connectors,
                "unreported_terminal_operations": (
                    unreported_terminal_operations
                ),
                "backup_missing": backup_missing,
                "backup_not_during_soak": backup_not_during_soak,
                "backup_stale": backup_stale,
                "latest_backup_not_restore_verified": (
                    latest_backup_not_restore_verified
                ),
                "retention_run_missing": retention_missing,
            }
            incidents = {
                "failed_or_unknown_attempts": self._count(
                    session,
                    select(func.count(ExecutionAttempt.id)).where(
                        ExecutionAttempt.started_at >= started_at,
                        ExecutionAttempt.status.in_(("failed", "unknown")),
                    ),
                ),
                "terminal_operations": len(terminal_operations),
                "system_alerts": self._count(
                    session,
                    select(func.count(OutboxEvent.id)).where(
                        OutboxEvent.created_at >= started_at,
                        OutboxEvent.event_type
                        == "discord.system_alert.requested",
                    ),
                ),
            }
            ready = (
                elapsed >= required
                and completed_at is None
                and all(value == 0 for value in checks.values())
            )
            return SoakStatus(
                started_at=started_at,
                completed_at=completed_at,
                elapsed_seconds=elapsed,
                required_seconds=required,
                remaining_seconds=max(0, required - elapsed),
                checks=checks,
                incidents=incidents,
                ready_to_complete=ready,
            )

    def complete(self) -> SoakStatus:
        status = self.status()
        if not status.ready_to_complete:
            return status
        with self.session_factory.begin() as session:
            session.add(
                AuditEvent(
                    event_type="soak.completed",
                    entity_type="deployment",
                    entity_id=None,
                    actor_type="operator",
                    actor_id=self.settings.operator_discord_user_id,
                    data={
                        "started_at": (
                            status.started_at.isoformat()
                            if status.started_at is not None
                            else None
                        ),
                        "elapsed_seconds": status.elapsed_seconds,
                        "checks": status.checks,
                        "incidents": status.incidents,
                    },
                )
            )
        return self.status()
