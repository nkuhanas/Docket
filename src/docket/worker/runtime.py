import asyncio
from datetime import UTC, datetime

import structlog

logger = structlog.get_logger(__name__)


class WorkerRuntime:
    def __init__(self, heartbeat_seconds: float) -> None:
        self.heartbeat_seconds = heartbeat_seconds
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
        logger.info("worker_started", mode="scaffold")
        while not self._stop.is_set():
            self.last_heartbeat = datetime.now(UTC)
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
