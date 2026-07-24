import asyncio
import threading
import time

import pytest

from docket.worker import WorkerRuntime


class FakeOperationRunner:
    def run_due_once(self) -> bool:
        return False

    def reconcile_once(self) -> bool:
        return False

    def recover_expired_leases(self) -> int:
        return 0


class FakeProjectionRunner:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self.calls = 0
        self.pending = 0
        self.delivered = 0

    def enqueue(self, count: int) -> None:
        with self._condition:
            self.pending += count

    def run_due_once(self) -> bool:
        with self._condition:
            self.calls += 1
            processed = False
            if self.pending:
                self.pending -= 1
                self.delivered += 1
                processed = True
            self._condition.notify_all()
            return processed

    def recover_expired_leases(self) -> int:
        return 0

    def wait_for(self, field: str, minimum: int, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while int(getattr(self, field)) < minimum:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


def _runtime(
    projection: FakeProjectionRunner,
    *,
    projection_poll_seconds: float,
) -> WorkerRuntime:
    return WorkerRuntime(
        60,
        FakeOperationRunner(),  # type: ignore[arg-type]
        operation_poll_seconds=60,
        reconciliation_poll_seconds=60,
        stale_lease_poll_seconds=60,
        discord_projection_runner=projection,  # type: ignore[arg-type]
        discord_projection_poll_seconds=projection_poll_seconds,
    )


@pytest.mark.asyncio
async def test_projection_wake_drains_committed_work_without_waiting_for_poll() -> None:
    projection = FakeProjectionRunner()
    runtime = _runtime(projection, projection_poll_seconds=60)
    await runtime.start()
    try:
        assert await asyncio.to_thread(projection.wait_for, "calls", 1)
        projection.enqueue(3)
        started = time.monotonic()
        wake_results = await asyncio.gather(
            *[asyncio.to_thread(runtime.wake_discord_projection) for _ in range(3)]
        )
        assert wake_results == [True, True, True]
        assert await asyncio.to_thread(projection.wait_for, "delivered", 3)
        assert time.monotonic() - started < 1
    finally:
        await runtime.stop()
    assert runtime.wake_discord_projection() is False


@pytest.mark.asyncio
async def test_projection_poll_remains_a_lost_wake_fallback() -> None:
    projection = FakeProjectionRunner()
    runtime = _runtime(projection, projection_poll_seconds=0.05)
    await runtime.start()
    try:
        assert await asyncio.to_thread(projection.wait_for, "calls", 1)
        projection.enqueue(1)
        assert await asyncio.to_thread(projection.wait_for, "delivered", 1)
    finally:
        await runtime.stop()
