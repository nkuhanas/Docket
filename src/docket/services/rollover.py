from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from docket.config import Settings
from docket.domain.canonical import sha256_json
from docket.domain.enums import (
    ActionStatus,
    ApprovalStatus,
    CommandStatus,
    OutboxStatus,
    QueueItemStatus,
)
from docket.models import (
    Account,
    Action,
    ActionRevision,
    Approval,
    AuditEvent,
    CommandRequest,
    DiscordDailyThread,
    DiscordProjection,
    OutboxEvent,
    QueueItem,
    Record,
    ScheduledNotification,
)
from docket.models.base import utc_now
from docket.security import issue_short_code, short_code_sha256
from docket.services.queue import QueueService, ensure_local_actions

_UNRESOLVED = {
    QueueItemStatus.PENDING.value,
    QueueItemStatus.AWAITING_APPROVAL.value,
    QueueItemStatus.EXECUTING.value,
    QueueItemStatus.FAILED.value,
    QueueItemStatus.RECONCILIATION_REQUIRED.value,
}


class RolloverService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings

    def _local_now(self, now: datetime) -> datetime:
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return now.astimezone(ZoneInfo(self.settings.timezone))

    @staticmethod
    def _thread_name(local_date: date) -> str:
        return f"{local_date.isoformat()} — {local_date.strftime('%A')}"

    def _ensure_thread_row(self, session: Session, local_date: date) -> DiscordDailyThread:
        daily_thread = session.scalar(
            select(DiscordDailyThread).where(
                DiscordDailyThread.guild_id == self.settings.discord_guild_id,
                DiscordDailyThread.channel_id == self.settings.queue_channel_id,
                DiscordDailyThread.local_date == local_date,
            )
        )
        if daily_thread is None:
            daily_thread = DiscordDailyThread(
                guild_id=self.settings.discord_guild_id,
                channel_id=self.settings.queue_channel_id,
                local_date=local_date,
                thread_name=self._thread_name(local_date),
                status="pending",
            )
            session.add(daily_thread)
            session.flush()
        key = f"discord_thread_ensure:{daily_thread.id}:{daily_thread.lifecycle_version}"
        if session.scalar(select(OutboxEvent).where(OutboxEvent.deduplication_key == key)) is None:
            session.add(
                OutboxEvent(
                    event_type="discord.thread.ensure_requested",
                    aggregate_type="discord_daily_thread",
                    aggregate_id=daily_thread.id,
                    deduplication_key=key,
                    payload={"daily_thread_id": str(daily_thread.id)},
                    status=OutboxStatus.PENDING.value,
                )
            )
        return daily_thread

    def _projection_for(
        self,
        session: Session,
        queue_item: QueueItem,
        daily_thread: DiscordDailyThread,
        *,
        reason: str,
    ) -> tuple[DiscordProjection, bool]:
        projection = session.scalar(
            select(DiscordProjection).where(
                DiscordProjection.queue_item_id == queue_item.id,
                DiscordProjection.daily_thread_id == daily_thread.id,
            )
        )
        created = projection is None
        if projection is None:
            projection = DiscordProjection(
                queue_item_id=queue_item.id,
                daily_thread_id=daily_thread.id,
                render_sha256="0" * 64,
                component_sha256="0" * 64,
                status="pending",
            )
            session.add(projection)
            session.flush()
        key = f"discord_projection:{queue_item.id}:date:{daily_thread.local_date.isoformat()}"
        if session.scalar(select(OutboxEvent).where(OutboxEvent.deduplication_key == key)) is None:
            session.add(
                OutboxEvent(
                    event_type="discord.projection.requested",
                    aggregate_type="queue_item",
                    aggregate_id=queue_item.id,
                    deduplication_key=key,
                    payload={
                        "queue_item_id": str(queue_item.id),
                        "projection_id": str(projection.id),
                        "target_local_date": daily_thread.local_date.isoformat(),
                        "reason": reason,
                    },
                    status=OutboxStatus.PENDING.value,
                )
            )
        return projection, created

    def _refresh_previous_projection(
        self,
        session: Session,
        queue_item: QueueItem,
        current: DiscordProjection,
        current_date: date,
    ) -> None:
        pending_approval = session.scalar(
            select(Approval.id)
            .join(ActionRevision, ActionRevision.id == Approval.action_revision_id)
            .join(Action, Action.id == ActionRevision.action_id)
            .where(
                Action.queue_item_id == queue_item.id,
                ActionRevision.revision == Action.current_revision,
                Approval.status == ApprovalStatus.PENDING.value,
            )
            .limit(1)
        )
        if pending_approval is not None:
            # The projection acknowledgement moves the active approval binding and
            # transactionally schedules the historical-card refresh.
            return
        previous = session.execute(
            select(DiscordProjection, DiscordDailyThread)
            .join(
                DiscordDailyThread,
                DiscordDailyThread.id == DiscordProjection.daily_thread_id,
            )
            .where(
                DiscordProjection.queue_item_id == queue_item.id,
                DiscordDailyThread.local_date < current_date,
            )
            .order_by(DiscordDailyThread.local_date.desc())
            .limit(1)
        ).first()
        if previous is None:
            return
        projection, daily_thread = previous
        key = f"discord_projection:{queue_item.id}:carryover:{daily_thread.local_date}:{current.id}"
        if session.scalar(select(OutboxEvent).where(OutboxEvent.deduplication_key == key)) is None:
            session.add(
                OutboxEvent(
                    event_type="discord.projection.refresh_requested",
                    aggregate_type="queue_item",
                    aggregate_id=queue_item.id,
                    deduplication_key=key,
                    payload={
                        "queue_item_id": str(queue_item.id),
                        "projection_id": str(projection.id),
                        "target_local_date": daily_thread.local_date.isoformat(),
                        "reason": "carried_forward",
                    },
                    status=OutboxStatus.PENDING.value,
                )
            )

    def _expire_due_approvals(self, session: Session, now: datetime) -> int:
        approvals = session.scalars(
            select(Approval).where(
                Approval.status == ApprovalStatus.PENDING.value,
                Approval.expires_at <= now,
            )
        ).all()
        expired = 0
        for approval in approvals:
            revision = session.get(ActionRevision, approval.action_revision_id)
            action = session.get(Action, revision.action_id) if revision is not None else None
            queue_item = (
                session.get(QueueItem, action.queue_item_id)
                if action is not None and action.queue_item_id is not None
                else None
            )
            if revision is None or action is None or queue_item is None:
                continue
            approval.status = ApprovalStatus.EXPIRED.value
            approval.control_projection_id = None
            action.status = ActionStatus.EXPIRED.value
            queue_item.status = QueueItemStatus.PENDING.value
            queue_item.version += 1
            QueueService(session, self.settings).enqueue_refresh(queue_item, "approval_expired")
            session.add(
                AuditEvent(
                    event_type="approval.expired",
                    entity_type="approval",
                    entity_id=approval.id,
                    actor_type="docket",
                    data={"action_revision_id": str(revision.id), "version": queue_item.version},
                )
            )
            expired += 1
        return expired

    def expire_due_approvals(self, now: datetime | None = None) -> int:
        now = now or utc_now()
        with self.session_factory.begin() as session:
            return self._expire_due_approvals(session, now)

    def _renew_expired_action(self, session: Session, queue_item: QueueItem, now: datetime) -> bool:
        action = session.scalar(
            select(Action).where(
                Action.queue_item_id == queue_item.id,
                Action.status == ActionStatus.EXPIRED.value,
            )
        )
        if action is None:
            return False
        old_revision = session.scalar(
            select(ActionRevision).where(
                ActionRevision.action_id == action.id,
                ActionRevision.revision == action.current_revision,
            )
        )
        if old_revision is None or old_revision.account_id is None:
            return False
        account = session.get(Account, old_revision.account_id)
        target = old_revision.target_versions.get("record", {})
        try:
            record_id = uuid.UUID(str(target.get("id")))
        except ValueError:
            return False
        record = session.get(Record, record_id)
        if (
            account is None
            or not account.enabled
            or account.provider != "google"
            or "google_calendar" not in account.capabilities
            or record is None
            or record.version != target.get("version")
            or set(old_revision.target_versions) != {"record", "queue_item"}
            or old_revision.parameters.get("calendar_id") != self.settings.google_calendar_id
        ):
            return False
        action.current_revision += 1
        action.status = ActionStatus.APPROVAL_PENDING.value
        queue_item.status = QueueItemStatus.AWAITING_APPROVAL.value
        queue_item.version += 1
        target_versions = dict(old_revision.target_versions)
        target_versions["queue_item"] = {
            "id": str(queue_item.id),
            "version": queue_item.version,
        }
        revision = ActionRevision(
            action_id=action.id,
            revision=action.current_revision,
            action_type=old_revision.action_type,
            account_id=old_revision.account_id,
            parameters=dict(old_revision.parameters),
            parameters_sha256=old_revision.parameters_sha256,
            preview=dict(old_revision.preview),
            preview_sha256=old_revision.preview_sha256,
            risk_class=old_revision.risk_class,
            target_versions=target_versions,
            created_by_actor_type="docket",
        )
        session.add(revision)
        session.flush()
        approval_id = uuid.uuid4()
        expires_at = now + timedelta(seconds=self.settings.approval_ttl_seconds)
        signing_key = self.settings.read_secret(self.settings.interaction_signing_key_file).encode()
        short_code = issue_short_code(approval_id, expires_at, signing_key)
        session.add(
            Approval(
                id=approval_id,
                action_revision_id=revision.id,
                status=ApprovalStatus.PENDING.value,
                short_code_sha256=short_code_sha256(short_code),
                authorized_user_id=self.settings.operator_discord_user_id,
                requested_at=now,
                expires_at=expires_at,
            )
        )
        session.add(
            AuditEvent(
                event_type="approval.renewed_for_carryover",
                entity_type="action",
                entity_id=action.id,
                actor_type="docket",
                data={
                    "revision": revision.revision,
                    "target_versions": target_versions,
                    "expires_at": expires_at.isoformat(),
                },
            )
        )
        return True

    def _daily_summary(
        self,
        session: Session,
        daily_thread: DiscordDailyThread,
        now: datetime,
        counts: dict[str, int],
    ) -> None:
        key = f"daily_summary:{daily_thread.local_date.isoformat()}"
        item = session.scalar(select(QueueItem).where(QueueItem.deduplication_key == key))
        summary = (
            f"{counts['carried']} carried forward · {counts['woken']} resumed · "
            f"{counts['awaiting']} awaiting approval"
        )
        if item is None:
            item = QueueItem(
                deduplication_key=key,
                material_fingerprint=sha256_json({"date": str(daily_thread.local_date), **counts}),
                category="daily_summary",
                title=f"Docket queue — {daily_thread.local_date.isoformat()}",
                summary=summary,
                status=QueueItemStatus.COMPLETED.value,
                priority="normal",
                received_at=now,
                resolved_at=now,
                resolution_code="daily_summary",
            )
            session.add(item)
            session.flush()
        self._projection_for(session, item, daily_thread, reason="daily_summary")

    def run_due_once(self, now: datetime | None = None) -> bool:
        now = now or utc_now()
        local_now = self._local_now(now)
        if local_now.hour < self.settings.daily_rollover_hour:
            return False
        local_date = local_now.date()
        request_key = f"system:daily_rollover:{local_date.isoformat()}"
        with self.session_factory.begin() as session:
            existing = session.scalar(
                select(CommandRequest).where(CommandRequest.request_key == request_key)
            )
            if existing is not None:
                return False
            command = CommandRequest(
                request_key=request_key,
                operation_name="daily_rollover",
                input_sha256=sha256_json({"local_date": local_date.isoformat()}),
                actor_type="docket",
                status=CommandStatus.IN_PROGRESS.value,
            )
            savepoint = session.begin_nested()
            session.add(command)
            try:
                session.flush()
            except IntegrityError:
                savepoint.rollback()
                return False
            else:
                savepoint.commit()
            self._expire_due_approvals(session, now)
            daily_thread = self._ensure_thread_row(session, local_date)

            woken = 0
            snoozed = session.scalars(
                select(QueueItem).where(
                    QueueItem.status == QueueItemStatus.SNOOZED.value,
                    or_(
                        QueueItem.snoozed_until <= now,
                        QueueItem.snooze_local_date <= local_date,
                    ),
                )
            ).all()
            for item in snoozed:
                pending_approval = session.scalar(
                    select(Approval.id)
                    .join(
                        ActionRevision,
                        ActionRevision.id == Approval.action_revision_id,
                    )
                    .join(Action, Action.id == ActionRevision.action_id)
                    .where(
                        Action.queue_item_id == item.id,
                        ActionRevision.revision == Action.current_revision,
                        Approval.status == ApprovalStatus.PENDING.value,
                    )
                    .limit(1)
                )
                item.status = (
                    QueueItemStatus.AWAITING_APPROVAL.value
                    if pending_approval is not None
                    else QueueItemStatus.PENDING.value
                )
                item.snoozed_until = None
                item.snooze_local_date = None
                if pending_approval is None:
                    item.version += 1
                    ensure_local_actions(session, item, projection_date=local_date)
                session.add(
                    AuditEvent(
                        event_type="queue_item.resumed",
                        entity_type="queue_item",
                        entity_id=item.id,
                        actor_type="docket",
                        data={"version": item.version, "local_date": local_date.isoformat()},
                    )
                )
                woken += 1

            unresolved = session.scalars(
                select(QueueItem).where(QueueItem.status.in_(_UNRESOLVED))
            ).all()
            carried = 0
            awaiting = 0
            for item in unresolved:
                self._renew_expired_action(session, item, now)
                ensure_local_actions(session, item, projection_date=local_date)
                projection, created = self._projection_for(
                    session, item, daily_thread, reason="daily_rollover"
                )
                if created:
                    self._refresh_previous_projection(session, item, projection, local_date)
                    carried += 1
                if item.status == QueueItemStatus.AWAITING_APPROVAL.value:
                    awaiting += 1

            counts = {"carried": carried, "woken": woken, "awaiting": awaiting}
            self._daily_summary(session, daily_thread, now, counts)
            session.add(
                AuditEvent(
                    event_type="queue.rollover_completed",
                    entity_type="discord_daily_thread",
                    entity_id=daily_thread.id,
                    actor_type="docket",
                    request_id=command.id,
                    data={"local_date": local_date.isoformat(), **counts},
                )
            )
            command.status = CommandStatus.SUCCEEDED.value
            command.result = {"daily_thread_id": str(daily_thread.id), **counts}
            command.completed_at = now
        return True

    def maintain_archives(self, now: datetime | None = None) -> int:
        now = now or utc_now()
        local_date = self._local_now(now).date()
        scheduled = 0
        with self.session_factory.begin() as session:
            threads = session.scalars(
                select(DiscordDailyThread).where(
                    DiscordDailyThread.local_date < local_date,
                    DiscordDailyThread.status == "active",
                    DiscordDailyThread.thread_id.is_not(None),
                )
            ).all()
            for daily_thread in threads:
                queue_ids = session.scalars(
                    select(DiscordProjection.queue_item_id).where(
                        DiscordProjection.daily_thread_id == daily_thread.id
                    )
                ).all()
                pending = (
                    session.scalar(
                        select(OutboxEvent.id)
                        .where(
                            OutboxEvent.aggregate_type == "queue_item",
                            OutboxEvent.aggregate_id.in_(queue_ids),
                            OutboxEvent.status.in_(
                                (OutboxStatus.PENDING.value, OutboxStatus.DELIVERING.value)
                            ),
                            OutboxEvent.event_type.in_(
                                (
                                    "discord.projection.requested",
                                    "discord.projection.refresh_requested",
                                )
                            ),
                        )
                        .limit(1)
                    )
                    if queue_ids
                    else None
                )
                if pending is not None:
                    continue
                pending_reminder = session.scalar(
                    select(ScheduledNotification.id)
                    .where(
                        ScheduledNotification.daily_thread_id == daily_thread.id,
                        ScheduledNotification.status == "delivering",
                    )
                    .limit(1)
                )
                if pending_reminder is not None:
                    continue
                already_pending = session.scalar(
                    select(OutboxEvent.id)
                    .where(
                        OutboxEvent.aggregate_type == "discord_daily_thread",
                        OutboxEvent.aggregate_id == daily_thread.id,
                        OutboxEvent.event_type == "discord.thread.lifecycle_requested",
                        OutboxEvent.status.in_(
                            (OutboxStatus.PENDING.value, OutboxStatus.DELIVERING.value)
                        ),
                    )
                    .limit(1)
                )
                if already_pending is not None:
                    continue
                daily_thread.lifecycle_version += 1
                session.add(
                    OutboxEvent(
                        event_type="discord.thread.lifecycle_requested",
                        aggregate_type="discord_daily_thread",
                        aggregate_id=daily_thread.id,
                        deduplication_key=(
                            f"discord_thread_lifecycle:{daily_thread.id}:"
                            f"{daily_thread.lifecycle_version}:archived"
                        ),
                        payload={
                            "daily_thread_id": str(daily_thread.id),
                            "desired_state": "archived",
                        },
                        status=OutboxStatus.PENDING.value,
                    )
                )
                scheduled += 1
        return scheduled
