"""Compile named Task edits into the existing shared provider operation queue."""

import uuid
from copy import deepcopy
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docket.config import Settings, get_settings
from docket.domain.canonical import sha256_json
from docket.models import (
    AuditEvent,
    ChangeSet,
    GoogleTaskList,
    IntentSession,
    Operation,
    OperationTarget,
    Task,
    TaskConflictResolution,
    TaskPolicyEffect,
    TaskProviderBinding,
    TaskSyncConflict,
)
from docket.models.base import utc_now
from docket.providers.google.tasks import TasksProviderError, transport_notes
from docket.schemas.authority import ChangeSetContent
from docket.services.task_bindings import TaskBindingService
from docket.services.task_reconciliation import canonical_values
from docket.services.task_title_labels import task_time_header

_FIELD_NAMES = {
    "title": "title",
    "description": "notes",
    "scheduled_date": "scheduled_date",
    "task_state": "status",
    "completed_at": "completed_at",
    "parent_task_ref": "parent",
    "task_list_ref": "location",
    "after_task_ref": "position",
    "canonical_status": "deletion",
    "remind_at": "title",
    "reminder_timezone": "title",
}
_TERMINAL = {"succeeded", "superseded", "failed"}


class TaskProviderIntentService:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.bindings = TaskBindingService(session)

    def recover_stale(self, binding: TaskProviderBinding, observation_ref: str) -> int:
        """Retry a rejected conditional write only after fresh field reconciliation."""
        from docket.services.operations import DEFAULT_PROVIDER_MAX_ATTEMPTS
        from docket.services.tasks_dispatch import TasksDispatchService

        restored = 0
        for operation, target in self.session.execute(
            select(Operation, OperationTarget)
            .join(OperationTarget)
            .where(
                OperationTarget.canonical_target_ref == binding.task_ref,
                Operation.status == "failed",
                Operation.last_error_code == "google_tasks_stale",
                Operation.attempt_count < DEFAULT_PROVIDER_MAX_ATTEMPTS,
            )
            .with_for_update(of=Operation)
        ):
            if target.parameters.get("binding_ref") != binding.ref_id:
                continue
            admission = TasksDispatchService(self.session, self.settings).prepare(operation, target)
            if admission.disposition != "ready":
                continue
            operation.status = target.status = "pending"
            operation.last_error_code = target.last_error_code = None
            operation.last_error_message = None
            operation.next_attempt_at = target.next_attempt_at = utc_now()
            self.session.add(
                AuditEvent(
                    event_type="operation.requeued_after_task_observation",
                    actor_type="system",
                    actor_id="task_sync",
                    primary_ref=operation.ref_id,
                    affected_refs=[operation.ref_id, binding.task_ref],
                    basis_refs=[observation_ref, operation.originating_changeset_ref],
                    data={"same_named_intent": True},
                )
            )
            restored += 1
        return restored

    def supersede_pending(
        self,
        task_ref: str,
        changeset: ChangeSet,
        *,
        binding_ref: str | None = None,
        fields: set[str] | None = None,
    ) -> list[str]:
        """Only work known not to be in flight can be canceled without reconciliation."""
        affected = []
        for operation, target in self.session.execute(
            select(Operation, OperationTarget)
            .join(OperationTarget)
            .where(
                OperationTarget.canonical_target_ref == task_ref,
                Operation.operation_type.in_(
                    {
                        "tasks_insert",
                        "tasks_patch",
                        "tasks_move",
                        "tasks_delete",
                    }
                ),
                Operation.status == "pending",
            )
            .with_for_update(of=Operation)
        ):
            if binding_ref and target.parameters.get("binding_ref") != binding_ref:
                continue
            projected = set(target.parameters.get("projected_fields", []))
            if fields is not None and not projected.intersection(fields):
                continue
            operation.status = target.status = "superseded"
            operation.lease_token = target.lease_token = None
            operation.leased_until = target.leased_until = None
            operation.next_attempt_at = target.next_attempt_at = None
            operation.last_error_code = target.last_error_code = None
            operation.last_error_message = None
            operation.result = target.result = {
                "disposition": "superseded",
                "superseded_before_next_call": True,
                "by_changeset_ref": changeset.ref_id,
            }
            affected.append(operation.ref_id)
            self.session.add(
                AuditEvent(
                    event_type="operation.superseded",
                    actor_type="system",
                    actor_id="task_projection",
                    primary_ref=operation.ref_id,
                    affected_refs=[operation.ref_id, task_ref],
                    basis_refs=[changeset.ref_id],
                    data={"superseded_before_next_call": True},
                )
            )
        self.session.flush()
        return affected

    def identify_uncertain_creation(
        self,
        binding: TaskProviderBinding,
        observation_ref: str,
        changeset: ChangeSet,
    ) -> list[str]:
        """An explicit native link can park old uncertainty without claiming delivery."""
        affected = []
        for operation, target in self.session.execute(
            select(Operation, OperationTarget)
            .join(OperationTarget)
            .where(
                Operation.operation_type == "tasks_insert",
                Operation.status.in_({"running", "reconciliation_required"}),
                OperationTarget.canonical_target_ref == binding.task_ref,
                OperationTarget.parameters["binding_ref"].as_string() == binding.ref_id,
            )
            .with_for_update(of=Operation)
        ):
            selected = {"changeset_ref": changeset.ref_id, "observation_ref": observation_ref}
            target.recovery = {**(target.recovery or {}), "selected_counterpart": selected}
            if operation.status == "reconciliation_required":
                operation.status = target.status = "superseded"
                operation.next_attempt_at = target.next_attempt_at = None
                operation.result = target.result = {
                    "disposition": "superseded",
                    "provider_outcome": "unresolved",
                    "selected_counterpart": selected,
                }
            affected.append(operation.ref_id)
            self.session.add(
                AuditEvent(
                    event_type="operation.creation_identity_selected",
                    actor_type="system",
                    actor_id="native_task_link",
                    primary_ref=operation.ref_id,
                    affected_refs=[operation.ref_id, binding.ref_id, observation_ref],
                    basis_refs=[changeset.ref_id],
                    data={"original_outcome_preserved": True},
                )
            )
        self.session.flush()
        return affected

    def _binding(
        self,
        task: Task,
        changeset: ChangeSet,
        *,
        publish: bool = False,
    ) -> TaskProviderBinding | None:
        active = self.bindings.active(task.ref_id)
        if (
            active is not None
            and active.provider_snapshot.get("deletion")
            and task.canonical_status == "active"
            and task.task_state != "cancelled"
            and publish
        ):
            self.supersede_pending(task.ref_id, changeset, binding_ref=active.ref_id)
            active.active = False
            self.bindings.retain(active, [changeset.ref_id])
            active = None
        if active is not None and task.task_list_ref is None:
            self.supersede_pending(task.ref_id, changeset, binding_ref=active.ref_id)
            active.active, active.detached = False, True
            self.bindings.retain(active, [changeset.ref_id])
            return None
        if active is not None:
            return active
        if not publish and self.session.scalar(
            select(TaskProviderBinding.ref_id)
            .where(
                TaskProviderBinding.task_ref == task.ref_id,
            )
            .limit(1)
        ):
            return None
        # Detachment is an exclusion, not a request for a new representation.
        previous = self.session.scalar(
            select(TaskProviderBinding)
            .where(
                TaskProviderBinding.task_ref == task.ref_id,
                TaskProviderBinding.detached.is_(True),
            )
            .limit(1)
        )
        if previous is not None or task.task_list_ref is None or task.canonical_status != "active":
            return None
        binding = TaskProviderBinding(
            task_ref=task.ref_id,
            task_list_ref=task.task_list_ref,
            active=True,
            detached=False,
            generation=1,
            canonical_version=task.version,
            correlation=uuid.uuid4().hex,
            created_by_changeset_ref=changeset.ref_id,
            baseline={},
            provider_snapshot={},
            limitations={},
        )
        self.bindings.retain(binding, [changeset.ref_id])
        return binding

    def _open_fields(self, binding: TaskProviderBinding) -> set[str]:
        conflicts = self.session.scalars(
            select(TaskSyncConflict).where(
                TaskSyncConflict.binding_ref == binding.ref_id,
                TaskSyncConflict.observation_ref == binding.observation_ref,
            )
        ).all()
        fields: set[str] = set()
        for conflict in conflicts:
            done = set(
                self.session.scalars(
                    select(TaskConflictResolution.field_name).where(
                        TaskConflictResolution.conflict_ref == conflict.ref_id,
                    )
                )
            )
            fields.update(set(conflict.fields) - done)
        return fields

    def operator_update(
        self,
        binding: TaskProviderBinding,
        task: Task,
        fields: set[str],
        changeset: ChangeSet,
    ) -> None:
        """A fresh, explicit named-field edit resolves only those competing fields."""
        for conflict in self.session.scalars(
            select(TaskSyncConflict).where(
                TaskSyncConflict.binding_ref == binding.ref_id,
                TaskSyncConflict.observation_ref == binding.observation_ref,
            )
        ):
            done = set(
                self.session.scalars(
                    select(TaskConflictResolution.field_name).where(
                        TaskConflictResolution.conflict_ref == conflict.ref_id,
                    )
                )
            )
            for name in sorted((set(conflict.fields) & fields) - done):
                self.session.add(
                    TaskConflictResolution(
                        conflict_ref=conflict.ref_id,
                        field_name=name,
                        selection="operator_update",
                        changeset_ref=changeset.ref_id,
                        canonical_version_after=task.version,
                    )
                )
        self.session.flush()
        baseline = dict(binding.baseline)
        baseline.update(
            {
                key: binding.provider_snapshot[key]
                for key in fields
                if key in binding.provider_snapshot
            }
        )
        if baseline != binding.baseline:
            binding.baseline = baseline
            self.bindings.retain(binding, [changeset.ref_id])

    def _queue(
        self,
        task: Task,
        binding: TaskProviderBinding,
        changeset: ChangeSet,
        kind: str,
        values: dict[str, Any],
        *,
        after: str | None = None,
        carried_by: str | None = None,
    ) -> Operation:
        key = f"{changeset.ref_id}:tasks:{binding.ref_id}:{task.version}:{kind}:" + ",".join(values)
        existing = self.session.scalar(select(Operation).where(Operation.idempotency_key == key))
        if existing is not None:
            return existing
        listing = self.session.get(GoogleTaskList, task.task_list_ref or binding.task_list_ref)
        assert listing is not None
        sequence = 1 + (
            self.session.scalar(
                select(
                    func.max(OperationTarget.parameters["sequence"].as_integer()),
                ).where(
                    OperationTarget.canonical_target_ref == task.ref_id,
                    OperationTarget.parameters["binding_ref"].as_string() == binding.ref_id,
                )
            )
            or 0
        )
        parameters: dict[str, Any] = {
            "target_kind": "google_task",
            "task_ref": task.ref_id,
            "binding_ref": binding.ref_id,
            "generation": binding.generation,
            "task_list_ref": listing.ref_id,
            "canonical_version": task.version,
            "values": deepcopy(values),
            "provider_before": deepcopy(binding.provider_snapshot),
            "projected_fields": sorted(values),
            "title": task.title,
            "correlation": binding.correlation,
            "after_task_ref": after,
            "append_marker": kind == "insert" or binding.marker_present,
            "sequence": sequence,
            "title_prefix": task_time_header(task)[0],
            "subtree_root_ref": carried_by,
        }
        if kind == "insert":
            # An over-limit note stays canonical. Its explicit field limitation
            # does not prevent independent title/date/state projection.
            try:
                transport_notes(task.description or "", binding.correlation)
            except TasksProviderError:
                parameters["projected_fields"] = [
                    field for field in parameters["projected_fields"] if field != "notes"
                ]
                parameters["values"].pop("notes", None)
        operation = Operation(
            originating_changeset_ref=changeset.ref_id,
            basis_refs=[changeset.ref_id],
            canonical_target_refs=[task.ref_id, binding.ref_id],
            idempotency_key=key,
            operation_type="tasks_" + kind,
            account_id=listing.account_id,
            provider_correlation="tasks:" + uuid.uuid4().hex,
        )
        self.session.add(operation)
        self.session.flush()
        self.session.add(
            OperationTarget(
                operation_id=operation.id,
                target_key=task.ref_id,
                canonical_target_ref=task.ref_id,
                target_kind="google_task",
                idempotency_key=key + ":target",
                parameters=parameters,
                parameters_sha256=sha256_json(parameters),
            )
        )
        self.session.flush()
        return operation

    def materialize(
        self,
        changeset: ChangeSet,
        content: ChangeSetContent,
        refs_by_change: dict[str, list[str]],
    ) -> list[Operation]:
        intent = self.session.get(IntentSession, changeset.intent_session_id)
        policy_import = intent is not None and bool(intent.source_policy_effect_ref)
        effect = (
            self.session.get(TaskPolicyEffect, intent.source_policy_effect_ref)
            if intent is not None and intent.source_policy_effect_ref
            else None
        )
        queued: list[Operation] = []
        from docket.services.task_subtrees import child_change

        frames: list[tuple[Any, str, str | None, str | None]] = []
        for original in content.tracked_context_changes:
            refs = [
                ref for ref in refs_by_change.get(original.change_id, []) if ref.startswith("task_")
            ]
            if not refs:
                continue
            group = original.change_id if len(refs) > 1 else None
            derived_frames: list[tuple[Any, str]] = []
            for ref in refs[1:]:
                child = self.session.scalar(select(Task).where(Task.ref_id == ref))
                assert child is not None
                derived_change = child_change(original, child)
                if derived_change is not None:
                    derived_frames.append((derived_change, ref))
            scoped: list[tuple[Any, str]] = [(original, refs[0]), *derived_frames]
            moving = (
                original.action == "update" and "task_list_ref" in original.payload.model_fields_set
            )
            if moving and derived_frames:
                # Google's one parent move carries the subtree. Child operations
                # retain actual destination reads, never issue duplicate moves.
                for frame, ref in scoped:
                    payload = frame.payload.model_dump(exclude_unset=True)
                    movement = {
                        key: payload.pop(key)
                        for key in ("task_list_ref", "parent_task_ref", "after_task_ref")
                        if key in payload
                    }
                    move = frame.model_copy(
                        update={"payload": frame.payload.model_validate(movement)}
                    )
                    frames.append((move, ref, refs[0] if ref != refs[0] else None, group))
                remaining: list[tuple[Any, str]] = []
                for frame, ref in scoped:
                    rest = {
                        key: value
                        for key, value in frame.payload.model_dump(
                            exclude_unset=True,
                        ).items()
                        if key not in {"task_list_ref", "parent_task_ref", "after_task_ref"}
                    }
                    if rest:
                        remaining.append(
                            (
                                frame.model_copy(
                                    update={
                                        "payload": frame.payload.model_validate(rest),
                                    }
                                ),
                                ref,
                            )
                        )
                scoped = remaining
            cascading = original.action == "retract" or (
                original.action == "update"
                and (
                    getattr(original.payload, "task_state", None) in {"completed", "cancelled"}
                    or getattr(original.payload, "canonical_status", None) == "retracted"
                )
            )
            # Only the parent writes Google's current subtree. Descendants retain
            # actual read-back evidence, never independent lifecycle writes.
            frames.extend(
                (frame, ref, refs[0] if cascading and ref != refs[0] else None, group)
                for frame, ref in scoped
            )
        for change, task_ref, carried_by, _group in frames:
            if change.object_type not in {"task", "task_sync_resolution"}:
                continue
            task = self.session.scalar(select(Task).where(Task.ref_id == task_ref))
            assert task is not None
            if policy_import:
                # A confirmed completion retires only the managed display
                # header; imported user fields never produce an echo write.
                binding = self.bindings.active(task.ref_id)
                if (
                    binding is not None
                    and binding.provider_task_id
                    and task.canonical_status == "active"
                    and (
                        (
                            task.remind_at is None
                            and binding.provider_snapshot.get("transport_title_prefix")
                        )
                        or (effect is not None and effect.reminder_generation_ref is not None)
                    )
                ):
                    self.supersede_pending(
                        task.ref_id, changeset, fields={"title"}, binding_ref=binding.ref_id
                    )
                    queued.append(
                        self._queue(task, binding, changeset, "patch", {"title": task.title})
                    )
                continue
            publish = change.action == "create" or (
                change.object_type == "task"
                and change.action == "update"
                and bool(
                    {"task_state", "canonical_status", "task_list_ref"}.intersection(
                        change.payload.model_fields_set,
                    )
                )
            )
            if change.object_type == "task_sync_resolution" and change.payload.choose == "docket":
                conflict = self.session.get(TaskSyncConflict, change.object_ref)
                assert conflict is not None
                publish = "deletion" in (change.payload.fields or conflict.fields)
            binding = self._binding(task, changeset, publish=publish)
            if binding is None:
                continue
            if change.object_type == "task_sync_resolution":
                spec = change.payload
                if spec.choose != "docket":
                    continue
                conflict = self.session.get(TaskSyncConflict, change.object_ref)
                assert conflict is not None
                fields = set(spec.fields or conflict.fields)
                after = None
            else:
                values = change.create_spec if change.action == "create" else change.payload
                fields = (
                    {"deletion"}
                    if change.action == "retract"
                    else {
                        _FIELD_NAMES[key] for key in values.model_fields_set if key in _FIELD_NAMES
                    }
                )
                if fields.intersection({"status", "deletion", "scheduled_date"}):
                    pending_header = self.session.scalar(
                        select(OperationTarget.id)
                        .join(
                            Operation,
                        )
                        .where(
                            OperationTarget.parameters["binding_ref"].as_string() == binding.ref_id,
                            OperationTarget.parameters["title_prefix"].as_string() != "",
                            Operation.status.in_({"pending", "running", "reconciliation_required"}),
                        )
                        .limit(1)
                    )
                    if (
                        pending_header is not None
                        or binding.provider_snapshot.get("transport_title_prefix")
                        or task_time_header(task)[0]
                    ):
                        fields.add("title")
                after = getattr(values, "after_task_ref", None)
                if change.action == "update":
                    # A derived time header does not authorize resolving an
                    # independently conflicting user-authored title.
                    explicit_fields = {
                        _FIELD_NAMES[key]
                        for key in values.model_fields_set
                        if key in _FIELD_NAMES and key not in {"remind_at", "reminder_timezone"}
                    }
                    self.operator_update(binding, task, explicit_fields, changeset)
            desired = canonical_values(task)
            blocked = self._open_fields(binding)
            if task.canonical_status == "retracted" or task.task_state == "cancelled":
                if binding.provider_snapshot.get("deletion"):
                    continue
                self.supersede_pending(task.ref_id, changeset, binding_ref=binding.ref_id)
                in_flight = self.session.scalar(
                    select(Operation)
                    .join(OperationTarget)
                    .where(
                        OperationTarget.canonical_target_ref == task.ref_id,
                        Operation.operation_type == "tasks_insert",
                        Operation.status.in_({"running", "reconciliation_required"}),
                    )
                    .limit(1)
                )
                if binding.provider_task_id is None and in_flight is None:
                    binding.active = False
                    self.bindings.retain(binding, [changeset.ref_id])
                    continue
                queued.append(
                    self._queue(
                        task,
                        binding,
                        changeset,
                        "delete",
                        {"deletion": True},
                        carried_by=carried_by,
                    )
                )
                continue
            limitations = dict(binding.limitations)
            try:
                if binding.marker_present or binding.provider_task_id is None:
                    transport_notes(task.description or "", binding.correlation)
                elif len(task.description or "") > 8192:
                    raise TasksProviderError("google_tasks_notes_limit", "Notes exceed capacity.")
                limitations.pop("notes", None)
            except TasksProviderError:
                limitations["notes"] = "google_transport_capacity"
            if limitations != binding.limitations:
                binding.limitations = limitations
                self.bindings.retain(binding, [changeset.ref_id])
            if binding.provider_task_id is None:
                pending = self.session.scalar(
                    select(Operation)
                    .join(OperationTarget)
                    .where(
                        OperationTarget.canonical_target_ref == task.ref_id,
                        Operation.operation_type == "tasks_insert",
                        Operation.status.not_in(_TERMINAL),
                    )
                    .limit(1)
                )
                if pending is not None and pending.status == "pending" and fields:
                    self.supersede_pending(task.ref_id, changeset, binding_ref=binding.ref_id)
                    pending = None
                if pending is None:
                    if task.task_list_ref and binding.task_list_ref != task.task_list_ref:
                        binding.task_list_ref = task.task_list_ref
                        binding.generation += 1
                        self.bindings.retain(binding, [changeset.ref_id])
                    queued.append(
                        self._queue(
                            task,
                            binding,
                            changeset,
                            "insert",
                            desired,
                            after=after,
                        )
                    )
                    continue
            if "deletion" in blocked:
                continue
            if fields.intersection({"parent", "position", "location"}) and not blocked.intersection(
                {"parent", "location"},
            ):
                queued.append(
                    self._queue(
                        task,
                        binding,
                        changeset,
                        "move",
                        {"parent": task.parent_task_ref, "location": task.task_list_ref},
                        after=after,
                        carried_by=carried_by,
                    )
                )
            if carried_by:
                fields.intersection_update({"status", "deletion"})
            for field_group in (
                {"title"},
                {"notes"},
                {"scheduled_date"},
                {"status", "completed_at"},
            ):
                if (
                    not fields.intersection(field_group)
                    or blocked.intersection(field_group)
                    or set(binding.limitations).intersection(field_group)
                ):
                    continue
                selected = {
                    key: desired[key]
                    for key in sorted(field_group)
                    if not (carried_by and key == "completed_at")
                }
                if all(
                    key in binding.provider_snapshot and binding.provider_snapshot[key] == value
                    for key, value in selected.items()
                ) and (
                    "title" not in selected
                    or binding.provider_snapshot.get(
                        "transport_title_prefix",
                        "",
                    )
                    == task_time_header(task)[0]
                ):
                    continue
                self.supersede_pending(
                    task.ref_id, changeset, binding_ref=binding.ref_id, fields=field_group
                )
                queued.append(
                    self._queue(task, binding, changeset, "patch", selected, carried_by=carried_by)
                )
        scopes: list[set[str]] = []
        for _, _ref, _, group in frames:
            if group:
                scope = {item[1] for item in frames if item[3] == group}
                overlapping = [other for other in scopes if other.intersection(scope)]
                for other in overlapping:
                    scope.update(other)
                    scopes.remove(other)
                scopes.append(scope)
        for scope in scopes:
            predecessor = None
            roots: dict[str, Operation] = {}
            for operation in queued:
                if not scope.intersection(operation.canonical_target_refs):
                    continue
                target = self.session.scalar(
                    select(OperationTarget).where(
                        OperationTarget.operation_id == operation.id,
                    )
                )
                assert target is not None
                root = target.parameters.get("subtree_root_ref")
                dependency = (
                    roots.get(root)
                    if root and operation.operation_type != "tasks_move"
                    else predecessor
                )
                if dependency is not None:
                    operation.predecessor_operation_id = dependency.id
                roots[target.canonical_target_ref] = operation
                predecessor = operation
        return queued
