import asyncio
import threading
import time

import pytest

from docket.worker import WorkerRuntime


class FakeOperationRunner:
    def __init__(self, pending: int = 0) -> None:
        self.pending = pending
        self.calls = 0

    def run_due_once(self) -> bool:
        self.calls += 1
        if not self.pending:
            return False
        self.pending -= 1
        return True

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

    def enqueue_stale_projection_repairs(self) -> int:
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


async def _wait_for(projection: FakeProjectionRunner, field: str, minimum: int) -> bool:
    deadline = asyncio.get_running_loop().time() + 1
    while int(getattr(projection, field)) < minimum:
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.01)
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


async def _projection_wake_drains_committed_work_without_waiting_for_poll() -> None:
    projection = FakeProjectionRunner()
    runtime = _runtime(projection, projection_poll_seconds=60)
    await runtime.start()
    try:
        assert await _wait_for(projection, "calls", 1)
        projection.enqueue(3)
        started = time.monotonic()
        wake_results: list[bool] = []
        wake_threads = [
            threading.Thread(
                target=lambda: wake_results.append(runtime.wake_discord_projection())
            )
            for _ in range(3)
        ]
        for thread in wake_threads:
            thread.start()
        while any(thread.is_alive() for thread in wake_threads):
            await asyncio.sleep(0.01)
        for thread in wake_threads:
            thread.join()
        assert wake_results == [True, True, True]
        assert await _wait_for(projection, "delivered", 3)
        assert time.monotonic() - started < 1
    finally:
        await runtime.stop()
    assert runtime.wake_discord_projection() is False


async def _projection_poll_remains_a_lost_wake_fallback() -> None:
    projection = FakeProjectionRunner()
    runtime = _runtime(projection, projection_poll_seconds=0.05)
    await runtime.start()
    try:
        assert await _wait_for(projection, "calls", 1)
        projection.enqueue(1)
        assert await _wait_for(projection, "delivered", 1)
    finally:
        await runtime.stop()


def _run_without_default_executor(monkeypatch: pytest.MonkeyPatch, scenario) -> None:
    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)
    asyncio.run(scenario())


def test_projection_wake_drains_committed_work_without_waiting_for_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_without_default_executor(
        monkeypatch,
        _projection_wake_drains_committed_work_without_waiting_for_poll,
    )


def test_projection_poll_remains_a_lost_wake_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_without_default_executor(
        monkeypatch,
        _projection_poll_remains_a_lost_wake_fallback,
    )


def test_operation_drain_processes_ready_items_without_poll_gaps() -> None:
    runner = FakeOperationRunner(pending=7)
    runtime = WorkerRuntime(
        60,
        runner,  # type: ignore[arg-type]
        operation_poll_seconds=60,
        operation_drain_limit=10,
        reconciliation_poll_seconds=60,
        stale_lease_poll_seconds=60,
    )

    assert asyncio.run(runtime._drain_due_operations()) == 7
    assert runner.calls == 8
    assert runner.pending == 0


def test_operation_drain_honors_bound() -> None:
    runner = FakeOperationRunner(pending=12)
    runtime = WorkerRuntime(
        60,
        runner,  # type: ignore[arg-type]
        operation_poll_seconds=60,
        operation_drain_limit=5,
        reconciliation_poll_seconds=60,
        stale_lease_poll_seconds=60,
    )

    assert asyncio.run(runtime._drain_due_operations()) == 5
    assert runner.calls == 5
    assert runner.pending == 7
