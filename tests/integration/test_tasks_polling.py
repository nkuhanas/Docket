"""Real paging/lease transactions with a deterministic Google Tasks adapter."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from test_task_policy_authority import admitted_import

from docket.config import get_settings
from docket.models import (
    GoogleTaskList,
    GoogleTaskObservation,
    Task,
    TaskSyncState,
)
from docket.providers.google.fake_tasks import FakeTasksProvider
from docket.providers.google.tasks import TasksProviderError
from docket.services.tasks_polling import TasksPollingService


@pytest.fixture
def setup_polling(session_factory, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "tasks_enabled", True)
    monkeypatch.setattr(settings, "google_account_external_id", "synthetic-tasks")
    with session_factory.begin() as session:
        _, _, _, listing = admitted_import(session)
        listing_ref = listing.ref_id
        session.add(TaskSyncState(task_list_ref=listing_ref))
    provider = FakeTasksProvider(page_size=1)
    provider.seed_list("synthetic-list", "Validation")
    provider.insert_task("synthetic-list", {"title": "First"})
    provider.insert_task("synthetic-list", {"title": "Second"})
    now = [datetime.now(UTC)]
    poller = TasksPollingService(session_factory, provider, settings, clock=lambda: now[0])
    return poller, provider, listing_ref, now


def test_pages_resume_and_only_full_coverage_advances_watermark(session_factory, setup_polling):
    poller, _, ref, now = setup_polling
    assert poller.run_due_once()  # list discovery
    assert poller.run_due_once()  # task page one
    with session_factory() as session:
        state = session.get(TaskSyncState, ref)
        assert state.page_token == "1" and state.last_success_at is None
        assert session.scalar(select(func.count()).select_from(Task)) == 1
        generation = state.generation
    claim = poller._claim()
    assert claim is not None
    assert not poller.run_due_once()  # another worker cannot steal a live page
    now[0] += timedelta(seconds=poller.settings.tasks_lease_seconds + 1)
    assert poller.run_due_once()
    with session_factory() as session:
        state = session.get(TaskSyncState, ref)
        assert state.generation is None and state.page_token is None
        assert state.last_success_at is not None and state.last_full_success_at is not None
        assert state.status == "current"
        assert session.scalar(select(func.count()).select_from(Task)) == 2
        assert set(session.scalars(select(GoogleTaskObservation.inventory_generation).where(
            GoogleTaskObservation.provider_task_id != "synthetic-id",
        ))) == {generation}


def test_observe_inventory_does_not_import_or_infer_deletions(session_factory, setup_polling):
    poller, _, ref, _ = setup_polling
    with session_factory.begin() as session:
        listing = session.get(GoogleTaskList, ref)
        listing.policy_mode = "observe"
    for _ in range(3):
        assert poller.run_due_once()
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 0
        assert session.scalar(select(func.count()).select_from(GoogleTaskObservation)) == 3


def test_revoked_policy_preserves_observations_without_imports(
    session_factory, setup_polling, monkeypatch,
):
    poller, _, ref, _ = setup_polling
    monkeypatch.setattr(poller.settings, "interactive_agent_enabled", False)
    for _ in range(3):
        assert poller.run_due_once()
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 0
        assert session.get(TaskSyncState, ref).last_error_code == "task_sync_policy_denied"
        assert session.scalar(select(func.count()).select_from(GoogleTaskObservation)) == 3


def test_unavailable_list_is_not_mass_task_deletion(session_factory, setup_polling, monkeypatch):
    poller, provider, ref, now = setup_polling
    for _ in range(3):
        poller.run_due_once()
    now[0] += timedelta(seconds=301)

    def inaccessible(*args, **kwargs):
        raise TasksProviderError("google_tasks_not_found", "The list is unavailable.")

    monkeypatch.setattr(provider, "list_tasks", inaccessible)
    assert poller.run_due_once()  # repeat discovery
    assert poller.run_due_once()  # list error
    with session_factory() as session:
        assert set(session.scalars(select(Task.canonical_status))) == {"active"}
        state = session.get(TaskSyncState, ref)
        assert state.last_error_code == "google_tasks_not_found"
        assert state.failure_count == 1 and state.next_attempt_at is not None


def test_disable_switch_prevents_calls(setup_polling, monkeypatch):
    poller, provider, _, _ = setup_polling
    provider.calls.clear()
    monkeypatch.setattr(poller.settings, "tasks_enabled", False)
    assert not poller.run_due_once()
    assert not provider.calls
