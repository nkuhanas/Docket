from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.domain.public_refs import parse_public_ref
from docket.models import (
    AuditEvent,
    CalendarLane,
    CanonicalEvent,
    ChangeSet,
    Entity,
    Item,
    ReminderPlan,
    Task,
    TemporalBinding,
    TemporalCalendarProjection,
)
from docket.schemas.tracked_context import (
    ItemInput,
    ItemPatchInput,
    ReminderPlanInput,
    ReminderPlanPatchInput,
    TaskInput,
    TaskPatchInput,
    TemporalBindingInput,
    TemporalBindingPatchInput,
    TemporalCalendarProjectionInput,
    TemporalCalendarProjectionPatchInput,
)

TrackedContextHandler = Callable[[Session, ChangeSet, Any], list[str]]


class TrackedContextService:
    """Apply Item, Task, Time, projection, and reminder effects atomically."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def handlers(self) -> dict[str, TrackedContextHandler]:
        return {
            "item": self.apply_item,
            "temporal_binding": self.apply_temporal_binding,
            "task": self.apply_task,
            "temporal_calendar_projection": self.apply_temporal_projection,
            "reminder_plan": self.apply_reminder_plan,
        }

    @staticmethod
    def _provenance(changeset: ChangeSet, change: Any) -> dict[str, Any]:
        return {
            "basis_refs": list(change.basis_refs),
            "decision_refs": [
                ref for ref in change.basis_refs if parse_public_ref(ref)[0] == "dec"
            ],
            "source_refs": [
                ref for ref in change.basis_refs if parse_public_ref(ref)[0] == "src"
            ],
            "created_by_changeset_ref": changeset.ref_id,
        }

    def _audit(
        self,
        *,
        event_type: str,
        subject: Any,
        changeset: ChangeSet,
        change: Any,
        affected_refs: list[str],
    ) -> None:
        self.session.add(
            AuditEvent(
                event_type=event_type,
                entity_type=change.object_type,
                entity_id=subject.id,
                actor_type="operator",
                actor_id=get_settings().operator_discord_user_id,
                primary_ref=subject.ref_id,
                affected_refs=affected_refs,
                basis_refs=list(change.basis_refs),
                data={
                    "changeset_ref": changeset.ref_id,
                    "change_id": change.change_id,
                    "action": change.action,
                },
            )
        )

    def _item(self, ref_id: str) -> Item:
        item = self.session.scalar(select(Item).where(Item.ref_id == ref_id))
        if item is None:
            raise DocketError(
                code="item_not_found",
                message="Item public reference was not found.",
                details={"item_ref": ref_id},
            )
        return item

    def _task(self, ref_id: str) -> Task:
        task = self.session.scalar(select(Task).where(Task.ref_id == ref_id))
        if task is None:
            raise DocketError(
                code="task_not_found",
                message="Task public reference was not found.",
                details={"task_ref": ref_id},
            )
        return task

    def _temporal_binding(self, ref_id: str) -> TemporalBinding:
        binding = self.session.scalar(
            select(TemporalBinding).where(TemporalBinding.ref_id == ref_id)
        )
        if binding is None:
            raise DocketError(
                code="temporal_binding_not_found",
                message="TemporalBinding public reference was not found.",
                details={"temporal_binding_ref": ref_id},
            )
        return binding

    def _require_context_entities(self, refs: list[str]) -> None:
        if not refs:
            return
        found = set(
            self.session.scalars(select(Entity.ref_id).where(Entity.ref_id.in_(refs)))
        )
        missing = sorted(set(refs) - found)
        if missing:
            raise DocketError(
                code="context_entity_not_found",
                message="Item context contains an unknown Entity reference.",
                details={"entity_refs": missing},
            )

    def _require_temporal_subject(self, subject_ref: str) -> None:
        prefix, _payload = parse_public_ref(subject_ref)
        if prefix == "item":
            self._item(subject_ref)
        elif prefix == "task":
            self._task(subject_ref)
        else:  # schema prevents this branch
            raise DocketError(
                code="invalid_temporal_subject",
                message="TemporalBinding requires an Item or Task subject.",
            )

    def _require_reminder_subject(self, subject_ref: str) -> None:
        prefix, _payload = parse_public_ref(subject_ref)
        if prefix == "evt":
            exists = self.session.scalar(
                select(CanonicalEvent.id).where(CanonicalEvent.ref_id == subject_ref)
            )
        elif prefix == "time":
            exists = self.session.scalar(
                select(TemporalBinding.id).where(TemporalBinding.ref_id == subject_ref)
            )
        else:  # schema prevents this branch
            exists = None
        if exists is None:
            raise DocketError(
                code="reminder_subject_not_found",
                message="ReminderPlan subject was not found.",
                details={"subject_ref": subject_ref},
            )

    def apply_item(self, _session: Session, changeset: ChangeSet, change: Any) -> list[str]:
        if change.action == "create":
            spec = ItemInput.model_validate(change.create_spec)
            self._require_context_entities(list(spec.context_entity_refs))
            if spec.parent_item_ref is not None:
                self._item(spec.parent_item_ref)
            provenance = self._provenance(changeset, change)
            item = Item(
                title=spec.title,
                kind=spec.kind,
                description=spec.description,
                context_entity_refs=list(spec.context_entity_refs),
                parent_item_ref=spec.parent_item_ref,
                canonical_status=spec.canonical_status,
                metadata_json=dict(spec.metadata_json),
                source_refs=list(dict.fromkeys([*provenance["source_refs"], *spec.source_refs])),
                basis_refs=provenance["basis_refs"],
                decision_refs=provenance["decision_refs"],
                created_by_changeset_ref=provenance["created_by_changeset_ref"],
            )
            self.session.add(item)
            self.session.flush()
        else:
            item = self._item(change.object_ref)
            if change.action == "retract":
                item.canonical_status = "retracted"
            elif change.action == "update":
                patch = ItemPatchInput.model_validate(change.payload)
                values = patch.model_dump(exclude_unset=True)
                if "context_entity_refs" in values:
                    self._require_context_entities(values["context_entity_refs"] or [])
                if values.get("parent_item_ref") is not None:
                    parent = self._item(values["parent_item_ref"])
                    if parent.ref_id == item.ref_id:
                        raise DocketError(
                            code="item_parent_cycle",
                            message="Item cannot be its own parent.",
                        )
                for key, value in values.items():
                    setattr(item, key, value)
            else:
                raise DocketError(
                    code="unsupported_item_action",
                    message="Unsupported Item mutation action.",
                )
            item.basis_refs = list(change.basis_refs)
            item.decision_refs = self._provenance(changeset, change)["decision_refs"]
            item.source_refs = list(
                dict.fromkeys(
                    [*item.source_refs, *self._provenance(changeset, change)["source_refs"]]
                )
            )
            item.version += 1
        self._audit(
            event_type="item.changed",
            subject=item,
            changeset=changeset,
            change=change,
            affected_refs=[item.ref_id],
        )
        return [item.ref_id]

    def apply_temporal_binding(
        self, _session: Session, changeset: ChangeSet, change: Any
    ) -> list[str]:
        if change.action in {"create", "supersede"}:
            spec = TemporalBindingInput.model_validate(change.create_spec)
            assert spec.subject_ref is not None
            self._require_temporal_subject(spec.subject_ref)
            prior: TemporalBinding | None = None
            if change.action == "supersede":
                prior = self._temporal_binding(change.object_ref)
                prior.canonical_status = "historical"
                prior.basis_refs = list(change.basis_refs)
                prior.version += 1
            else:
                active = self.session.scalar(
                    select(TemporalBinding).where(
                        TemporalBinding.subject_ref == spec.subject_ref,
                        TemporalBinding.role == spec.role,
                        TemporalBinding.binding_key == spec.binding_key,
                        TemporalBinding.canonical_status == "active",
                    )
                )
                if active is not None:
                    raise DocketError(
                        code="temporal_binding_exists",
                        message=(
                            "An active TemporalBinding already owns this subject, "
                            "role, and key."
                        ),
                        details={"temporal_binding_ref": active.ref_id},
                    )
            provenance = self._provenance(changeset, change)
            binding = TemporalBinding(
                subject_ref=spec.subject_ref,
                role=spec.role,
                binding_key=spec.binding_key,
                temporal_value=spec.temporal_value.model_dump(mode="json"),
                canonical_status=spec.canonical_status,
                basis_refs=provenance["basis_refs"],
                decision_refs=provenance["decision_refs"],
                source_refs=list(dict.fromkeys([*provenance["source_refs"], *spec.source_refs])),
                created_by_changeset_ref=provenance["created_by_changeset_ref"],
                supersedes_ref=prior.ref_id if prior is not None else None,
                version=(prior.version if prior is not None else 1),
            )
            self.session.add(binding)
            self.session.flush()
            affected = [binding.ref_id, *([prior.ref_id] if prior is not None else [])]
        else:
            binding = self._temporal_binding(change.object_ref)
            if change.action == "retract":
                binding.canonical_status = "retracted"
            elif change.action == "update":
                patch = TemporalBindingPatchInput.model_validate(change.payload)
                values = patch.model_dump(exclude_unset=True)
                if "temporal_value" in values and patch.temporal_value is not None:
                    values["temporal_value"] = patch.temporal_value.model_dump(mode="json")
                for key, value in values.items():
                    setattr(binding, key, value)
            else:
                raise DocketError(
                    code="unsupported_temporal_binding_action",
                    message="Unsupported TemporalBinding mutation action.",
                )
            binding.basis_refs = list(change.basis_refs)
            binding.decision_refs = self._provenance(changeset, change)["decision_refs"]
            binding.source_refs = list(
                dict.fromkeys(
                    [
                        *binding.source_refs,
                        *self._provenance(changeset, change)["source_refs"],
                    ]
                )
            )
            binding.version += 1
            affected = [binding.ref_id]
        self._audit(
            event_type="temporal_binding.changed",
            subject=binding,
            changeset=changeset,
            change=change,
            affected_refs=affected,
        )
        return affected

    def apply_task(self, _session: Session, changeset: ChangeSet, change: Any) -> list[str]:
        if change.action == "create":
            spec = TaskInput.model_validate(change.create_spec)
            assert spec.item_ref is not None
            self._item(spec.item_ref)
            provenance = self._provenance(changeset, change)
            task = Task(
                item_ref=spec.item_ref,
                title=spec.title,
                description=spec.description,
                task_state=spec.task_state,
                priority=spec.priority,
                canonical_status=spec.canonical_status,
                completed_at=spec.completed_at,
                basis_refs=provenance["basis_refs"],
                decision_refs=provenance["decision_refs"],
                source_refs=list(dict.fromkeys([*provenance["source_refs"], *spec.source_refs])),
                created_by_changeset_ref=provenance["created_by_changeset_ref"],
            )
            self.session.add(task)
            self.session.flush()
        else:
            task = self._task(change.object_ref)
            if change.action == "retract":
                task.canonical_status = "retracted"
            elif change.action == "update":
                patch = TaskPatchInput.model_validate(change.payload)
                values = patch.model_dump(exclude_unset=True)
                if values.get("item_ref") is not None:
                    self._item(values["item_ref"])
                for key, value in values.items():
                    setattr(task, key, value)
            else:
                raise DocketError(
                    code="unsupported_task_action",
                    message="Unsupported Task mutation action.",
                )
            task.basis_refs = list(change.basis_refs)
            task.decision_refs = self._provenance(changeset, change)["decision_refs"]
            task.source_refs = list(
                dict.fromkeys(
                    [*task.source_refs, *self._provenance(changeset, change)["source_refs"]]
                )
            )
            task.version += 1
        self._audit(
            event_type="task.changed",
            subject=task,
            changeset=changeset,
            change=change,
            affected_refs=[task.ref_id, task.item_ref],
        )
        return [task.ref_id]

    def apply_temporal_projection(
        self, _session: Session, changeset: ChangeSet, change: Any
    ) -> list[str]:
        projection: TemporalCalendarProjection
        if change.action == "create":
            spec = TemporalCalendarProjectionInput.model_validate(change.create_spec)
            assert spec.temporal_binding_ref is not None
            assert spec.lane_ref is not None
            self._temporal_binding(spec.temporal_binding_ref)
            lane = self.session.scalar(
                select(CalendarLane).where(
                    CalendarLane.ref_id == spec.lane_ref,
                    CalendarLane.enabled.is_(True),
                )
            )
            if lane is None:
                raise DocketError(
                    code="calendar_lane_unavailable",
                    message="Temporal projection requires an enabled CalendarLane.",
                )
            if spec.reminder_plan_ref is not None:
                self._reminder_plan(spec.reminder_plan_ref)
            projection = TemporalCalendarProjection(
                temporal_binding_ref=spec.temporal_binding_ref,
                lane_ref=spec.lane_ref,
                display_policy=spec.display_policy.model_dump(mode="json"),
                reminder_plan_ref=spec.reminder_plan_ref,
                enabled=spec.enabled,
                basis_refs=list(change.basis_refs),
                created_by_changeset_ref=changeset.ref_id,
            )
            self.session.add(projection)
            self.session.flush()
        else:
            stored_projection = self.session.scalar(
                select(TemporalCalendarProjection).where(
                    TemporalCalendarProjection.ref_id == change.object_ref
                )
            )
            if stored_projection is None:
                raise DocketError(
                    code="temporal_calendar_projection_not_found",
                    message="TemporalCalendarProjection was not found.",
                )
            projection = stored_projection
            if change.action == "retract":
                projection.enabled = False
            elif change.action == "update":
                patch = TemporalCalendarProjectionPatchInput.model_validate(change.payload)
                values = patch.model_dump(exclude_unset=True)
                if "display_policy" in values and patch.display_policy is not None:
                    values["display_policy"] = patch.display_policy.model_dump(mode="json")
                if values.get("lane_ref") is not None:
                    lane_id = self.session.scalar(
                        select(CalendarLane.id).where(
                            CalendarLane.ref_id == values["lane_ref"],
                            CalendarLane.enabled.is_(True),
                        )
                    )
                    if lane_id is None:
                        raise DocketError(
                            code="calendar_lane_unavailable",
                            message="Temporal projection requires an enabled CalendarLane.",
                        )
                if values.get("reminder_plan_ref") is not None:
                    self._reminder_plan(values["reminder_plan_ref"])
                for key, value in values.items():
                    setattr(projection, key, value)
            else:
                raise DocketError(
                    code="unsupported_temporal_projection_action",
                    message="Unsupported TemporalCalendarProjection mutation action.",
                )
            projection.basis_refs = list(change.basis_refs)
            projection.version += 1
        self._audit(
            event_type="temporal_calendar_projection.changed",
            subject=projection,
            changeset=changeset,
            change=change,
            affected_refs=[projection.ref_id, projection.temporal_binding_ref],
        )
        return [projection.ref_id]

    def _reminder_plan(self, ref_id: str) -> ReminderPlan:
        reminder = self.session.scalar(
            select(ReminderPlan).where(ReminderPlan.ref_id == ref_id)
        )
        if reminder is None:
            raise DocketError(
                code="reminder_plan_not_found",
                message="ReminderPlan public reference was not found.",
                details={"reminder_plan_ref": ref_id},
            )
        return reminder

    def apply_reminder_plan(
        self, _session: Session, changeset: ChangeSet, change: Any
    ) -> list[str]:
        if change.action == "create":
            spec = ReminderPlanInput.model_validate(change.create_spec)
            assert spec.subject_ref is not None
            self._require_reminder_subject(spec.subject_ref)
            reminder = ReminderPlan(
                subject_ref=spec.subject_ref,
                delivery_channels=list(spec.delivery_channels),
                lead_seconds=list(spec.lead_seconds),
                date_trigger_local_time=(
                    spec.date_trigger_local_time.isoformat()
                    if spec.date_trigger_local_time is not None
                    else None
                ),
                timezone=spec.timezone,
                canonical_status=spec.canonical_status,
                basis_refs=list(change.basis_refs),
                created_by_changeset_ref=changeset.ref_id,
            )
            self.session.add(reminder)
            self.session.flush()
        else:
            reminder = self._reminder_plan(change.object_ref)
            if change.action == "retract":
                reminder.canonical_status = "retracted"
            elif change.action == "update":
                patch = ReminderPlanPatchInput.model_validate(change.payload)
                values = patch.model_dump(exclude_unset=True)
                if values.get("subject_ref") is not None:
                    self._require_reminder_subject(values["subject_ref"])
                if "date_trigger_local_time" in values:
                    local_time = patch.date_trigger_local_time
                    values["date_trigger_local_time"] = (
                        local_time.isoformat() if local_time is not None else None
                    )
                for key, value in values.items():
                    setattr(reminder, key, value)
            else:
                raise DocketError(
                    code="unsupported_reminder_plan_action",
                    message="Unsupported ReminderPlan mutation action.",
                )
            reminder.basis_refs = list(change.basis_refs)
            reminder.version += 1
        self._audit(
            event_type="reminder_plan.changed",
            subject=reminder,
            changeset=changeset,
            change=change,
            affected_refs=[reminder.ref_id, reminder.subject_ref],
        )
        return [reminder.ref_id]
