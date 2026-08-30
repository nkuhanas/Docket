from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.domain.public_refs import parse_public_ref
from docket.models import (
    AuditEvent,
    CalendarLane,
    CanonicalEvent,
    ChangeSet,
    Entity,
    EventItemLink,
    Fact,
    InterpretedStatement,
    Item,
    ItemSourceBinding,
    ReminderPlan,
    Source,
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
            "source_refs": [ref for ref in change.basis_refs if parse_public_ref(ref)[0] == "src"],
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

    def _source_fragments(
        self,
        change: Any,
        *,
        source_refs: list[str],
    ) -> list[dict[str, Any]]:
        allowed_source_refs = set(source_refs)
        statement_refs = [
            ref for ref in change.basis_refs if ref.startswith("stm_")
        ]
        if not statement_refs or not allowed_source_refs:
            return []
        statements = list(
            self.session.scalars(
                select(InterpretedStatement).where(
                    InterpretedStatement.ref_id.in_(statement_refs)
                )
            )
        )
        sources = {
            source.ref_id: source
            for source in self.session.scalars(
                select(Source).where(
                    Source.ref_id.in_(
                        [
                            statement.source_ref
                            for statement in statements
                            if statement.source_ref is not None
                        ]
                    )
                )
            )
        }
        fragments: list[dict[str, Any]] = []
        for statement in statements:
            if (
                statement.source_ref not in allowed_source_refs
                or statement.source_fragment_locator is None
            ):
                continue
            source = sources.get(statement.source_ref)
            if source is None:
                raise DocketError(
                    code="item_source_not_found",
                    message="An Item source-fragment statement references an unknown Source.",
                    details={"source_ref": statement.source_ref},
                )
            fragments.append(
                {
                    "statement_ref": statement.ref_id,
                    "source_ref": source.ref_id,
                    "source_revision_key": (
                        f"{source.ref_id}:{source.content_hash or 'unhashed'}"
                    ),
                    "source_fragment_locator": dict(statement.source_fragment_locator),
                    "locator_hash": sha256_json(statement.source_fragment_locator),
                    "semantic_role": statement.predicate,
                }
            )
        return fragments

    def _correlated_item(
        self,
        fragments: list[dict[str, Any]],
    ) -> Item | None:
        item_refs: set[str] = set()
        for fragment in fragments:
            binding = self.session.scalar(
                select(ItemSourceBinding).where(
                    ItemSourceBinding.source_ref == fragment["source_ref"],
                    ItemSourceBinding.source_revision_key
                    == fragment["source_revision_key"],
                    ItemSourceBinding.locator_hash == fragment["locator_hash"],
                    ItemSourceBinding.semantic_role == fragment["semantic_role"],
                )
            )
            if binding is not None:
                item_refs.add(binding.item_ref)
        if len(item_refs) > 1:
            raise DocketError(
                code="item_source_correlation_conflict",
                message="Exact source fragments resolve to more than one Item.",
                details={"item_refs": sorted(item_refs)},
            )
        return self._item(next(iter(item_refs))) if item_refs else None

    def _bind_item_fragments(
        self,
        item: Item,
        fragments: list[dict[str, Any]],
        change: Any,
    ) -> None:
        for fragment in fragments:
            existing = self.session.scalar(
                select(ItemSourceBinding).where(
                    ItemSourceBinding.source_ref == fragment["source_ref"],
                    ItemSourceBinding.source_revision_key
                    == fragment["source_revision_key"],
                    ItemSourceBinding.locator_hash == fragment["locator_hash"],
                    ItemSourceBinding.semantic_role == fragment["semantic_role"],
                )
            )
            if existing is not None:
                if existing.item_ref != item.ref_id:
                    raise DocketError(
                        code="item_source_correlation_conflict",
                        message="An exact source fragment is already bound to another Item.",
                        details={
                            "item_ref": item.ref_id,
                            "bound_item_ref": existing.item_ref,
                        },
                    )
                continue
            self.session.add(
                ItemSourceBinding(
                    item_ref=item.ref_id,
                    source_ref=fragment["source_ref"],
                    source_revision_key=fragment["source_revision_key"],
                    source_fragment_locator=fragment["source_fragment_locator"],
                    locator_hash=fragment["locator_hash"],
                    semantic_role=fragment["semantic_role"],
                    basis_refs=list(
                        dict.fromkeys(
                            [*change.basis_refs, fragment["statement_ref"]]
                        )
                    ),
                )
            )

    @staticmethod
    def _page(items: list[dict[str, Any]], *, limit: int, cursor: str | None) -> dict[str, Any]:
        try:
            offset = int(cursor or "0")
        except ValueError as exc:
            raise DocketError(
                code="invalid_cursor",
                message="Cursor must be a non-negative offset.",
            ) from exc
        if offset < 0:
            raise DocketError(
                code="invalid_cursor",
                message="Cursor must be a non-negative offset.",
            )
        page = items[offset : offset + limit]
        next_offset = offset + len(page)
        result: dict[str, Any] = {
            "ok": True,
            "items": page,
            "count": len(page),
            "total_if_known": len(items),
            "truncated": next_offset < len(items),
        }
        if next_offset < len(items):
            result["cursor"] = str(next_offset)
        while page and len(json.dumps(result, separators=(",", ":")).encode()) > 16_384:
            page.pop()
            result["count"] = len(page)
            result["truncated"] = True
            result["cursor"] = str(offset + len(page))
        return result

    @staticmethod
    def _temporal_dates(value: dict[str, Any]) -> tuple[date | None, date | None]:
        kind = value.get("kind")
        try:
            if kind == "date":
                parsed = date.fromisoformat(str(value["date"]))
                return parsed, parsed
            if kind == "datetime":
                parsed = date.fromisoformat(str(value["local_datetime"])[:10])
                return parsed, parsed
            if kind == "date_interval":
                return (
                    date.fromisoformat(str(value["start_date"])),
                    date.fromisoformat(str(value["end_date"])),
                )
            if kind == "datetime_interval":
                return (
                    date.fromisoformat(str(value["start_local"])[:10]),
                    date.fromisoformat(str(value["end_local"])[:10]),
                )
        except (KeyError, TypeError, ValueError):
            return None, None
        return None, None

    def _item_facets(
        self, item_refs: list[str]
    ) -> tuple[dict[str, list[TemporalBinding]], dict[str, list[Task]]]:
        if not item_refs:
            return {}, {}
        temporal_by_item: dict[str, list[TemporalBinding]] = {ref: [] for ref in item_refs}
        tasks_by_item: dict[str, list[Task]] = {ref: [] for ref in item_refs}
        tasks = list(
            self.session.scalars(
                select(Task)
                .where(Task.item_ref.in_(item_refs), Task.canonical_status == "active")
                .order_by(Task.created_at, Task.ref_id)
            )
        )
        for task in tasks:
            tasks_by_item[task.item_ref].append(task)
        task_to_item = {task.ref_id: task.item_ref for task in tasks}
        subjects = [*item_refs, *task_to_item]
        bindings = list(
            self.session.scalars(
                select(TemporalBinding)
                .where(
                    TemporalBinding.subject_ref.in_(subjects),
                    TemporalBinding.canonical_status == "active",
                )
                .order_by(TemporalBinding.created_at, TemporalBinding.ref_id)
            )
        )
        for binding in bindings:
            item_ref = (
                binding.subject_ref
                if binding.subject_ref in temporal_by_item
                else task_to_item.get(binding.subject_ref)
            )
            if item_ref is not None:
                temporal_by_item[item_ref].append(binding)
        return temporal_by_item, tasks_by_item

    def query_items(
        self,
        *,
        text: str | None,
        kind: str | None,
        context_entity_ref: str | None,
        parent_item_ref: str | None,
        temporal_role: str | None,
        date_from: date | None,
        date_to: date | None,
        has_open_task: bool | None,
        source_ref: str | None,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        statement = select(Item).where(Item.canonical_status == "active")
        if text:
            needle = f"%{text.casefold()}%"
            statement = statement.where(
                or_(Item.title.ilike(needle), Item.description.ilike(needle))
            )
        if kind is not None:
            statement = statement.where(Item.kind == kind)
        if parent_item_ref is not None:
            statement = statement.where(Item.parent_item_ref == parent_item_ref)
        candidates = list(
            self.session.scalars(statement.order_by(Item.title, Item.ref_id).limit(1000))
        )
        if context_entity_ref is not None:
            candidates = [
                item for item in candidates if context_entity_ref in item.context_entity_refs
            ]
        if source_ref is not None:
            candidates = [item for item in candidates if source_ref in item.source_refs]
        refs = [item.ref_id for item in candidates]
        temporal_by_item, tasks_by_item = self._item_facets(refs)

        def included(item: Item) -> bool:
            tasks = tasks_by_item[item.ref_id]
            open_task = any(task.task_state not in {"completed", "cancelled"} for task in tasks)
            if has_open_task is not None and open_task is not has_open_task:
                return False
            bindings = temporal_by_item[item.ref_id]
            if temporal_role is not None:
                bindings = [binding for binding in bindings if binding.role == temporal_role]
                if not bindings:
                    return False
            if date_from is not None or date_to is not None:
                matches = False
                for binding in bindings:
                    start, end = self._temporal_dates(binding.temporal_value)
                    if start is None or end is None:
                        continue
                    if date_from is not None and end < date_from:
                        continue
                    if date_to is not None and start > date_to:
                        continue
                    matches = True
                    break
                if not matches:
                    return False
            return True

        filtered = [item for item in candidates if included(item)]
        context_refs = sorted({ref for item in filtered for ref in item.context_entity_refs})
        entities = (
            {
                entity.ref_id: entity.display_name
                for entity in self.session.scalars(
                    select(Entity).where(Entity.ref_id.in_(context_refs))
                )
            }
            if context_refs
            else {}
        )
        projected = []
        for item in filtered:
            bindings = temporal_by_item[item.ref_id]
            tasks = tasks_by_item[item.ref_id]
            projected.append(
                {
                    "ref": item.ref_id,
                    "title": item.title,
                    **({"kind": item.kind} if item.kind is not None else {}),
                    "context_summary": [
                        {"ref": ref, "name": entities.get(ref, ref)}
                        for ref in item.context_entity_refs[:5]
                    ],
                    "temporal_summary": [
                        {
                            "ref": binding.ref_id,
                            "role": binding.role,
                            "value": binding.temporal_value,
                        }
                        for binding in bindings[:5]
                    ],
                    "task_summary": [
                        {
                            "ref": task.ref_id,
                            "title": task.title,
                            "state": task.task_state,
                        }
                        for task in tasks[:5]
                    ],
                }
            )
        return self._page(projected, limit=limit, cursor=cursor)

    def item_context(self, item_ref: str) -> dict[str, Any]:
        item = self._item(item_ref)
        temporal_by_item, tasks_by_item = self._item_facets([item.ref_id])
        child_items = list(
            self.session.scalars(
                select(Item)
                .where(
                    Item.parent_item_ref == item.ref_id,
                    Item.canonical_status == "active",
                )
                .order_by(Item.title, Item.ref_id)
                .limit(25)
            )
        )
        facts = list(
            self.session.scalars(
                select(Fact)
                .where(Fact.subject_ref == item.ref_id, Fact.status == "active")
                .order_by(Fact.predicate, Fact.ref_id)
                .limit(50)
            )
        )
        links = list(
            self.session.scalars(
                select(EventItemLink)
                .where(EventItemLink.item_ref == item.ref_id)
                .order_by(EventItemLink.created_at)
                .limit(25)
            )
        )
        event_refs = [link.event_ref for link in links]
        events = (
            {
                event.ref_id: event
                for event in self.session.scalars(
                    select(CanonicalEvent).where(CanonicalEvent.ref_id.in_(event_refs))
                )
            }
            if event_refs
            else {}
        )
        binding_refs = [binding.ref_id for binding in temporal_by_item[item.ref_id]]
        projections = (
            list(
                self.session.scalars(
                    select(TemporalCalendarProjection)
                    .where(TemporalCalendarProjection.temporal_binding_ref.in_(binding_refs))
                    .order_by(TemporalCalendarProjection.created_at)
                    .limit(25)
                )
            )
            if binding_refs
            else []
        )
        source_bindings = list(
            self.session.scalars(
                select(ItemSourceBinding)
                .where(ItemSourceBinding.item_ref == item.ref_id)
                .order_by(ItemSourceBinding.created_at)
                .limit(50)
            )
        )
        result: dict[str, Any] = {
            "ok": True,
            "ref": item.ref_id,
            "title": item.title,
            "kind": item.kind,
            "description": item.description,
            "canonical_status": item.canonical_status,
            "version": item.version,
            "context_entity_refs": item.context_entity_refs,
            "parent_item_ref": item.parent_item_ref,
            "children": [
                {"ref": child.ref_id, "title": child.title, "kind": child.kind}
                for child in child_items
            ],
            "facts": [
                {"ref": fact.ref_id, "predicate": fact.predicate, "value": fact.value_json}
                for fact in facts
            ],
            "temporal_bindings": [
                {
                    "ref": binding.ref_id,
                    "subject_ref": binding.subject_ref,
                    "role": binding.role,
                    "binding_key": binding.binding_key,
                    "value": binding.temporal_value,
                    "version": binding.version,
                }
                for binding in temporal_by_item[item.ref_id]
            ],
            "tasks": [
                {
                    "ref": task.ref_id,
                    "title": task.title,
                    "state": task.task_state,
                    "priority": task.priority,
                    "version": task.version,
                }
                for task in tasks_by_item[item.ref_id]
            ],
            "linked_events": [
                {
                    "ref": link.event_ref,
                    "title": events[link.event_ref].title if link.event_ref in events else None,
                    "realizes_temporal_binding_ref": link.realizes_temporal_binding_ref,
                }
                for link in links
            ],
            "calendar_projections": [
                {
                    "ref": projection.ref_id,
                    "temporal_binding_ref": projection.temporal_binding_ref,
                    "lane_ref": projection.lane_ref,
                    "enabled": projection.enabled,
                }
                for projection in projections
            ],
            "source_bindings": [
                {
                    "source_ref": binding.source_ref,
                    "source_revision_key": binding.source_revision_key,
                    "source_fragment_locator": binding.source_fragment_locator,
                    "semantic_role": binding.semantic_role,
                }
                for binding in source_bindings
            ],
            "provenance_refs": list(
                dict.fromkeys(
                    [
                        *item.basis_refs,
                        *item.decision_refs,
                        *item.source_refs,
                        item.created_by_changeset_ref,
                    ]
                )
            ),
        }
        if len(json.dumps(result, separators=(",", ":")).encode()) > 16_384:
            result["children"] = result["children"][:10]
            result["facts"] = result["facts"][:20]
            result["linked_events"] = result["linked_events"][:10]
            result["calendar_projections"] = result["calendar_projections"][:10]
            result["source_bindings"] = result["source_bindings"][:20]
            result["warnings"] = ["output_truncated"]
        return result

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
        found = set(self.session.scalars(select(Entity.ref_id).where(Entity.ref_id.in_(refs))))
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

    def _require_date_reminder_policy(
        self,
        subject_ref: str,
        *,
        date_trigger_local_time: object | None,
        timezone: str | None,
    ) -> None:
        if subject_ref.startswith("time_"):
            binding = self.session.scalar(
                select(TemporalBinding).where(TemporalBinding.ref_id == subject_ref)
            )
            assert binding is not None
            date_only = binding.temporal_value.get("kind") in {"date", "date_interval"}
        else:
            event = self.session.scalar(
                select(CanonicalEvent).where(CanonicalEvent.ref_id == subject_ref)
            )
            assert event is not None
            timing = event.event_spec.get("timing", {})
            date_only = isinstance(timing, dict) and timing.get("kind") == "all_day"
        if date_only and (date_trigger_local_time is None or timezone is None):
            raise DocketError(
                code="date_reminder_policy_required",
                message=(
                    "A date-only reminder requires an explicit local trigger time "
                    "and IANA timezone."
                ),
                details={"subject_ref": subject_ref},
            )

    def apply_item(self, _session: Session, changeset: ChangeSet, change: Any) -> list[str]:
        if change.action == "create":
            spec = ItemInput.model_validate(change.create_spec)
            self._require_context_entities(list(spec.context_entity_refs))
            if spec.parent_item_ref is not None:
                self._item(spec.parent_item_ref)
            fragments = self._source_fragments(
                change,
                source_refs=list(spec.source_refs),
            )
            attachment_sources = list(
                self.session.scalars(
                    select(Source.ref_id).where(
                        Source.ref_id.in_(spec.source_refs),
                        Source.source_kind == "attachment",
                    )
                )
            )
            fragment_source_refs = {
                str(fragment["source_ref"]) for fragment in fragments
            }
            missing_fragment_sources = sorted(
                set(attachment_sources) - fragment_source_refs
            )
            if missing_fragment_sources:
                raise DocketError(
                    code="item_source_fragment_required",
                    message=(
                        "An attachment-imported Item requires an exact derived "
                        "source-fragment statement."
                    ),
                    details={"source_refs": missing_fragment_sources},
                )
            correlated = self._correlated_item(fragments)
            if correlated is not None:
                expected_identity = (
                    spec.title,
                    spec.kind,
                    list(spec.context_entity_refs),
                    spec.parent_item_ref,
                )
                existing_identity = (
                    correlated.title,
                    correlated.kind,
                    list(correlated.context_entity_refs),
                    correlated.parent_item_ref,
                )
                if (
                    correlated.canonical_status != "active"
                    or existing_identity != expected_identity
                ):
                    raise DocketError(
                        code="item_source_correlation_conflict",
                        message=(
                            "The exact source fragment is already bound to an "
                            "incompatible Item."
                        ),
                        details={"item_ref": correlated.ref_id},
                    )
                self._bind_item_fragments(correlated, fragments, change)
                return [correlated.ref_id]
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
            self._bind_item_fragments(item, fragments, change)
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
                    expected_value = spec.temporal_value.model_dump(mode="json")
                    statement_refs = {
                        ref for ref in change.basis_refs if ref.startswith("stm_")
                    }
                    if (
                        statement_refs
                        and statement_refs.issubset(set(active.basis_refs))
                        and set(spec.source_refs).issubset(set(active.source_refs))
                        and active.temporal_value == expected_value
                    ):
                        return [active.ref_id]
                    raise DocketError(
                        code="temporal_binding_exists",
                        message=(
                            "An active TemporalBinding already owns this subject, role, and key."
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
            statement_refs = {
                ref for ref in change.basis_refs if ref.startswith("stm_")
            }
            exact_tasks = list(
                self.session.scalars(
                    select(Task).where(
                        Task.item_ref == spec.item_ref,
                        Task.title == spec.title,
                        Task.task_state == spec.task_state,
                        Task.priority == spec.priority,
                        Task.canonical_status == spec.canonical_status,
                    )
                )
            )
            exact_source_replays = [
                existing
                for existing in exact_tasks
                if statement_refs
                and statement_refs.issubset(set(existing.basis_refs))
                and set(spec.source_refs).issubset(set(existing.source_refs))
                and existing.description == spec.description
                and existing.completed_at == spec.completed_at
            ]
            if len(exact_source_replays) > 1:
                raise DocketError(
                    code="task_source_correlation_conflict",
                    message="Exact source evidence resolves to more than one Task.",
                    details={
                        "task_refs": sorted(task.ref_id for task in exact_source_replays)
                    },
                )
            if exact_source_replays:
                return [exact_source_replays[0].ref_id]
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
        reminder = self.session.scalar(select(ReminderPlan).where(ReminderPlan.ref_id == ref_id))
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
            self._require_date_reminder_policy(
                spec.subject_ref,
                date_trigger_local_time=spec.date_trigger_local_time,
                timezone=spec.timezone,
            )
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
                next_subject_ref = values.get("subject_ref", reminder.subject_ref)
                self._require_date_reminder_policy(
                    next_subject_ref,
                    date_trigger_local_time=values.get(
                        "date_trigger_local_time", reminder.date_trigger_local_time
                    ),
                    timezone=values.get("timezone", reminder.timezone),
                )
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
