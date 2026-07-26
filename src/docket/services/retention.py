from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, exists, select, update
from sqlalchemy.orm import Session, sessionmaker

from docket.config import Settings, get_settings
from docket.models import (
    Action,
    ActionRevision,
    AuditEvent,
    ExecutionAttempt,
    Operation,
    OperationItem,
    QueueItem,
    QueueItemSource,
    Record,
    ScheduledNotification,
    SourceItem,
)
from docket.models.base import utc_now


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _affected(result: object) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


@dataclass(frozen=True, slots=True)
class RetentionRunResult:
    ran: bool
    counts: dict[str, int]


class RetentionService:
    """Apply bounded metadata retention without touching canonical records."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()

    def run_due_once(self, *, force: bool = False) -> RetentionRunResult:
        if not self.settings.retention_enabled:
            return RetentionRunResult(ran=False, counts={})
        now = utc_now()
        local_date = now.astimezone(ZoneInfo(self.settings.timezone)).date()
        with self.session_factory.begin() as session:
            if not force:
                latest = session.scalar(
                    select(AuditEvent.created_at)
                    .where(AuditEvent.event_type == "retention.cleanup_completed")
                    .order_by(AuditEvent.created_at.desc())
                    .limit(1)
                )
                if (
                    latest is not None
                    and _aware(latest)
                    .astimezone(ZoneInfo(self.settings.timezone))
                    .date()
                    == local_date
                ):
                    return RetentionRunResult(ran=False, counts={})

            ignored_cutoff = now - timedelta(days=30)
            ordinary_cutoff = now - timedelta(days=365)
            diagnostic_cutoff = now - timedelta(days=90)
            linked_source = exists(
                select(QueueItemSource.source_item_id).where(
                    QueueItemSource.source_item_id == SourceItem.id
                )
            )
            primary_source = exists(
                select(QueueItem.id).where(
                    QueueItem.primary_source_item_id == SourceItem.id
                )
            )
            ignored_sources = _affected(session.execute(
                delete(SourceItem).where(
                    SourceItem.status == "ignored",
                    SourceItem.created_at < ignored_cutoff,
                    ~linked_source,
                    ~primary_source,
                )
            ))
            ordinary_sources = _affected(session.execute(
                delete(SourceItem).where(
                    SourceItem.status != "ignored",
                    SourceItem.created_at < ordinary_cutoff,
                    ~linked_source,
                    ~primary_source,
                )
            ))
            scrubbed_attempts = _affected(session.execute(
                update(ExecutionAttempt)
                .where(
                    ExecutionAttempt.started_at < diagnostic_cutoff,
                    ExecutionAttempt.status.in_(("failed", "unknown")),
                )
                .values(
                    error_message=None,
                    response_summary=None,
                )
            ))
            deleted_attempts = _affected(session.execute(
                delete(ExecutionAttempt).where(
                    ExecutionAttempt.started_at < ordinary_cutoff
                )
            ))
            scrubbed_operation_item_results = 0
            operation_items = session.execute(
                select(OperationItem, Action)
                .join(Operation, Operation.id == OperationItem.operation_id)
                .join(
                    ActionRevision,
                    ActionRevision.id == Operation.action_revision_id,
                )
                .join(Action, Action.id == ActionRevision.action_id)
                .where(
                    OperationItem.result.is_not(None),
                    OperationItem.updated_at < ordinary_cutoff,
                    Operation.status.in_(
                        ("succeeded", "partial_failed", "failed")
                    ),
                    Action.status.in_(
                        ("succeeded", "partial_failed", "rejected", "failed")
                    ),
                )
            ).all()
            for item, action in operation_items:
                if action.record_id is not None:
                    record = session.get(Record, action.record_id)
                    if record is not None and (
                        record.status == "active"
                        or _aware(record.updated_at) >= ordinary_cutoff
                    ):
                        continue
                item.result = None
                scrubbed_operation_item_results += 1
            notifications = _affected(session.execute(
                delete(ScheduledNotification).where(
                    ScheduledNotification.status.in_(("delivered", "cancelled")),
                    ScheduledNotification.updated_at < diagnostic_cutoff,
                )
            ))
            audits = _affected(session.execute(
                delete(AuditEvent).where(AuditEvent.created_at < ordinary_cutoff)
            ))
            counts = {
                "ignored_sources": ignored_sources,
                "ordinary_sources": ordinary_sources,
                "scrubbed_attempts": scrubbed_attempts,
                "deleted_attempts": deleted_attempts,
                "scrubbed_operation_item_results": (
                    scrubbed_operation_item_results
                ),
                "notifications": notifications,
                "audits": audits,
            }
            session.add(
                AuditEvent(
                    event_type="retention.cleanup_completed",
                    entity_type="retention",
                    entity_id=None,
                    actor_type="system",
                    actor_id=None,
                    data={
                        "local_date": local_date.isoformat(),
                        "counts": counts,
                    },
                )
            )
            return RetentionRunResult(ran=True, counts=counts)
