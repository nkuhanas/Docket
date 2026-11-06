"""Imports and three-way reconciliation use canonical transactions and real policy roots."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from test_task_policy_authority import admitted_import

from docket.config import get_settings
from docket.models import (
    AuditEvent,
    AuthenticatedRequest,
    ChangeSet,
    GoogleTaskObservation,
    Item,
    OperatorUtterance,
    Task,
    TaskSyncConflict,
)
from docket.providers.google.tasks import GoogleTask
from docket.schemas.authority import TaskModify
from docket.services.task_bindings import TaskBindingService
from docket.services.task_sync import TaskSyncService
from docket.services.tracked_context import TrackedContextService


@pytest.fixture(autouse=True)
def enabled_tasks(monkeypatch):
    monkeypatch.setattr(get_settings(), "tasks_enabled", True)


def imported(session):
    _, _, effect, listing = admitted_import(session)
    observation = session.get(GoogleTaskObservation, effect.observation_ref)
    service = TaskSyncService(session)
    result = service.reconcile(observation)
    task = session.scalar(select(Task).where(Task.ref_id == result["task_ref"]))
    return service, listing, task


def changed(service, listing, task, **fields):
    original = service.session.get(GoogleTaskObservation, task.observation_ref)
    return service.observe(
        listing,
        GoogleTask(original.provider_task_id, '"changed"', {**original.fields, **fields}),
        generation=uuid.uuid4(), read_started_at=datetime.now(UTC),
    )


def edit(session, task, **fields):
    changeset = session.scalar(select(ChangeSet).where(
        ChangeSet.ref_id == task.created_by_changeset_ref,
    ))
    TrackedContextService(session).apply_task(session, changeset, TaskModify.model_validate({
        "object_type": "task", "action": "update", "object_ref": task.ref_id,
        "change_id": "fixture-local-edit", "affected_fields": list(fields),
        "basis_refs": list(task.basis_refs), "payload": fields,
    }))


def test_import_retains_complete_title_and_is_idempotent(session):
    service, listing, task = imported(session)
    binding = service.bindings.active(task.ref_id)
    observed = session.get(GoogleTaskObservation, binding.observation_ref)
    assert len(task.title) == 1024
    assert session.scalar(select(Item.title)) == task.title
    assert task.task_list_ref == listing.ref_id
    assert service.reconcile(observed)["state"] == "unchanged"
    assert session.scalar(select(func.count()).select_from(Task)) == 1
    assert session.scalar(select(func.count()).select_from(AuthenticatedRequest)) == 1
    assert session.scalar(select(func.count()).select_from(OperatorUtterance)) == 0
    task_audit = session.scalar(select(AuditEvent).where(AuditEvent.event_type == "task.changed"))
    assert task_audit.actor_type == "task_sync_policy"
    assert task_audit.actor_id == listing.policy_ref


def test_google_rename_updates_owned_item_but_preserves_independent_local_notes(session):
    service, listing, task = imported(session)
    binding = service.bindings.active(task.ref_id)
    edit(session, task, description="Independent local notes")
    observation = changed(service, listing, binding, title="Renamed in Google")
    result = service.reconcile(observation)
    assert result["state"] == "reconciled"
    assert task.title == "Renamed in Google"
    assert task.description == "Independent local notes"
    assert session.scalar(select(Item.title)) == task.title
    assert binding.baseline["notes"] == ""


def test_field_conflicts_do_not_block_independent_inbound_fields(session):
    service, listing, task = imported(session)
    binding = service.bindings.active(task.ref_id)
    edit(session, task, title="Local rename")
    observation = changed(service, listing, binding, title="Google rename", notes="Google notes")
    result = service.reconcile(observation)
    assert result["state"] == "conflict" and result["conflict_fields"] == ["title"]
    assert task.title == "Local rename" and task.description == "Google notes"
    conflict = session.scalar(select(TaskSyncConflict))
    assert conflict.canonical_version == task.version
    assert binding.baseline["notes"] == "Google notes"
    assert binding.baseline["title"] == "A" * 1024


def test_deletion_with_pending_local_edits_is_a_conflict(session):
    service, listing, task = imported(session)
    binding = service.bindings.active(task.ref_id)
    edit(session, task, title="Keep this pending local change")
    observation = changed(service, listing, binding, deleted=True)
    result = service.reconcile(observation)
    assert "deletion" in result["conflict_fields"]
    assert task.canonical_status == "active"


def test_confirmed_deletion_retracts_unchanged_task_and_retains_item(session):
    service, listing, task = imported(session)
    binding = service.bindings.active(task.ref_id)
    observation = changed(service, listing, binding, deleted=True)
    service.reconcile(observation)
    assert task.canonical_status == "retracted"
    assert session.scalar(select(Item.canonical_status)) == "active"


def test_detached_identity_never_reimports_a_new_task(session):
    service, listing, task = imported(session)
    binding = service.bindings.active(task.ref_id)
    binding.active, binding.detached = False, True
    TaskBindingService(session).retain(binding, list(task.basis_refs))
    observation = changed(service, listing, binding, title="Detached Google title")
    assert service.reconcile(observation)["state"] == "detached"
    assert session.scalar(select(func.count()).select_from(Task)) == 1
    assert task.title == "A" * 1024


def test_observe_policy_never_imports(session):
    _, _, effect, listing = admitted_import(session)
    listing.policy_mode = "observe"
    observation = session.get(GoogleTaskObservation, effect.observation_ref)
    assert TaskSyncService(session).reconcile(observation)["state"] == "observed"
    assert session.scalar(select(func.count()).select_from(Task)) == 0


def test_inflight_old_read_cannot_undo_newer_binding_observation(session):
    service, listing, task = imported(session)
    old = service.observe(
        listing, GoogleTask("synthetic-id", '"older"', {"title": "Old", "status": "needsAction"}),
        generation=uuid.uuid4(), read_started_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    assert service.reconcile(old)["state"] == "fresh_read_required"
    assert task.title == "A" * 1024
