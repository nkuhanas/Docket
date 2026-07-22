import asyncio
import time
from datetime import UTC, datetime

import structlog

from docket.services.calendar_sync import CalendarSyncService
from docket.services.discord_projection import DiscordProjectionRunner
from docket.services.operations import OperationRunner
from docket.services.reminders import ReminderDispatcher
from docket.services.rollover import RolloverService

logger = structlog.get_logger(__name__)


class WorkerRuntime:
    def __init__(
        self,
        heartbeat_seconds: float,
        operation_runner: OperationRunner,
        *,
        operation_poll_seconds: float,
        reconciliation_poll_seconds: float,
        stale_lease_poll_seconds: float,
        discord_projection_runner: DiscordProjectionRunner | None = None,
        discord_projection_poll_seconds: float = 5.0,
        rollover_service: RolloverService | None = None,
        rollover_poll_seconds: float = 60.0,
        calendar_sync_service: CalendarSyncService | None = None,
        calendar_sync_poll_seconds: float = 60.0,
        reminder_dispatcher: ReminderDispatcher | None = None,
        reminder_dispatch_poll_seconds: float = 30.0,
    ) -> None:
        self.heartbeat_seconds = heartbeat_seconds
        self.operation_runner = operation_runner
        self.operation_poll_seconds = operation_poll_seconds
        self.reconciliation_poll_seconds = reconciliation_poll_seconds
        self.stale_lease_poll_seconds = stale_lease_poll_seconds
        self.discord_projection_runner = discord_projection_runner
        self.discord_projection_poll_seconds = discord_projection_poll_seconds
        self.rollover_service = rollover_service
        self.rollover_poll_seconds = rollover_poll_seconds
        self.calendar_sync_service = calendar_sync_service
        self.calendar_sync_poll_seconds = calendar_sync_poll_seconds
        self.reminder_dispatcher = reminder_dispatcher
        self.reminder_dispatch_poll_seconds = reminder_dispatch_poll_seconds
        self.last_heartbeat: datetime | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="docket-worker-heartbeat")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        logger.info("worker_started", mode="calendar-operations")
        next_operation = 0.0
        next_reconciliation = 0.0
        next_recovery = 0.0
        next_discord_projection = 0.0
        next_rollover = 0.0
        next_calendar_sync = 0.0
        next_reminder_dispatch = 0.0
        while not self._stop.is_set():
            self.last_heartbeat = datetime.now(UTC)
            now = time.monotonic()
            try:
                if now >= next_operation:
                    await asyncio.to_thread(self.operation_runner.run_due_once)
                    next_operation = now + self.operation_poll_seconds
                if now >= next_reconciliation:
                    await asyncio.to_thread(self.operation_runner.reconcile_once)
                    next_reconciliation = now + self.reconciliation_poll_seconds
                if now >= next_recovery:
                    await asyncio.to_thread(self.operation_runner.recover_expired_leases)
                    if self.discord_projection_runner is not None:
                        await asyncio.to_thread(
                            self.discord_projection_runner.recover_expired_leases
                        )
                    if self.calendar_sync_service is not None:
                        await asyncio.to_thread(self.calendar_sync_service.recover_expired_leases)
                    next_recovery = now + self.stale_lease_poll_seconds
                if self.discord_projection_runner is not None and now >= next_discord_projection:
                    await asyncio.to_thread(self.discord_projection_runner.run_due_once)
                    next_discord_projection = now + self.discord_projection_poll_seconds
                if self.rollover_service is not None and now >= next_rollover:
                    await asyncio.to_thread(self.rollover_service.expire_due_approvals)
                    await asyncio.to_thread(self.rollover_service.run_due_once)
                    await asyncio.to_thread(self.rollover_service.maintain_archives)
                    next_rollover = now + self.rollover_poll_seconds
                if self.calendar_sync_service is not None and now >= next_calendar_sync:
                    await asyncio.to_thread(self.calendar_sync_service.run_due_once)
                    await asyncio.to_thread(self.calendar_sync_service.evaluate_staleness)
                    next_calendar_sync = now + self.calendar_sync_poll_seconds
                if self.reminder_dispatcher is not None and now >= next_reminder_dispatch:
                    await asyncio.to_thread(self.reminder_dispatcher.run_due_once)
                    next_reminder_dispatch = now + self.reminder_dispatch_poll_seconds
            except Exception:
                logger.exception("worker_iteration_failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.heartbeat_seconds)
            except TimeoutError:
                continue
        logger.info("worker_stopped")

    def is_healthy(self) -> bool:
        if self.last_heartbeat is None:
            return False
        age = datetime.now(UTC) - self.last_heartbeat
        return age.total_seconds() <= max(5.0, self.heartbeat_seconds * 3)
