"""Bounded, resumable Google Tasks inventory; no network I/O under row locks."""

import random
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from docket.agent_auth import require_principal
from docket.config import Settings
from docket.domain.errors import DocketError
from docket.models import (
    GoogleTaskList,
    ProviderAccount,
    TasksAccountState,
    TaskSyncState,
)
from docket.models.base import utc_now
from docket.providers.google.tasks import TasksProvider, TasksProviderError
from docket.services.task_item_titles import lock_task_rows
from docket.services.task_lists import TaskListService
from docket.services.task_sync import TaskSyncService


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC)


class TasksPollingService:
    def __init__(
        self, session_factory: sessionmaker[Session], provider: TasksProvider, settings: Settings,
        *, clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.factory, self.provider, self.settings, self.clock = (
            session_factory, provider, settings, clock
        )

    @staticmethod
    def request(session: Session, listing_ref: str | None) -> dict[str, Any]:
        principal = require_principal("read")
        if listing_ref:
            listing = TaskListService(session).owned(listing_ref)
            if listing.policy_mode == "disabled":
                raise DocketError(
                    code="task_list_disabled", message="Select an observe or two-way list."
                )
            state = session.get(TaskSyncState, listing_ref)
            if state is None:
                state = TaskSyncState(task_list_ref=listing_ref)
                session.add(state)
            state.requested = True
            state.next_attempt_at = None
        else:
            accounts = session.scalars(select(ProviderAccount).where(
                ProviderAccount.provider == "google", ProviderAccount.enabled.is_(True),
            )).all()
            for account in accounts:
                if "google_tasks" not in account.capabilities:
                    continue
                state_account = session.get(TasksAccountState, account.id)
                if state_account is None:
                    state_account = TasksAccountState(
                        account_id=account.id, operator_ref=principal.operator_ref,
                    )
                    session.add(state_account)
                state_account.requested, state_account.next_attempt_at = True, None
            for sync in session.scalars(select(TaskSyncState).join(GoogleTaskList).where(
                GoogleTaskList.operator_ref == principal.operator_ref,
                GoogleTaskList.policy_mode != "disabled",
            )):
                sync.requested, sync.next_attempt_at = True, None
        return {"ok": True, "refresh": "queued", "task_list_ref": listing_ref,
                "canonical_effects": "Only an existing, current two-way policy permits imports."}

    def _due(self, model: Any, now: datetime) -> Any:
        return (
            or_(model.leased_until.is_(None), model.leased_until <= now),
            or_(model.next_attempt_at.is_(None), model.next_attempt_at <= now),
        )

    def _claim(
        self,
    ) -> tuple[str, str | uuid.UUID, uuid.UUID, str, str | None, datetime | None] | None:
        now = aware(self.clock())
        with self.factory.begin() as session:
            # This configured credential belongs to exactly the configured account.
            account = session.scalar(select(ProviderAccount).where(
                ProviderAccount.provider == "google", ProviderAccount.enabled.is_(True),
                ProviderAccount.external_account_id == self.settings.google_account_external_id,
            ))
            if account is None or "google_tasks" not in account.capabilities:
                return None
            discovery = session.get(TasksAccountState, account.id)
            if discovery is None:
                session.add(TasksAccountState(
                    account_id=account.id, operator_ref=self.settings.canonical_operator_ref,
                ))
                session.flush()
            discovery = session.scalar(select(TasksAccountState).where(
                TasksAccountState.account_id == account.id,
                TasksAccountState.operator_ref == self.settings.canonical_operator_ref,
                *self._due(TasksAccountState, now),
            ).with_for_update(skip_locked=True))
            if discovery is not None:
                token = uuid.uuid4()
                discovery.lease_token = token
                discovery.leased_until = now + timedelta(seconds=self.settings.tasks_lease_seconds)
                discovery.last_attempt_at = now
                if discovery.generation is None:
                    discovery.generation = uuid.uuid4()
                return "discovery", account.id, token, "", discovery.page_token, None
            state = session.scalar(select(TaskSyncState).join(GoogleTaskList).where(
                GoogleTaskList.account_id == account.id,
                GoogleTaskList.operator_ref == self.settings.canonical_operator_ref,
                GoogleTaskList.available.is_(True), GoogleTaskList.policy_mode != "disabled",
                *self._due(TaskSyncState, now),
            ).order_by(TaskSyncState.last_attempt_at.asc().nulls_first())
                .with_for_update(of=TaskSyncState, skip_locked=True))
            if state is None:
                return None
            listing = session.get(GoogleTaskList, state.task_list_ref)
            assert listing is not None
            token = uuid.uuid4()
            state.lease_token = token
            state.leased_until = now + timedelta(seconds=self.settings.tasks_lease_seconds)
            state.last_attempt_at = now
            if state.generation is None or state.scan_policy_ref != listing.policy_ref:
                state.generation, state.page_token = uuid.uuid4(), None
                state.scan_started_at, state.scan_policy_ref = now, listing.policy_ref
                full = (state.last_full_success_at is None
                        or (now - aware(state.last_full_success_at)).total_seconds()
                        >= self.settings.tasks_full_inventory_seconds)
                state.scan_updated_min = (
                    None if full or state.last_success_at is None
                    else aware(state.last_success_at) - timedelta(minutes=2)
                )
            return ("tasks", listing.ref_id, token, listing.provider_list_id,
                    state.page_token, state.scan_updated_min)

    def _failure(self, kind: str, ref: str | uuid.UUID, token: uuid.UUID, code: str) -> None:
        with self.factory.begin() as session:
            row = (session.get(TasksAccountState, ref, with_for_update=True)
                   if kind == "discovery"
                   else session.get(TaskSyncState, ref, with_for_update=True))
            if row is None or row.lease_token != token:
                return
            row.failure_count += 1
            row.last_error_code = code
            row.next_attempt_at = aware(self.clock()) + timedelta(
                seconds=min(3600, 15 * 2 ** min(row.failure_count, 8)) + random.uniform(0, 5)
            )
            row.lease_token, row.leased_until = None, None
            if code == "google_tasks_invalid_request":
                # An expired page token cannot establish full inventory coverage.
                row.page_token, row.generation = None, None

    def run_due_once(self) -> bool:
        if not self.settings.tasks_enabled:
            return False
        claimed = self._claim()
        if claimed is None:
            return False
        kind, ref, token, provider_list, page_token, updated_min = claimed
        started = aware(self.clock())
        try:
            if kind == "discovery":
                lists = self.provider.list_task_lists(page_token=page_token)
                with self.factory.begin() as session:
                    # Every list mutation follows the same graph → list lock order.
                    lock_task_rows(session)
                    state = session.get(TasksAccountState, ref, with_for_update=True)
                    if state is None or state.lease_token != token:
                        return True
                    account = session.get(ProviderAccount, ref)
                    if account is None or not account.enabled:
                        state.lease_token, state.leased_until = None, None
                        return True
                    for value in lists.lists:
                        provider_id, title = value.get("id"), value.get("title")
                        if (not isinstance(provider_id, str) or not provider_id
                                or not isinstance(title, str) or len(title) > 1024):
                            raise TasksProviderError(
                                "google_tasks_invalid_list", "A task list could not be read."
                            )
                        listing = session.scalar(select(GoogleTaskList).where(
                            GoogleTaskList.account_id == ref,
                            GoogleTaskList.provider_list_id == provider_id,
                        ))
                        if listing is None:
                            listing = GoogleTaskList(
                                account_id=ref, operator_ref=self.settings.canonical_operator_ref,
                                provider_list_id=provider_id, title=title,
                            )
                            session.add(listing)
                            session.flush()
                            session.add(TaskSyncState(task_list_ref=listing.ref_id))
                        elif listing.title != title or not listing.available:
                            listing.version += 1
                        listing.title, listing.available = title, True
                        listing.discovery_generation = state.generation
                        listing.observed_at = aware(self.clock())
                    state.page_token = lists.next_page_token
                    if state.page_token is None:
                        for absent in session.scalars(select(GoogleTaskList).where(
                            GoogleTaskList.account_id == ref,
                            or_(GoogleTaskList.discovery_generation != state.generation,
                                GoogleTaskList.discovery_generation.is_(None)),
                            GoogleTaskList.available.is_(True),
                        )):
                            absent.available = False
                            absent.version += 1
                        state.generation = None
                        state.last_success_at = aware(self.clock())
                        state.requested = False
                        state.next_attempt_at = state.last_success_at + timedelta(
                            seconds=self.settings.tasks_sync_interval_seconds,
                        )
                    else:
                        state.next_attempt_at = None
                    state.failure_count, state.last_error_code = 0, None
                    state.lease_token, state.leased_until = None, None
            else:
                page = self.provider.list_tasks(
                    provider_list, page_token=page_token,
                    updated_min=updated_min, show_assigned=True,
                )
                assert isinstance(ref, str)
                with self.factory.begin() as session:
                    lock_task_rows(session, lists=[ref])
                    state_tasks = session.get(TaskSyncState, ref, with_for_update=True)
                    listing = session.get(GoogleTaskList, ref)
                    if state_tasks is None or state_tasks.lease_token != token or listing is None:
                        return True
                    current_account = session.get(ProviderAccount, listing.account_id)
                    if (listing.policy_ref != state_tasks.scan_policy_ref
                            or listing.policy_mode == "disabled" or not listing.available
                            or current_account is None or not current_account.enabled
                            or "google_tasks" not in current_account.capabilities):
                        state_tasks.generation, state_tasks.page_token = None, None
                        state_tasks.lease_token, state_tasks.leased_until = None, None
                        state_tasks.requested = True
                        return True
                    service = TaskSyncService(session, self.settings)
                    assert state_tasks.generation is not None
                    observations = [service.observe(
                        listing, task, generation=state_tasks.generation, read_started_at=started,
                    ) for task in page.tasks]
                    # Parents present in this page are admitted first. A parent on
                    # a later page remains an observation and is retried next scan.
                    pending = list(observations)
                    ordered = []
                    while pending:
                        ids = {row.provider_task_id for row in pending}
                        ready = [row for row in pending if row.fields.get("parent") not in ids]
                        if not ready:
                            ready = list(pending)
                        ordered.extend(ready)
                        pending = [row for row in pending if row not in ready]
                    warning = None
                    for observation in ordered:
                        try:
                            with session.begin_nested():
                                result = service.reconcile(observation)
                                if result["state"] == "fresh_read_required":
                                    warning = "task_fresh_read_required"
                        except DocketError as exc:
                            # Retain the observation without blocking independent
                            # imports. Revoked policy grants never enter canonical state.
                            warning = exc.code
                    state_tasks.page_token = page.next_page_token
                    state_tasks.failure_count = 0
                    state_tasks.last_error_code = warning
                    state_tasks.lease_token, state_tasks.leased_until = None, None
                    if page.next_page_token is None:
                        state_tasks.status = "current"
                        state_tasks.last_success_at = state_tasks.scan_started_at
                        if state_tasks.scan_updated_min is None:
                            state_tasks.last_full_success_at = state_tasks.scan_started_at
                        state_tasks.generation = None
                        state_tasks.requested = False
                        state_tasks.next_attempt_at = aware(self.clock()) + timedelta(
                            seconds=self.settings.tasks_sync_interval_seconds,
                        )
                    else:
                        state_tasks.status, state_tasks.next_attempt_at = "paging", None
        except TasksProviderError as exc:
            self._failure(kind, ref, token, exc.code)
        return True
