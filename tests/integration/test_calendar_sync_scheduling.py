from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.models import CalendarLane, CalendarSyncState, ProviderAccount
from docket.providers.google.calendar import CalendarProviderError, CalendarSnapshotPage
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.services.calendar_sync import CalendarSyncService

BASE = datetime(2026, 9, 29, 5, tzinfo=UTC)
pytestmark = pytest.mark.integration


class RecordingProvider(FakeCalendarProvider):
    def __init__(self) -> None:
        super().__init__()
        self.targets: list[str] = []
        self.failing: set[str] = set()

    def list_events_page(
        self, *, calendar_id: str, time_min: object, time_max: object, page_token: str | None
    ) -> CalendarSnapshotPage:
        self.targets.append(calendar_id)
        if calendar_id in self.failing:
            raise CalendarProviderError("google_auth_invalid", "Injected failure", transient=False)
        return super().list_events_page(
            calendar_id=calendar_id, time_min=time_min, time_max=time_max, page_token=page_token
        )


def _lanes(factory: sessionmaker[Session], count: int) -> list[CalendarLane]:
    with factory.begin() as session:
        accounts = [
            ProviderAccount(
                provider="google",
                external_account_id=f"sync-account-{i}",
                capabilities=["google_calendar"],
                enabled=True,
            )
            for i in range(2)
        ]
        session.add_all(accounts)
        session.flush()
        lanes = [
            CalendarLane(
                account_id=accounts[i % 2].id,
                lane=f"class_{i}",
                display_name=f"Class {i}",
                calendar_id=f"class-{i}",
                status="active",
                enabled=True,
                color_hex="#123456",
                basis_refs=[new_public_ref("utt")],
                created_by_changeset_ref=new_public_ref("chg"),
                updated_at=BASE - timedelta(days=i + 1),
            )
            for i in range(count)
        ]
        session.add_all(lanes)
        session.flush()
        # The public ref is the deterministic tie-breaker for unattempted lanes.
        lanes.sort(key=lambda lane: lane.ref_id)
        session.expunge_all()
        return lanes


def _state(lane: CalendarLane, *, attempted: datetime | None, status: str = "current"):
    return CalendarSyncState(
        account_id=lane.account_id,
        calendar_id=lane.calendar_id,
        window_start=BASE - timedelta(days=30),
        window_end=BASE + timedelta(days=400),
        status=status,
        last_attempt_at=attempted,
        last_success_at=attempted if status == "current" else None,
    )


def test_all_twelve_lanes_sync_across_restarts_without_editing_lanes(session_factory) -> None:
    lanes = _lanes(session_factory, 12)
    original_lanes = {
        lane.id: (lane.version, lane.updated_at.replace(tzinfo=UTC)) for lane in lanes
    }
    clock = [BASE]
    settings = get_settings().model_copy(update={"calendar_reads_enabled": True})
    provider = RecordingProvider()

    def service():
        return CalendarSyncService(session_factory, provider, settings, clock=lambda: clock[0])

    # Recreate the service for every poll: fairness must be durable, not a cursor
    # in process memory. Missing sync rows are eligible via the outer join.
    for _ in lanes:
        assert service().run_due_once()
    assert provider.targets == [lane.calendar_id for lane in lanes]
    assert not service().run_due_once()

    clock[0] += timedelta(seconds=settings.calendar_sync_interval_seconds)
    for _ in lanes:
        assert service().run_due_once()
    assert not service().run_due_once()
    assert Counter(provider.targets) == {lane.calendar_id: 2 for lane in lanes}
    with session_factory() as session:
        states = session.scalars(select(CalendarSyncState)).all()
        assert len(states) == 12
        assert all(state.status == "current" and state.last_error_code is None for state in states)
        assert all(state.last_success_at.replace(tzinfo=UTC) == clock[0] for state in states)
        assert {
            lane.id: (lane.version, lane.updated_at.replace(tzinfo=UTC))
            for lane in session.scalars(select(CalendarLane))
        } == original_lanes


def test_oldest_due_attempt_wins_over_lane_edit_or_success_time(session_factory) -> None:
    lanes = _lanes(session_factory, 3)
    with session_factory.begin() as session:
        session.add_all(
            [
                _state(lanes[0], attempted=BASE - timedelta(seconds=10), status="stale"),
                _state(lanes[1], attempted=BASE - timedelta(days=2)),
                _state(lanes[2], attempted=BASE - timedelta(days=3), status="failed"),
            ]
        )
    settings = get_settings().model_copy(update={"calendar_reads_enabled": True})
    provider = RecordingProvider()
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: BASE)
    assert sync.run_due_once()
    assert sync.run_due_once()
    assert not sync.run_due_once()
    assert provider.targets == [lanes[2].calendar_id, lanes[1].calendar_id]


