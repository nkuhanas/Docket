"""Retain provider observations and reconcile them under bounded list policy.

This service owns no transport retry loop. Its caller supplies an observation,
and commits canonical changes, baselines and conflicts in the same transaction.
"""

import uuid
from contextlib import suppress
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import Settings, get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.models import (
    ChangeSet,
    GoogleTaskList,
    GoogleTaskObservation,
    IntentSession,
    Task,
    TaskPolicyEffect,
    TaskProviderBinding,
    TaskSyncConflict,
)
from docket.models.base import utc_now
from docket.providers.google.tasks import GoogleTask
from docket.schemas.authority import ChangeSetCommit, ChangeSetContent, ChangeSetPrepare
from docket.services.change_sets import _content_payload
from docket.services.task_bindings import TaskBindingService
from docket.services.task_item_titles import lock_task_rows
from docket.services.task_policy_authority import PLANNER_VERSION, TaskPolicyAuthority
from docket.services.task_reconciliation import (
    canonical_values,
    instant,
    merge_fields,
    observed_values,
    task_patch,
)


class TaskSyncService:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session, self.settings = session, settings or get_settings()
        self.bindings = TaskBindingService(session)

    def observe(
        self,
        listing: GoogleTaskList,
        task: GoogleTask,
        *,
        generation: uuid.UUID,
        read_started_at: datetime,
        read_after_binding_version: int | None = None,
    ) -> GoogleTaskObservation:
        # Identical ordinary polls share evidence. A forced fresh read can carry
        # an explicit binding-version fence after an overlapping delivery.
        source_hash = sha256_json({
            "etag": task.etag,
            "fields": task.fields,
            "read_after_binding_version": read_after_binding_version,
        })
        existing = self.session.scalar(select(GoogleTaskObservation).where(
            GoogleTaskObservation.task_list_ref == listing.ref_id,
            GoogleTaskObservation.provider_task_id == task.task_id,
            GoogleTaskObservation.source_hash == source_hash,
        ))
        if existing is not None:
            binding = self.bindings.identity(listing.ref_id, task.task_id)
            previous_at = binding.observed_at if binding is not None else None
            if (binding is None or previous_at is None or binding.provider_etag == task.etag
                    or read_started_at.replace(tzinfo=read_started_at.tzinfo or UTC)
                    < previous_at.replace(tzinfo=previous_at.tzinfo or UTC)):
                return existing
            # This is a genuinely fresh read of an older provider version after a
            # newer delivery, rather than reuse of its original evidence time.
            return self.observe(
                listing, task, generation=generation, read_started_at=read_started_at,
                read_after_binding_version=binding.version,
            ) if read_after_binding_version is None else existing
        row = GoogleTaskObservation(
            task_list_ref=listing.ref_id,
            provider_task_id=task.task_id,
            provider_etag=task.etag,
            source_hash=source_hash,
            fields=deepcopy(task.fields),
            inventory_generation=generation,
            read_started_at=read_started_at,
            read_after_binding_version=read_after_binding_version,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def conflict(
        self,
        binding: TaskProviderBinding,
        observation: GoogleTaskObservation,
        task: Task,
        fields: list[str],
        google: dict[str, Any],
    ) -> TaskSyncConflict:
        source_hash = sha256_json({
            "observation": observation.ref_id,
            "canonical_version": task.version,
            "baseline": binding.baseline,
            "fields": sorted(fields),
        })
        row = self.session.scalar(select(TaskSyncConflict).where(
            TaskSyncConflict.binding_ref == binding.ref_id,
            TaskSyncConflict.source_hash == source_hash,
        ))
        if row is None:
            row = TaskSyncConflict(
                binding_ref=binding.ref_id, observation_ref=observation.ref_id,
                source_hash=source_hash, canonical_version=task.version,
                baseline=deepcopy(binding.baseline), local_values=canonical_values(task),
                google_values=deepcopy(google), fields=sorted(fields),
            )
            self.session.add(row)
            self.session.flush()
        return row

    def _apply(
        self,
        listing: GoogleTaskList,
        observation: GoogleTaskObservation,
        action: str,
        values: dict[str, Any],
        *,
        task: Task | None = None,
        binding: TaskProviderBinding | None = None,
    ) -> tuple[Task, ChangeSet]:
        from docket.services.interactive_authority import InteractiveAuthorityService

        assert listing.policy_ref is not None
        effect_ref = new_public_ref("teff")
        basis = [effect_ref, listing.policy_ref, observation.ref_id]
        mutation: dict[str, Any] = {
            "mutation_type": {"create": "task_create", "update": "task_modify",
                              "retract": "task_retract"}[action],
            "change_id": "observed-task", "action": action, "object_type": "task",
            "affected_fields": list(values) if action != "retract" else ["canonical_status"],
            "basis_refs": basis,
        }
        if task is not None:
            mutation["object_ref"] = task.ref_id
        if action != "retract":
            mutation["create_spec" if action == "create" else "payload"] = values
        content = ChangeSetContent.model_validate({
            "basis_refs": basis, "tracked_context_changes": [mutation],
            "expected_versions": {task.ref_id: task.version} if task else {},
        })
        payload = _content_payload(content)
        effect = TaskPolicyEffect(
            ref_id=effect_ref, policy_ref=listing.policy_ref, observation_ref=observation.ref_id,
            effect_hash=sha256_json(payload), content=payload, planner_version=PLANNER_VERSION,
            expected_binding_version=binding.version if binding else None,
        )
        self.session.add(effect)
        self.session.flush()
        intent = IntentSession(
            conversation_ref="task-sync:" + listing.ref_id,
            source_policy_effect_ref=effect.ref_id, semantic_state="ready",
        )
        self.session.add(intent)
        self.session.flush()
        engine = InteractiveAuthorityService(self.session).changesets
        prepared, _ = engine.prepare(ChangeSetPrepare(
            intent_session_ref=intent.ref_id, expected_session_version=intent.version,
            idempotency_key="task-sync:" + effect.ref_id, content=content,
        ))
        if prepared.state != "validated":
            raise DocketError(
                code="task_import_validation_failed",
                message="The observed task could not be committed; its evidence is retained.",
            )
        committed, receipt = engine.commit(ChangeSetCommit(
            changeset_ref=prepared.ref_id, expected_version=prepared.version,
            idempotency_key=prepared.idempotency_key, authority_policy_effect_ref=effect.ref_id,
        ))
        if task is None:
            ref = next(ref for ref in receipt.affected_refs if ref.startswith("task_"))
            task = self.session.scalar(select(Task).where(Task.ref_id == ref))
        assert task is not None
        return task, committed

    def reconcile(self, observation: GoogleTaskObservation) -> dict[str, Any]:
        listing = self.session.get(GoogleTaskList, observation.task_list_ref)
        if listing is None or listing.operator_ref != self.settings.canonical_operator_ref:
            raise TaskPolicyAuthority.denied()
        binding = self.bindings.identity(listing.ref_id, observation.provider_task_id)
        if binding is None:
            binding = self.bindings.previous_identity(listing.ref_id, observation.provider_task_id)
            if binding is not None:
                return {"state": "previous_identity", "observation_ref": observation.ref_id}
        lock_task_rows(self.session, [binding.task_ref] if binding else [], lists=[listing.ref_id])
        # A concurrent move/detachment/import may have completed while waiting
        # for the graph lock. Never attach an old list observation to its new head.
        binding = self.bindings.identity(listing.ref_id, observation.provider_task_id)
        if binding is None and self.bindings.previous_identity(
            listing.ref_id, observation.provider_task_id,
        ) is not None:
            return {"state": "previous_identity", "observation_ref": observation.ref_id}
        if listing.policy_mode != "two_way":
            return {"state": "observed", "observation_ref": observation.ref_id}
        if binding is not None and (binding.detached or not binding.active):
            return {"state": "detached", "observation_ref": observation.ref_id}
        assert listing.policy_ref is not None
        TaskPolicyAuthority(self.session, self.settings).policy(listing.policy_ref, lock=True)
        if observation.fields.get("assignmentInfo"):
            return {"state": "assigned_read_only", "observation_ref": observation.ref_id}
        if binding is not None:
            previous_at = binding.observed_at
            started = observation.read_started_at or observation.observed_at
            if (previous_at is not None
                    and started.replace(tzinfo=started.tzinfo or UTC)
                    < previous_at.replace(tzinfo=previous_at.tzinfo or UTC)
                    and observation.provider_etag != binding.provider_etag
                    and observation.read_after_binding_version != binding.version):
                return {"state": "fresh_read_required", "binding_ref": binding.ref_id}
            if binding.observation_ref == observation.ref_id:
                return {"state": "unchanged", "binding_ref": binding.ref_id}
        google, marker = observed_values(self.session, observation, binding)
        if binding is None:
            if google["deletion"]:
                return {"state": "unlinked_tombstone", "observation_ref": observation.ref_id}
            task, changeset = self._apply(
                listing, observation, "create",
                {"task_list_ref": listing.ref_id, **task_patch(google)},
            )
            binding = TaskProviderBinding(
                task_ref=task.ref_id, task_list_ref=listing.ref_id,
                provider_task_id=observation.provider_task_id, canonical_version=task.version,
                correlation=uuid.uuid4().hex, created_by_changeset_ref=changeset.ref_id,
                baseline=deepcopy(google), provider_snapshot=deepcopy(google), limitations={},
                generation=1, active=True, detached=False,
            )
            fields: list[str] = []
        else:
            found_task = self.session.scalar(select(Task).where(Task.ref_id == binding.task_ref))
            assert found_task is not None
            task = found_task
            local = canonical_values(task)
            merged = merge_fields(binding.baseline, local, google, binding.provider_snapshot,
                                  limited=set(binding.limitations))
            fields = merged.conflicts
            if google["deletion"] and not local["deletion"]:
                # A provider deletion never erases independent local edits.
                if any(local.get(key) != value for key, value in binding.baseline.items()
                       if key != "deletion"):
                    fields = sorted(set(fields) | {"deletion"})
                    merged.inbound.pop("deletion", None)
                    merged.baseline.pop("deletion", None)
                    if "deletion" in binding.baseline:
                        merged.baseline["deletion"] = binding.baseline["deletion"]
                else:
                    task, _ = self._apply(
                        listing, observation, "retract", {}, task=task, binding=binding,
                    )
            elif merged.inbound:
                incoming = dict(merged.inbound)
                if incoming.get("status") == "completed":
                    incoming["completed_at"] = google["completed_at"]
                patch = task_patch(incoming, task)
                if patch:
                    task, _ = self._apply(
                        listing, observation, "update", patch, task=task, binding=binding,
                    )
            if fields:
                self.conflict(binding, observation, task, fields, google)
            binding.baseline = merged.baseline
            binding.provider_snapshot = deepcopy(google)
        binding.observation_ref = observation.ref_id
        binding.provider_etag = observation.provider_etag
        binding.marker_present = marker
        binding.observed_at = utc_now()
        raw_updated = observation.fields.get("updated")
        if isinstance(raw_updated, str):
            with suppress(ValueError):
                binding.provider_updated_at = instant(raw_updated)
        binding.canonical_version = task.version
        self.bindings.retain(binding, [observation.ref_id, listing.policy_ref])
        return {
            "state": "conflict" if fields else "reconciled", "task_ref": task.ref_id,
            "binding_ref": binding.ref_id, "conflict_fields": fields,
            "observation_ref": observation.ref_id,
        }
