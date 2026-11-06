"""Task-specific dispatch admission inside the shared OperationRunner transaction."""

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import Settings
from docket.domain.errors import DocketError
from docket.models import GoogleTaskList, Operation, OperationTarget, ProviderAccount, Task
from docket.providers.google.tasks import transport_notes
from docket.services.task_bindings import TaskBindingService
from docket.services.task_policy_authority import TaskPolicyAuthority
from docket.services.task_provider_intents import TaskProviderIntentService
from docket.services.task_reconciliation import canonical_values
from docket.services.tasks_method_admission import TasksMethodAdmission


@dataclass(frozen=True)
class TaskDispatchAdmission:
    disposition: Literal["ready", "wait", "superseded"]
    parameters: dict[str, Any]
    reason: str | None = None


class TasksDispatchService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session, self.settings = session, settings

    def prepare(self, operation: Operation, target: OperationTarget) -> TaskDispatchAdmission:
        from docket.models import TaskProviderBinding

        params = deepcopy(target.parameters)

        def answer(
            disposition: Literal["ready", "wait", "superseded"],
            reason: str,
        ) -> TaskDispatchAdmission:
            return TaskDispatchAdmission(disposition, params, reason)

        binding = self.session.get(TaskProviderBinding, params.get("binding_ref"))
        task = self.session.scalar(select(Task).where(Task.ref_id == target.canonical_target_ref))
        if binding is None or task is None or binding.task_ref != task.ref_id:
            return answer("superseded", "task_projection_unavailable")
        if not binding.active or binding.detached or task.task_list_ref is None:
            return answer("superseded", "task_projection_detached")
        kind = operation.operation_type.removeprefix("tasks_")
        if task.canonical_status == "retracted" and kind != "delete":
            return answer("superseded", "task_retracted_before_dispatch")
        desired = params["values"]
        current = canonical_values(task)
        if kind != "insert" and any(
            key in current and value != current[key] for key, value in desired.items()
        ):
            return answer("superseded", "newer_task_field_intent")
        if kind == "patch" and "title" in desired:
            from docket.services.task_title_labels import task_time_header

            if params.get("title_prefix", "") != task_time_header(task)[0]:
                return answer("superseded", "newer_task_time_header")
        if kind == "move" and desired.get("location") != task.task_list_ref:
            return answer("superseded", "newer_task_location_intent")
        if not self.settings.tasks_enabled:
            return answer("wait", "google_tasks_disabled")
        listing = self.session.get(GoogleTaskList, binding.task_list_ref)
        destination = self.session.get(GoogleTaskList, params["task_list_ref"])
        if listing is None or destination is None:
            return answer("wait", "task_list_unavailable")
        if any(row.policy_mode != "two_way" or not row.available for row in (listing, destination)):
            return answer("wait", "task_list_policy_suspended")
        for selected in {listing.ref_id: listing, destination.ref_id: destination}.values():
            if selected.policy_ref is None:
                return answer("wait", "task_sync_policy_denied")
            try:
                TaskPolicyAuthority(self.session, self.settings).policy(
                    selected.policy_ref, lock=True
                )
            except DocketError:
                return answer("wait", "task_sync_policy_denied")
        account = self.session.get(ProviderAccount, listing.account_id)
        if (
            account is None
            or not account.enabled
            or listing.account_id != destination.account_id
            or account.provider != "google"
            or "google_tasks" not in account.capabilities
            or listing.operator_ref != self.settings.canonical_operator_ref
            or destination.operator_ref != self.settings.canonical_operator_ref
        ):
            return answer("wait", "task_account_unavailable")
        if self.settings.environment.value == "production":
            admitted = TasksMethodAdmission(self.session, self.settings).evidence(account)
            if kind not in admitted:
                return answer("wait", "google_tasks_method_unverified")
            if (
                "scheduled_date" in desired
                and (kind != "insert" or desired["scheduled_date"])
                and not admitted[kind].get("date_write")
            ):
                return answer("wait", "google_tasks_date_write_unverified")
        blocked = TaskProviderIntentService(self.session)._open_fields(binding)
        if set(desired).intersection(blocked) or "deletion" in blocked:
            return answer("wait", "task_field_conflict")
        if kind != "insert" and binding.provider_task_id is None:
            pending = self.session.scalar(
                select(Operation)
                .join(OperationTarget)
                .where(
                    OperationTarget.canonical_target_ref == task.ref_id,
                    Operation.operation_type == "tasks_insert",
                    Operation.status.in_({"pending", "running", "reconciliation_required"}),
                )
                .limit(1)
            )
            if kind == "delete" and pending is None:
                binding.active = False
                TaskBindingService(self.session).retain(binding, [operation.ref_id])
                return answer("superseded", "task_creation_was_not_sent")
            return answer("wait", "task_creation_not_confirmed")
        # Independent fields can proceed while a different field waits for a
        # decision. Delete and move require earlier work on this representation.
        for other, other_target in self.session.execute(
            select(Operation, OperationTarget)
            .join(OperationTarget)
            .where(
                OperationTarget.canonical_target_ref == task.ref_id,
                Operation.id != operation.id,
                Operation.status.in_({"pending", "running", "reconciliation_required"}),
            )
        ):
            if other_target.parameters.get("binding_ref") != binding.ref_id:
                continue
            if other_target.parameters["sequence"] >= params["sequence"]:
                continue
            if (
                kind in {"delete", "move"}
                or other.operation_type in {"tasks_insert", "tasks_move"}
                or set(other_target.parameters.get("values", {})).intersection(desired)
            ):
                return answer("wait", "task_predecessor_not_confirmed")
        if kind != "insert" and not binding.provider_etag:
            return answer("wait", "task_etag_unavailable")
        before = params.get("provider_before", {})
        if kind == "patch" and not before:
            # A patch queued while its creation was in flight has an exact
            # retained creation basis, rather than an invented empty remote base.
            for _, created_target in self.session.execute(
                select(Operation, OperationTarget)
                .join(OperationTarget)
                .where(
                    OperationTarget.canonical_target_ref == task.ref_id,
                    Operation.operation_type == "tasks_insert",
                    Operation.status == "succeeded",
                )
                .order_by(Operation.created_at.desc())
            ):
                if created_target.parameters.get("binding_ref") == binding.ref_id:
                    before = created_target.parameters["values"]
                    break
        if kind == "patch" and any(
            key in before
            and key in binding.provider_snapshot
            and binding.provider_snapshot[key] not in (before[key], value)
            and (
                key not in binding.baseline
                or binding.provider_snapshot[key] != binding.baseline[key]
            )
            for key, value in desired.items()
        ):
            return answer("wait", "task_provider_field_changed")
        parent_ref = desired.get("parent") if kind in {"insert", "move"} else None
        parent_id = None
        previous_id = None
        after_ref = params.get("after_task_ref") if kind in {"insert", "move"} else None
        for ref, role in ((parent_ref, "parent"), (after_ref, "previous")):
            if not ref:
                continue
            linked = TaskBindingService(self.session).active(ref)
            expected_list = destination.ref_id if kind == "move" else listing.ref_id
            if (
                linked is None
                or linked.detached
                or linked.task_list_ref != expected_list
                or not linked.provider_task_id
            ):
                return answer("wait", "task_hierarchy_binding_pending")
            if role == "parent":
                parent_id = linked.provider_task_id
            else:
                previous_id = linked.provider_task_id
        params["dispatch"] = {
            "source_list_id": listing.provider_list_id,
            "destination_list_id": destination.provider_list_id,
            "task_id": binding.provider_task_id,
            "etag": binding.provider_etag,
            "binding_version": binding.version,
            "generation": binding.generation,
            "parent_id": parent_id,
            "previous_id": previous_id,
            "fields": self.fields(params, insert=kind == "insert"),
        }
        return TaskDispatchAdmission("ready", params)

    @staticmethod
    def fields(params: dict[str, Any], *, insert: bool) -> dict[str, Any]:
        values = params["values"]
        result = {
            key: value for key, value in values.items() if key in {"title", "notes", "status"}
        }
        if "title" in result:
            result["title"] = params.get("title_prefix", "") + result["title"]
        if "notes" in result and params.get("append_marker"):
            result["notes"] = transport_notes(result["notes"], params["correlation"])
        elif insert:
            result["notes"] = transport_notes("", params["correlation"])
        if "scheduled_date" in values and (not insert or values["scheduled_date"]):
            day = values["scheduled_date"]
            result["due"] = day + "T00:00:00Z" if day else None
        if "completed_at" in values and (not insert or values["completed_at"]):
            result["completed"] = values["completed_at"]
        return result