@pytest.mark.parametrize("prior_success", [False, True])
def test_auth_failure_obeys_cooldown_and_does_not_starve_healthy_lanes(
    session_factory, prior_success: bool
) -> None:
    lanes = _lanes(session_factory, 3)
    failed_calendar = lanes[0].calendar_id
    with session_factory.begin() as session:
        row = _state(
            lanes[0],
            attempted=BASE - timedelta(days=5),
            status="stale" if prior_success else "failed",
        )
        row.last_success_at = BASE - timedelta(days=6) if prior_success else None
        row.snapshot_generation = uuid.uuid4() if prior_success else None
        row.last_error_code = "google_auth_invalid"
        session.add(row)
        generation = row.snapshot_generation
    settings = get_settings().model_copy(update={"calendar_reads_enabled": True})
    provider = RecordingProvider()
    provider.failing.add(failed_calendar)
    clock = [BASE]
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: clock[0])
    for _ in lanes:
        assert sync.run_due_once()
    for _ in range(5):
        assert not sync.run_due_once()
        assert not sync.sync_target(lanes[0].account_id, failed_calendar)
    assert Counter(provider.targets) == {lane.calendar_id: 1 for lane in lanes}
    with session_factory() as session:
        row = session.scalar(
            select(CalendarSyncState).where(CalendarSyncState.calendar_id == failed_calendar)
        )
        assert row.status == ("stale" if prior_success else "failed")
        assert row.last_error_code == "google_auth_invalid"
        assert row.snapshot_generation == generation

    # Provider recovery needs no OAuth script or manual sync-state edits for
    # reads: the next ordinary due attempt clears the old error on real success.
    provider.failing.clear()
    clock[0] += timedelta(seconds=settings.calendar_sync_interval_seconds)
    for _ in lanes:
        assert sync.run_due_once()
    with session_factory() as session:
        assert all(
            row.status == "current" and row.last_error_code is None
            for row in session.scalars(select(CalendarSyncState))
        )


@pytest.mark.parametrize("recover_expired", [False, True])
def test_leased_target_is_skipped_and_old_completion_cannot_replace_recovery(
    session_factory, recover_expired: bool
):
    lanes = _lanes(session_factory, 2)
    settings = get_settings().model_copy(
        update={
            "calendar_reads_enabled": True,
            "calendar_sync_lease_seconds": 600,
        }
    )
    provider = RecordingProvider()
    clock = [BASE]
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: clock[0])
    first = lanes[0]
    old_claim = sync._claim(first.account_id, first.calendar_id, force=False)
    assert old_claim is not None
    clock[0] += timedelta(seconds=300)
    assert sync.run_due_once()
    assert provider.targets == [lanes[1].calendar_id]
    assert not sync.run_due_once()
    assert not sync.sync_target(first.account_id, first.calendar_id, force=True)
    clock[0] += timedelta(seconds=300)
    if recover_expired:
        clock[0] += timedelta(seconds=1)
        assert sync.recover_expired_leases() == 1
    assert sync.run_due_once()
    with session_factory() as session:
        generation = session.get(CalendarSyncState, old_claim[0]).snapshot_generation
    with pytest.raises(DocketError) as rejected:
        sync._promote(*old_claim, [])
    assert rejected.value.code == "calendar_sync_lease_lost"
    sync._mark_failed(old_claim[0], old_claim[1], "late_failure")
    with session_factory() as session:
        row = session.get(CalendarSyncState, old_claim[0])
        assert row.snapshot_generation == generation
        assert row.status == "current" and row.last_error_code is None


@pytest.mark.parametrize(
    "excluded",
    [
        "disabled_lane",
        "disabled_account",
        "unprovisioned",
        "provisioning",
        "deleting",
        "deleted",
        "failed",
        "no_calendar",
        "other_provider",
    ],
)
def test_ineligible_target_cannot_block_due_lane(session_factory, excluded: str) -> None:
    lanes = _lanes(session_factory, 2)
    first = lanes[0]
    with session_factory.begin() as session:
        lane = session.get(CalendarLane, first.id)
        if excluded == "disabled_lane":
            lane.enabled = False
        elif excluded == "disabled_account":
            session.get(ProviderAccount, first.account_id).enabled = False
        elif excluded == "other_provider":
            session.get(ProviderAccount, first.account_id).provider = "discord"
        elif excluded == "no_calendar":
            lane.calendar_id = None
        else:
            lane.status = excluded
    provider = RecordingProvider()
    settings = get_settings().model_copy(update={"calendar_reads_enabled": True})
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: BASE)
    assert sync.run_due_once()
    assert not sync.run_due_once()
    assert provider.targets == [lanes[1].calendar_id]


def test_disabled_read_gate_does_not_create_state_or_call_provider(session_factory) -> None:
    _lanes(session_factory, 2)
    provider = RecordingProvider()
    settings = get_settings().model_copy(update={"calendar_reads_enabled": False})
    assert not CalendarSyncService(session_factory, provider, settings).run_due_once()
    assert provider.targets == []
    with session_factory() as session:
        assert session.scalar(select(CalendarSyncState)) is None


def test_explicit_refresh_bypasses_interval_but_background_poll_does_not(session_factory) -> None:
    lane = _lanes(session_factory, 1)[0]
    provider = RecordingProvider()
    settings = get_settings().model_copy(update={"calendar_reads_enabled": True})
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: BASE)
    assert sync.run_due_once()
    assert not sync.run_due_once()
    assert sync.sync_target(lane.account_id, lane.calendar_id, force=True)
    assert provider.targets == [lane.calendar_id, lane.calendar_id]
