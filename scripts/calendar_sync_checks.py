"""Calendar read-scheduler locking checks for the isolated PostgreSQL smoke."""

import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import select, text

from docket.config import get_settings
from docket.domain.public_refs import new_public_ref
from docket.models import CalendarLane, CalendarSyncState, ProviderAccount
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.services.calendar_sync import CalendarSyncService


@contextmanager
def _targets(factory, count):
    with factory.begin() as session:
        account = ProviderAccount(
            provider="google",
            external_account_id=new_public_ref("acct"),
            capabilities=["google_calendar"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        lanes = [
            CalendarLane(
                account_id=account.id,
                lane=f"sync_{i}",
                display_name=f"Sync fixture {i}",
                calendar_id=f"calendar-{i}",
                status="active",
                color_hex="#123456",
                basis_refs=[new_public_ref("utt")],
                created_by_changeset_ref=new_public_ref("chg"),
            )
            for i in range(count)
        ]
        session.add_all(lanes)
        session.flush()
        account_id = account.id
        lanes.sort(key=lambda lane: lane.ref_id)
        session.expunge_all()
    try:
        yield account_id, lanes
    finally:
        # This fixture runs before other assembly fixtures. Leave no eligible
        # target behind for subsequent independent checks in this smoke DB.
        with factory.begin() as session:
            session.get(ProviderAccount, account_id).enabled = False


def _service(factory, provider):
    settings = get_settings().model_copy(update={"calendar_reads_enabled": True})
    return CalendarSyncService(
        factory, provider, settings, clock=lambda: datetime(2026, 9, 29, 5, tzinfo=UTC)
    )


def test_calendar_sync_skips_locked_targets_and_commits_claim_before_io(factory) -> None:
    with _targets(factory, 2) as (account_id, lanes):

        class ProbeProvider(FakeCalendarProvider):
            def __init__(self):
                super().__init__()
                self.targets = []

            def list_events_page(self, *, calendar_id, time_min, time_max, page_token):
                self.targets.append(calendar_id)
                # Independent connection: the lease must already be durable,
                # and neither canonical lane nor sync row locked during I/O.
                with factory.begin() as inspecting:
                    inspecting.execute(text("SET LOCAL lock_timeout = '500ms'"))
                    lane = inspecting.scalar(
                        select(CalendarLane)
                        .where(
                            CalendarLane.account_id == account_id,
                            CalendarLane.calendar_id == calendar_id,
                        )
                        .with_for_update()
                    )
                    state = inspecting.scalar(
                        select(CalendarSyncState)
                        .where(
                            CalendarSyncState.account_id == account_id,
                            CalendarSyncState.calendar_id == calendar_id,
                        )
                        .with_for_update()
                    )
                    assert lane is not None
                    assert state is not None and state.status == "syncing"
                    assert state.lease_token is not None and state.last_attempt_at is not None
                return super().list_events_page(
                    calendar_id=calendar_id,
                    time_min=time_min,
                    time_max=time_max,
                    page_token=page_token,
                )

        provider = ProbeProvider()
        sync = _service(factory, provider)
        with ThreadPoolExecutor(max_workers=1) as pool, factory.begin() as holding:
            holding.scalar(
                select(CalendarLane)
                .where(
                    CalendarLane.id == lanes[0].id,
                )
                .with_for_update()
            )
            assert pool.submit(sync.run_due_once).result(timeout=10)
            assert provider.targets == [lanes[1].calendar_id]
        assert sync.run_due_once()
        assert provider.targets == [lanes[1].calendar_id, lanes[0].calendar_id]
        assert not sync.run_due_once()


def test_calendar_sync_first_state_race_claims_once_and_keeps_other_lanes_moving(factory) -> None:
    with _targets(factory, 2) as (account_id, lanes):
        entered, release = threading.Event(), threading.Event()
        barrier = threading.Barrier(2)

        class BlockingProvider(FakeCalendarProvider):
            def __init__(self):
                super().__init__()
                self.targets = []

            def list_events_page(self, *, calendar_id, time_min, time_max, page_token):
                self.targets.append(calendar_id)
                if calendar_id == lanes[0].calendar_id:
                    entered.set()
                    assert release.wait(timeout=10)
                return super().list_events_page(
                    calendar_id=calendar_id,
                    time_min=time_min,
                    time_max=time_max,
                    page_token=page_token,
                )

        provider = BlockingProvider()

        def foreground_refresh():
            barrier.wait(timeout=10)
            return _service(factory, provider).sync_target(
                account_id,
                lanes[0].calendar_id,
                force=True,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(foreground_refresh) for _ in range(2)]
            try:
                assert entered.wait(timeout=10)
                done, pending = wait(futures, timeout=10, return_when=FIRST_COMPLETED)
                assert len(done) == len(pending) == 1
                assert next(iter(done)).result() is False
                assert _service(factory, provider).run_due_once()
                assert provider.targets == [lanes[0].calendar_id, lanes[1].calendar_id]
            finally:
                release.set()
            assert sorted(future.result(timeout=10) for future in futures) == [False, True]
        with factory() as session:
            states = session.scalars(
                select(CalendarSyncState).where(
                    CalendarSyncState.account_id == account_id,
                )
            ).all()
            assert len(states) == 2
            assert all(state.status == "current" and state.lease_token is None for state in states)
        assert not _service(factory, provider).run_due_once()
