import asyncio
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import structlog

from docket.services.backups import BackupService
from docket.services.briefs import DailyBriefService
from docket.services.calendar_sync import CalendarSyncService
from docket.services.continuity import ExecutionLeaseCoordinator
from docket.services.deferred_ingress import DeferredIngressRunner
from docket.services.discord_projection import DiscordProjectionRunner
from docket.services.gateway_lifetimes import GatewayLifetimeReconciler
from docket.services.gmail_ingestion import GmailIngestionService
from docket.services.operations import OperationRunner

logger = structlog.get_logger(__name__)


class WorkerRuntime:
    def __init__(
        self,
        heartbeat_seconds: float,
        operation_runner: OperationRunner,
        *,
        operation_poll_seconds: float,
        operation_drain_limit: int = 10,
        reconciliation_poll_seconds: float,
        stale_lease_poll_seconds: float,
        discord_projection_runner: DiscordProjectionRunner | None = None,
        discord_projection_poll_seconds: float = 5.0,
        calendar_sync_service: CalendarSyncService | None = None,
        calendar_sync_poll_seconds: float = 60.0,
        backup_service: BackupService | None = None,
        backup_poll_seconds: float = 60.0,
        gmail_ingestion_service: GmailIngestionService | None = None,
        gmail_scan_poll_seconds: float = 60.0,
        daily_brief_service: DailyBriefService | None = None,
        daily_brief_poll_seconds: float = 30.0,
        gateway_lifetime_reconciler: GatewayLifetimeReconciler | None = None,
        execution_lease_coordinator: ExecutionLeaseCoordinator | None = None,
        deferred_ingress_runner: DeferredIngressRunner | None = None,
    ) -> None:
        self.heartbeat_seconds = heartbeat_seconds
        self.operation_runner = operation_runner
        self.operation_poll_seconds = operation_poll_seconds
        self.operation_drain_limit = operation_drain_limit
        self.reconciliation_poll_seconds = reconciliation_poll_seconds
        self.stale_lease_poll_seconds = stale_lease_poll_seconds
        self.discord_projection_runner = discord_projection_runner
        self.discord_projection_poll_seconds = discord_projection_poll_seconds
        self.calendar_sync_service = calendar_sync_service
        self.calendar_sync_poll_seconds = calendar_sync_poll_seconds
        self.backup_service = backup_service
        self.backup_poll_seconds = backup_poll_seconds
        self.gmail_ingestion_service = gmail_ingestion_service
        self.gmail_scan_poll_seconds = gmail_scan_poll_seconds
        self.daily_brief_service = daily_brief_service
        self.daily_brief_poll_seconds = daily_brief_poll_seconds
        self.gateway_lifetime_reconciler = gateway_lifetime_reconciler
        self.execution_lease_coordinator = execution_lease_coordinator
        self.deferred_ingress_runner = deferred_ingress_runner
        self.last_heartbeat: datetime | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._projection_task: asyncio.Task[None] | None = None
        self._projection_wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._loop = asyncio.get_running_loop()
        self._projection_wake = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="docket-worker-heartbeat")
        if self.discord_projection_runner is not None:
            self._projection_task = asyncio.create_task(
                self._run_discord_projection(),
                name="docket-discord-projection",
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._projection_wake is not None:
            self._projection_wake.set()
        if self._task is not None:
            await self._task
            self._task = None
        if self._projection_task is not None:
            await self._projection_task
            self._projection_task = None
        self._projection_wake = None
        self._loop = None

    def wake_discord_projection(self) -> bool:
        """Wake the local projection loop after a transaction commits an outbox row."""
        loop = self._loop
        wake = self._projection_wake
        if (
            self.discord_projection_runner is None
            or loop is None
            or wake is None
            or loop.is_closed()
            or self._stop.is_set()
        ):
            return False
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:
            return False
        return True

    async def _run_discord_projection(self) -> None:
        runner = self.discord_projection_runner
        wake = self._projection_wake
        assert runner is not None
        assert wake is not None
        logger.info(
            "discord_projection_worker_started",
            poll_seconds=self.discord_projection_poll_seconds,
        )
        while not self._stop.is_set():
            # Clear before draining. A commit that arrives while delivery is in
            # progress sets the event again and forces another immediate drain.
            wake.clear()
            try:
                while not self._stop.is_set() and await self._run_leased(
                    "outbox_delivery",
                    "discord-projection",
                    runner.run_due_once,
                ):
                    pass
            except Exception:
                logger.exception("discord_projection_worker_iteration_failed")
            if self._stop.is_set():
                break
            if wake.is_set():
                continue
            try:
                await asyncio.wait_for(
                    wake.wait(),
                    timeout=self.discord_projection_poll_seconds,
                )
            except TimeoutError:
                continue
        logger.info("discord_projection_worker_stopped")

    async def _run(self) -> None:
        logger.info("worker_started", mode="calendar-operations")
        next_operation = 0.0
        next_reconciliation = 0.0
        next_recovery = 0.0
        next_calendar_sync = 0.0
        next_backup = 0.0
        next_gmail_scan = 0.0
        next_daily_brief = 0.0
        next_projection_repair = 0.0
        next_deferred_ingress = 0.0
        while not self._stop.is_set():
            self.last_heartbeat = datetime.now(UTC)
            now = time.monotonic()
            try:
                if now >= next_operation:
                    await self._drain_due_operations()
                    next_operation = now + self.operation_poll_seconds
                if now >= next_reconciliation:
                    await self._run_leased(
                        "provider_call",
                        "operation-reconciliation",
                        self.operation_runner.reconcile_once,
                    )
                    next_reconciliation = now + self.reconciliation_poll_seconds
                if now >= next_recovery:
                    await asyncio.to_thread(self.operation_runner.recover_expired_leases)
                    if self.discord_projection_runner is not None:
                        await asyncio.to_thread(
                            self.discord_projection_runner.recover_expired_leases
                        )
                    if self.calendar_sync_service is not None:
                        await asyncio.to_thread(self.calendar_sync_service.recover_expired_leases)
                    if self.gateway_lifetime_reconciler is not None:
                        await asyncio.to_thread(self.gateway_lifetime_reconciler.run_once)
                    next_recovery = now + self.stale_lease_poll_seconds
                if self.discord_projection_runner is not None and now >= next_projection_repair:
                    repairs = await asyncio.to_thread(
                        self.discord_projection_runner.enqueue_stale_projection_repairs
                    )
                    if repairs:
                        self.wake_discord_projection()
                    next_projection_repair = now + 60.0
                if self.deferred_ingress_runner is not None and now >= next_deferred_ingress:
                    await self._run_leased(
                        "outbox_delivery",
                        "deferred-discord-ingress",
                        self.deferred_ingress_runner.run_once,
                    )
                    next_deferred_ingress = now + self.discord_projection_poll_seconds
                if self.calendar_sync_service is not None and now >= next_calendar_sync:
                    await self._run_leased(
                        "cron_execution",
                        "calendar-sync",
                        self.calendar_sync_service.run_due_once,
                    )
                    await self._run_leased(
                        "cron_execution",
                        "calendar-staleness",
                        self.calendar_sync_service.evaluate_staleness,
                    )
                    next_calendar_sync = now + self.calendar_sync_poll_seconds
                if self.backup_service is not None and now >= next_backup:
                    await self._run_leased(
                        "cron_execution",
                        "backup",
                        self.backup_service.run_due_once,
                    )
                    next_backup = now + self.backup_poll_seconds
                if self.gmail_ingestion_service is not None and now >= next_gmail_scan:
                    await self._run_leased(
                        "cron_execution",
                        "gmail-scan",
                        self.gmail_ingestion_service.run_due_once,
                    )
                    await self._run_leased(
                        "cron_execution",
                        "gmail-staleness",
                        self.gmail_ingestion_service.evaluate_staleness,
                    )
                    next_gmail_scan = now + self.gmail_scan_poll_seconds
                if self.daily_brief_service is not None and now >= next_daily_brief:
                    await self._run_leased(
                        "triage_turn",
                        "daily-brief",
                        self.daily_brief_service.run_due_once,
                    )
                    next_daily_brief = now + self.daily_brief_poll_seconds
            except Exception:
                logger.exception("worker_iteration_failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.heartbeat_seconds)
            except TimeoutError:
                continue
        logger.info("worker_stopped")

    async def _drain_due_operations(self) -> int:
        processed = 0
        while processed < self.operation_drain_limit:
            if not await self._run_leased(
                "provider_call",
                "operation-execution",
                self.operation_runner.run_due_once,
            ):
                break
            processed += 1
            self.last_heartbeat = datetime.now(UTC)
        return processed

    async def _run_leased(
        self,
        lease_kind: str,
        label: str,
        function: Callable[[], Any],
    ) -> Any:
        coordinator = self.execution_lease_coordinator
        if coordinator is None:
            return await asyncio.to_thread(function)
        completion_token = await asyncio.to_thread(
            coordinator.acquire,
            lease_key=f"worker:{label}:{uuid.uuid4()}",
            lease_kind=lease_kind,
        )
        if completion_token is None:
            return None
        try:
            result = await asyncio.to_thread(function)
        except Exception as exc:
            await asyncio.to_thread(
                coordinator.complete,
                completion_token,
                retain=True,
                metadata={"error_code": type(exc).__name__[:128]},
            )
            raise
        await asyncio.to_thread(
            coordinator.complete,
            completion_token,
            retain=bool(result),
        )
        return result

    def is_healthy(self) -> bool:
        if (
            self.last_heartbeat is None
            or self._task is None
            or self._task.done()
            or (
                self.discord_projection_runner is not None
                and (self._projection_task is None or self._projection_task.done())
            )
        ):
            return False
        age = datetime.now(UTC) - self.last_heartbeat
        return age.total_seconds() <= max(5.0, self.heartbeat_seconds * 3)
