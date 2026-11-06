"""Explicit linking, detachment and per-field reconciliation choices."""

import uuid
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.domain.errors import DocketError
from docket.models import (
    AuditEvent,
    ChangeSet,
    GoogleTaskObservation,
    Task,
    TaskConflictResolution,
    TaskProviderBinding,
    TaskSyncConflict,
)
from docket.models.base import utc_now
from docket.schemas.authority import TaskModify, TaskRetract
from docket.schemas.native_tasks import TaskGoogleLinkSpec, TaskSyncResolutionSpec
from docket.services.task_bindings import TaskBindingService
from docket.services.task_lists import TaskListService
from docket.services.task_reconciliation import canonical_values, observed_values, task_patch
from docket.services.task_sync import TaskSyncService
from docket.services.tracked_context import TrackedContextService


class TaskLinkService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.bindings = TaskBindingService(session)

    def _task(self, ref: str) -> Task:
        task = self.session.scalar(select(Task).where(Task.ref_id == ref))
        if task is None:
            raise DocketError(code="task_not_found", message="Read the selected task first.")
        return task

    def _latest_observation(self, observation: GoogleTaskObservation) -> None:
        latest = self.session.scalar(select(GoogleTaskObservation.ref_id).where(
            GoogleTaskObservation.task_list_ref == observation.task_list_ref,
            GoogleTaskObservation.provider_task_id == observation.provider_task_id,
        ).order_by(GoogleTaskObservation.observed_at.desc(),
                   GoogleTaskObservation.ref_id.desc()).limit(1))
        if latest != observation.ref_id:
            raise DocketError(
                code="task_observation_changed", message="Read the latest observed task version."
            )

    def _observed_versions(
        self, changeset: ChangeSet, task: Task, binding: TaskProviderBinding | None,
    ) -> None:
        versions = changeset.expected_versions
        if versions.get(task.ref_id) != task.version:
            raise DocketError(code="version_conflict", message="Read the task context again.")
        if binding is not None:
            self.bindings.require_version(binding, versions.get(binding.ref_id))

    def _edit(
        self, changeset: ChangeSet, change: Any, task: Task, values: dict[str, Any],
    ) -> list[str]:
        mutation: TaskModify | TaskRetract
        if values.get("deletion"):
            mutation = TaskRetract.model_validate({
                "object_type": "task", "action": "retract", "object_ref": task.ref_id,
                "change_id": change.change_id + ".task", "basis_refs": list(change.basis_refs),
                "affected_fields": ["canonical_status"],
            })
        else:
            patch = task_patch(values, task)
            if not patch:
                return []
            mutation = TaskModify.model_validate({
                "object_type": "task", "action": "update", "object_ref": task.ref_id,
                "change_id": change.change_id + ".task", "basis_refs": list(change.basis_refs),
                "affected_fields": list(patch), "payload": patch,
            })
        return TrackedContextService(self.session).apply_task(self.session, changeset, mutation)

    def _audit(self, changeset: ChangeSet, change: Any, refs: list[str], event: str) -> None:
        principal = require_principal("commit")
        self.session.add(AuditEvent(
            event_type=event, primary_ref=refs[0], affected_refs=refs,
            basis_refs=list(change.basis_refs), actor_type="agent",
            actor_id=principal.principal_ref,
            data={"created_by_changeset_ref": changeset.ref_id},
        ))

    def link(self, _session: Session, changeset: ChangeSet, change: Any) -> list[str]:
        require_principal("commit")
        spec = TaskGoogleLinkSpec.model_validate(change.payload)
        task = self._task(change.object_ref)
        previous = self.bindings.active(task.ref_id)
        self._observed_versions(changeset, task, previous)
        if spec.detach:
            if previous is None:
                raise DocketError(code="task_not_linked", message="This task has no active link.")
            previous.active, previous.detached = False, True
            self.bindings.retain(previous, list(change.basis_refs))
            from docket.services.task_provider_intents import TaskProviderIntentService

            TaskProviderIntentService(self.session).supersede_pending(
                task.ref_id, changeset, binding_ref=previous.ref_id,
            )
            refs = [task.ref_id, previous.ref_id]
            self._audit(changeset, change, refs, "task.google_detached")
            return refs
        observation = self.session.get(GoogleTaskObservation, spec.observation_ref)
        if observation is None:
            raise DocketError(code="task_observation_missing", message="Read the observed task.")
        self._latest_observation(observation)
        listing = TaskListService(self.session).owned(observation.task_list_ref)
        if listing.policy_mode != "two_way" or not listing.available:
            raise DocketError(
                code="task_list_not_two_way", message="Enable two-way synchronization first."
            )
        current = self.bindings.identity(listing.ref_id, observation.provider_task_id)
        if (current is None and previous is not None and previous.provider_task_id is None
                and previous.task_list_ref == listing.ref_id):
            # This is an explicit authenticated counterpart selection, not a
            # heuristic match or an automatic retry of the uncertain creation.
            current = previous
        if current is not None and current.task_ref != task.ref_id:
            raise DocketError(
                code="google_task_already_linked", message="That representation has another task."
            )
        if current is not None and current is not previous:
            self.bindings.require_version(current, changeset.expected_versions.get(current.ref_id))
        if observation.fields.get("deleted") or observation.fields.get("assignmentInfo"):
            raise DocketError(
                code="google_task_not_linkable", message="Choose an editable, existing task."
            )
        if previous is not None and previous is not current:
            previous.active, previous.detached = False, True
            self.bindings.retain(previous, list(change.basis_refs))
            from docket.services.task_provider_intents import TaskProviderIntentService

            TaskProviderIntentService(self.session).supersede_pending(
                task.ref_id, changeset, binding_ref=previous.ref_id,
            )
        # Selecting a counterpart does not silently adopt its differing content.
        if task.task_list_ref != listing.ref_id:
            mutation = TaskModify.model_validate({
                "object_type": "task", "action": "update", "object_ref": task.ref_id,
                "change_id": change.change_id + ".list", "basis_refs": list(change.basis_refs),
                "affected_fields": ["task_list_ref"],
                "payload": {"task_list_ref": listing.ref_id},
            })
            TrackedContextService(self.session).apply_task(self.session, changeset, mutation)
        binding = current or TaskProviderBinding(
            task_ref=task.ref_id, task_list_ref=listing.ref_id,
            provider_task_id=observation.provider_task_id, canonical_version=task.version,
            correlation=uuid.uuid4().hex, created_by_changeset_ref=changeset.ref_id,
            generation=1, active=True, detached=False, baseline={}, provider_snapshot={},
            limitations={},
        )
        google, marker = observed_values(self.session, observation, binding)
        local = canonical_values(task)
        differing = [key for key in local if local.get(key) != google.get(key)]
        binding.active, binding.detached = True, False
        binding.provider_task_id = observation.provider_task_id
        binding.canonical_version = task.version
        binding.baseline = {key: value for key, value in local.items() if key not in differing}
        binding.provider_snapshot = deepcopy(google)
        binding.provider_etag = observation.provider_etag
        binding.observation_ref = observation.ref_id
        binding.observed_at = utc_now()
        binding.marker_present = marker
        self.bindings.retain(binding, list(change.basis_refs))
        refs = [task.ref_id, binding.ref_id]
        from docket.services.task_provider_intents import TaskProviderIntentService

        refs.extend(TaskProviderIntentService(self.session).identify_uncertain_creation(
            binding, observation.ref_id, changeset,
        ))
        if differing:
            conflict = TaskSyncService(self.session).conflict(
                binding, observation, task, differing, google,
            )
            refs.append(conflict.ref_id)
        self._audit(changeset, change, refs, "task.google_linked")
        return refs

    def resolve(self, _session: Session, changeset: ChangeSet, change: Any) -> list[str]:
        require_principal("commit")
        spec = TaskSyncResolutionSpec.model_validate(change.payload)
        conflict = self.session.get(TaskSyncConflict, change.object_ref)
        binding = self.session.get(TaskProviderBinding, conflict.binding_ref) if conflict else None
        if conflict is None or binding is None or not binding.active or binding.detached:
            raise DocketError(
                code="task_conflict_unavailable", message="Read current task context."
            )
        TaskListService(self.session).owned(binding.task_list_ref)
        observation = self.session.get(GoogleTaskObservation, conflict.observation_ref)
        assert observation is not None
        self._latest_observation(observation)
        task = self._task(binding.task_ref)
        self._observed_versions(changeset, task, binding)
        resolved_versions = set(self.session.scalars(
            select(TaskConflictResolution.canonical_version_after).where(
                TaskConflictResolution.conflict_ref == conflict.ref_id,
            )
        ))
        if (task.version not in {conflict.canonical_version, *resolved_versions}
                or binding.observation_ref != conflict.observation_ref):
            raise DocketError(
                code="task_conflict_changed", message="Refresh and read the current conflict."
            )
        done = set(self.session.scalars(select(TaskConflictResolution.field_name).where(
            TaskConflictResolution.conflict_ref == conflict.ref_id,
        )))
        selected = set(spec.fields or conflict.fields) - done
        if not selected or not selected.issubset(set(conflict.fields)):
            raise DocketError(
                code="task_conflict_fields_invalid", message="Choose unresolved fields."
            )
        values = {key: conflict.google_values.get(key) for key in selected}
        if spec.choose == "google":
            if values.get("status") == "completed":
                if "completed_at" in conflict.fields and "completed_at" not in selected:
                    raise DocketError(
                        code="task_completion_fields_required",
                        message="Resolve completion state and completion time together.",
                    )
                values["completed_at"] = conflict.google_values.get("completed_at")
            self._edit(changeset, change, task, values)
        baseline = dict(binding.baseline)
        baseline.update({key: conflict.google_values.get(key) for key in selected})
        binding.baseline = baseline
        binding.canonical_version = task.version
        self.bindings.retain(binding, list(change.basis_refs))
        for name in sorted(selected):
            self.session.add(TaskConflictResolution(
                conflict_ref=conflict.ref_id, field_name=name, changeset_ref=changeset.ref_id,
                selection=spec.choose, canonical_version_after=task.version,
            ))
        refs = [task.ref_id, binding.ref_id, conflict.ref_id]
        self._audit(changeset, change, refs, "task.sync_conflict_resolved")
        return refs
