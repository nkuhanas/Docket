"""Task transport and receipts delegated by the shared OperationRunner.

Claims, attempts, lease recovery and worker scheduling remain in OperationRunner.
Each reconciliation step performs at most one provider read/page.
"""

from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.canonical import sha256_json
from docket.models import (
    ExecutionAttempt,
    GoogleTaskList,
    Operation,
    OperationTarget,
    ProviderAccount,
    Task,
    TaskProviderBinding,
)
from docket.models.base import utc_now
from docket.providers.google.factory import build_tasks_provider
from docket.providers.google.tasks import (
    GoogleTask,
    TasksProvider,
    TasksProviderError,
    canonical_notes,
)
from docket.services.task_bindings import TaskBindingService
from docket.services.task_item_titles import lock_task_rows
from docket.services.task_reconciliation import observed_values, transport_fields_match
from docket.services.task_sync import TaskSyncService
from docket.services.tasks_dispatch import TasksDispatchService
from docket.services.tasks_method_admission import TasksMethodAdmission

if TYPE_CHECKING:
    from docket.services.operations import ClaimedOperation, OperationRunner


class TasksExecutionService:
    def __init__(self, runner: OperationRunner) -> None:
        self.runner = runner

    def _selected_counterpart(
        self,
        session: Session,
        rows: tuple[Operation, OperationTarget, ExecutionAttempt],
        *,
        actual_observation_ref: str | None = None,
    ) -> bool:
        operation, target, attempt = rows
        selected = (target.recovery or {}).get("selected_counterpart")
        if not selected:
            return False
        operation.status = target.status = "superseded"
        operation.next_attempt_at = target.next_attempt_at = None
        operation.result = target.result = {
            "disposition": "superseded",
            "provider_outcome": "resource_observed" if actual_observation_ref else "unresolved",
            "selected_counterpart": selected,
            "actual_observation_ref": actual_observation_ref,
        }
        attempt.status = "succeeded" if actual_observation_ref else "unknown"
        attempt.response_summary = dict(operation.result)
        attempt.completed_at = utc_now()
        self.runner._clear_lease(operation, target)
        self.runner._audit(session, operation, target, "operation.creation_identity_selected")
        return True

    def _prepare(self, claim: ClaimedOperation) -> tuple[ClaimedOperation, TasksProvider] | None:
        with self.runner.session_factory.begin() as session:
            lock_task_rows(session)
            rows = self.runner._owned_claim(session, claim)
            if rows is None:
                return None
            operation, target, attempt = rows
            admission = TasksDispatchService(session, self.runner.settings).prepare(
                operation,
                target,
            )
            if admission.disposition != "ready":
                superseded = admission.disposition == "superseded"
                operation.status = target.status = "superseded" if superseded else "pending"
                operation.last_error_code = target.last_error_code = admission.reason
                operation.next_attempt_at = target.next_attempt_at = (
                    None if superseded else utc_now() + timedelta(seconds=30)
                )
                attempt.status = "succeeded" if superseded else "failed"
                attempt.error_code = None if superseded else admission.reason
                attempt.response_summary = {
                    "disposition": admission.disposition,
                    "provider_call": False,
                }
                attempt.completed_at = utc_now()
                if superseded:
                    operation.result = target.result = {
                        "disposition": "superseded",
                        "superseded_before_next_call": True,
                    }
                self.runner._clear_lease(operation, target)
                self.runner._audit(
                    session,
                    operation,
                    target,
                    "operation.superseded" if superseded else "operation.waiting",
                )
                return None
            parameters = admission.parameters
            parameters["dispatch"]["started_at"] = utc_now().isoformat()
            target.parameters = parameters
            target.parameters_sha256 = sha256_json(parameters)
            attempt.request_summary = {
                **attempt.request_summary,
                "parameters_sha256": target.parameters_sha256,
            }
            provider = self.runner.tasks_provider
            if self.runner.settings.environment.value == "production":
                account = session.get(ProviderAccount, operation.account_id)
                assert account is not None
                provider = build_tasks_provider(
                    self.runner.settings,
                    gate=TasksMethodAdmission(session, self.runner.settings).gate(account),
                )
            if provider is None:
                raise TasksProviderError("google_tasks_disabled", "Google Tasks is unavailable.")
            # This is the dispatch admission boundary. Subsequent lease recovery
            # treats interruption as uncertain, rather than repeating an insert.
            attempt.provider_request_id = "call-started:" + str(claim.lease_token)
            return replace(claim, parameters=parameters), provider

    def execute(self, claim: ClaimedOperation) -> None:
        prepared = self._prepare(claim)
        if prepared is None:
            return
        claim, provider = prepared
        call = claim.parameters["dispatch"]
        kind = claim.operation_type.removeprefix("tasks_")
        if kind == "insert":
            result = provider.insert_task(
                call["source_list_id"],
                call["fields"],
                parent=call["parent_id"],
                previous=call["previous_id"],
            )
        elif kind == "patch":
            result = provider.patch_task(
                call["source_list_id"],
                call["task_id"],
                call["fields"],
                etag=call["etag"],
            )
        elif kind == "move":
            result = provider.move_task(
                call["source_list_id"],
                call["task_id"],
                etag=call["etag"],
                destination_task_list_id=(
                    call["destination_list_id"]
                    if call["destination_list_id"] != call["source_list_id"]
                    else None
                ),
                parent=call["parent_id"],
                previous=call["previous_id"],
            )
        else:
            assert kind == "delete"
            provider.delete_task(call["source_list_id"], call["task_id"], etag=call["etag"])
            result = None
        self.success(claim, result)

    def success(self, claim: ClaimedOperation, result: GoogleTask | None) -> None:
        with self.runner.session_factory.begin() as session:
            lock_task_rows(session, [claim.parameters["task_ref"]])
            rows = self.runner._owned_claim(session, claim)
            if rows is None:
                return
            operation, target, attempt = rows
            params, call = claim.parameters, claim.parameters["dispatch"]
            binding = session.get(TaskProviderBinding, params["binding_ref"])
            task = session.scalar(select(Task).where(Task.ref_id == params["task_ref"]))
            assert binding is not None and task is not None
            matched = True
            basis = [operation.ref_id, operation.originating_changeset_ref]
            if result is None:
                # HTTP deletion acknowledgement is evidence of the effect, not a
                # fabricated provider Task resource or a synthetic poll response.
                binding.provider_snapshot = {**binding.provider_snapshot, "deletion": True}
                binding.baseline = {**binding.baseline, "deletion": True}
                binding.observation_ref, binding.provider_etag = None, None
                if task.canonical_status == "retracted":
                    binding.active = False
                elif binding.active:
                    binding.limitations = {
                        **binding.limitations,
                        "deletion": "google_representation_deleted",
                    }
            else:
                list_ref = (
                    params["task_list_ref"]
                    if operation.operation_type == "tasks_move"
                    else binding.task_list_ref
                )
                listing = session.get(GoogleTaskList, list_ref)
                assert listing is not None
                observation = TaskSyncService(session, self.runner.settings).observe(
                    listing,
                    result,
                    generation=uuid.uuid4(),
                    read_started_at=datetime.fromisoformat(call["started_at"]),
                )
                if self._selected_counterpart(
                    session,
                    rows,
                    actual_observation_ref=observation.ref_id,
                ):
                    return
                started = datetime.fromisoformat(call["started_at"])
                if (
                    binding.observed_at is not None
                    and binding.observed_at.replace(tzinfo=binding.observed_at.tzinfo or UTC)
                    > started
                    and binding.provider_etag != result.etag
                ):
                    # Keep the actual response without rolling the binding back
                    # over a newer observed version or explicit field decision.
                    matched = self.matches(result, params)
                    operation.status = target.status = (
                        "succeeded" if matched else "reconciliation_required"
                    )
                    operation.result = target.result = {
                        "observation_ref": observation.ref_id,
                        "preserved_newer_binding": True,
                    }
                    operation.next_attempt_at = target.next_attempt_at = (
                        None if matched else utc_now() + timedelta(seconds=30)
                    )
                    attempt.status = "succeeded" if matched else "unknown"
                    attempt.response_summary = dict(operation.result)
                    attempt.completed_at = utc_now()
                    self.runner._clear_lease(operation, target)
                    self.runner._audit(
                        session, operation, target, "operation.old_response_retained"
                    )
                    return
                values, marker = observed_values(
                    session, observation, binding,
                    title_prefix=(params.get("title_prefix", "")
                                  if "title" in params["projected_fields"] else None),
                )
                existing = TaskBindingService(session).identity(list_ref, result.task_id)
                baseline = deepcopy(binding.baseline)
                for field in params["projected_fields"]:
                    if field in values and values[field] == params["values"].get(field):
                        baseline[field] = values[field]
                    elif field not in {"location", "position"}:
                        matched = False
                if existing is None or existing.ref_id == binding.ref_id:
                    if binding.task_list_ref != list_ref or (
                        binding.provider_task_id is not None
                        and binding.provider_task_id != result.task_id
                    ):
                        binding.generation += 1
                    binding.provider_task_id, binding.task_list_ref = result.task_id, list_ref
                binding.provider_snapshot, binding.baseline = values, baseline
                binding.provider_etag, binding.marker_present = result.etag, marker
                binding.observation_ref = observation.ref_id
                basis.append(observation.ref_id)
            binding.observed_at = utc_now()
            binding.canonical_version = task.version
            TaskBindingService(session).retain(binding, basis)
            operation.status = target.status = "succeeded" if matched else "reconciliation_required"
            operation.result = target.result = {
                "binding_ref": binding.ref_id,
                "observation_ref": binding.observation_ref,
                "matched_requested_fields": matched,
            }
            operation.last_error_code = target.last_error_code = (
                None if matched else "google_tasks_result_mismatch"
            )
            operation.last_error_message = None
            operation.next_attempt_at = target.next_attempt_at = (
                None if matched else utc_now() + timedelta(seconds=30)
            )
            attempt.status = "succeeded" if matched else "unknown"
            attempt.response_summary = dict(operation.result)
            attempt.completed_at = utc_now()
            self.runner._clear_lease(operation, target)
            self.runner._audit(
                session,
                operation,
                target,
                "operation.succeeded" if matched else "operation.result_mismatch",
            )

    @staticmethod
    def matches(task: GoogleTask, parameters: dict[str, Any]) -> bool:
        if not transport_fields_match(
            task.fields, parameters["dispatch"]["fields"], parameters["correlation"]
        ):
            return False
        if "parent" in parameters.get("values", {}) and parameters["dispatch"].get(
            "parent_id"
        ) != task.fields.get("parent"):
            return False
        if parameters.get("after_task_ref"):
            # An opaque position cannot establish the requested sibling relation.
            # A lost move acknowledgement remains uncertain until repaired.
            return False
        return not task.fields.get("deleted")

    def _read_provider(self) -> TasksProvider:
        provider = self.runner.tasks_provider
        if provider is None:
            raise TasksProviderError("google_tasks_disabled", "Google Tasks is unavailable.")
        return provider

    def _checkpoint(
        self,
        claim: ClaimedOperation,
        recovery: dict[str, Any],
        code: str,
    ) -> None:
        with self.runner.session_factory.begin() as session:
            rows = self.runner._owned_claim(session, claim)
            if rows is None:
                return
            if self._selected_counterpart(session, rows):
                return
            operation, target, attempt = rows
            target.recovery = recovery
            operation.status = target.status = "reconciliation_required"
            operation.last_error_code = target.last_error_code = code
            operation.next_attempt_at = target.next_attempt_at = utc_now() + timedelta(seconds=1)
            attempt.status, attempt.completed_at = "unknown", utc_now()
            attempt.response_summary = {"next_step": code}
            self.runner._clear_lease(operation, target)

    def reconcile(self, claim: ClaimedOperation) -> None:
        call = claim.parameters.get("dispatch")
        if not isinstance(call, dict):
            # A claim interrupted before dispatch admission made no provider call.
            self.runner._finish_error(
                claim,
                TasksProviderError(
                    "google_tasks_dispatch_missing", "The task delivery request was not sent."
                ),
            )
            return
        provider = self._read_provider()
        self.runner.mark_provider_call_started(claim)
        kind = claim.operation_type.removeprefix("tasks_")
        with self.runner.session_factory() as session:
            from docket.models import OperationTarget

            target = session.get(OperationTarget, claim.operation_target_id)
            recovery = deepcopy(target.recovery or {}) if target else {}
        if recovery.get("selected_counterpart"):
            with self.runner.session_factory.begin() as session:
                rows = self.runner._owned_claim(session, claim)
                if rows is not None:
                    self._selected_counterpart(session, rows)
            return
        if kind == "insert":
            page = provider.list_tasks(call["source_list_id"], page_token=recovery.get("page"))
            matches = list(recovery.get("matches", []))
            for task in page.tasks:
                _, marker = canonical_notes(
                    task.fields.get("notes") or "", claim.parameters["correlation"]
                )
                if marker and self.matches(task, claim.parameters):
                    matches.append({"id": task.task_id, "etag": task.etag, "fields": task.fields})
                    matches = matches[:2]
            if page.next_page_token is None and len(matches) == 1:
                match = matches[0]
                self.success(claim, GoogleTask(match["id"], match["etag"], match["fields"]))
                return
            with self.runner.session_factory.begin() as session:
                rows = self.runner._owned_claim(session, claim)
                if rows is None:
                    return
                if self._selected_counterpart(session, rows):
                    return
                operation, target, attempt = rows
                target.recovery = (
                    {"page": page.next_page_token, "matches": matches}
                    if page.next_page_token
                    else None
                )
                operation.status = target.status = "reconciliation_required"
                operation.last_error_code = target.last_error_code = (
                    "google_tasks_recovery_paging"
                    if page.next_page_token
                    else "google_tasks_creation_uncertain"
                )
                operation.next_attempt_at = target.next_attempt_at = utc_now() + timedelta(
                    seconds=1 if page.next_page_token else 300,
                )
                attempt.status, attempt.completed_at = "unknown", utc_now()
                attempt.response_summary = {
                    "match_count": len(matches),
                    "complete": page.next_page_token is None,
                }
                self.runner._clear_lease(operation, target)
            return
        list_id = call["destination_list_id"] if kind == "move" else call["source_list_id"]
        if kind == "delete" and recovery.get("missing_task"):
            # A task GET 404 also occurs when its collection is inaccessible.
            # Positively verify the same collection before confirming absence.
            page = provider.list_tasks(list_id)
            if any(
                task.task_id == call["task_id"] and not task.fields.get("deleted")
                for task in page.tasks
            ):
                self._checkpoint(claim, {}, "google_tasks_outcome_changed")
            else:
                self.success(claim, None)
            return
        result = provider.get_task(list_id, call["task_id"])
        if kind == "delete" and result is None:
            self._checkpoint(
                claim,
                {"missing_task": True, "observed_at": utc_now().isoformat()},
                "google_tasks_verify_collection",
            )
        elif kind == "delete" and result is not None and result.fields.get("deleted"):
            self.success(claim, None)
        elif result is not None and self.matches(result, claim.parameters):
            self.success(claim, result)
        else:
            self.runner._finish_error(
                claim,
                TasksProviderError(
                    "google_tasks_outcome_changed", "The task outcome needs field reconciliation."
                ),
                reconciling=True,
            )
