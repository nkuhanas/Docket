"""Opt-in revision-pinned Calendar snapshots; display readiness never gates work."""

from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import (
    AuditEvent,
    CalendarLane,
    CanonicalEvent,
    ChangeSet,
    EventItemLink,
    NativeContext,
    WorkCalendarProjection,
    WorkCalendarRevision,
    WorkSession,
)
from docket.models.base import utc_now
from docket.schemas.authority import (
    CanonicalEventCancel,
    CanonicalEventCreate,
    CanonicalEventModify,
    LaneRoutingDecisionCreate,
    WorkCalendarCreate,
    WorkCalendarModify,
    WorkCalendarRetract,
    mutation_input_json,
)
from docket.schemas.calendar import StandaloneCalendarEventInput, TimedEventTiming
from docket.schemas.events import CanonicalEventCreateSpec, CanonicalEventPatchSpec, EventFieldPatch
from docket.schemas.policy import LaneRoutingDecisionCreateSpec
from docket.schemas.work_calendar import ShowWorkSession, WorkCalendarSpec
from docket.services.business_commands import mutation_plan
from docket.services.compensation_policies import instant
from docket.services.native_commands import NativeCommandService
from docket.services.work_attribution import WorkAttributionService

WORK_CALENDAR_MODELS: dict[str, type[BaseModel]] = {"docket_show_work_session": ShowWorkSession}
NAMESPACE = "actual-work:"
STATE_FIELDS = (
    "renderer_version",
    "source_revision_ref",
    "observed_work_revision_ref",
    "event_ref",
    "calendar_ref",
    "enabled",
    "needs_refresh",
)


def invalid(message: str, *, field: str = "work_ref") -> DocketError:
    return DocketError(code="work_calendar_unavailable", message=message, details={"field": field})


class WorkCalendarService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.work = WorkAttributionService(session)

    def for_work(self, ref: str, *, lock: bool = False) -> WorkCalendarProjection | None:
        query = select(WorkCalendarProjection).where(WorkCalendarProjection.work_ref == ref)
        if lock:
            query = query.with_for_update(key_share=True).execution_options(populate_existing=True)
        return self.session.scalar(query)

    def head(self, ref: str, *, lock: bool = False) -> WorkCalendarProjection:
        query = select(WorkCalendarProjection).where(WorkCalendarProjection.ref_id == ref)
        if lock:
            query = query.with_for_update(key_share=True).execution_options(populate_existing=True)
        row = self.session.scalar(query)
        if row is None or row.operator_ref != require_principal("read").operator_ref:
            raise invalid("Select an owned work Calendar projection.", field="target_ref")
        return row

    def for_event(self, ref: str) -> WorkCalendarProjection | None:
        return self.session.scalar(
            select(WorkCalendarProjection).where(
                WorkCalendarProjection.ref_id.in_(
                    select(WorkCalendarRevision.projection_ref).where(
                        WorkCalendarRevision.event_ref == ref
                    )
                )
            )
        )

    def require_editable_event(self, ref: str) -> None:
        row = self.for_event(ref)
        if row:
            self.head(row.ref_id)
            raise DocketError(
                code="work_calendar_read_only",
                message="Edit the WorkSession, refresh its snapshot or hide its wcal_ projection.",
                details={
                    "field": "event_ref",
                    "work_ref": row.work_ref,
                    "projection_ref": row.ref_id,
                    "next_action": "docket_get_work_context",
                },
            )

    def guard_event(self, changeset: ChangeSet, change: Any) -> None:
        event = (
            self.session.scalar(
                select(CanonicalEvent).where(CanonicalEvent.ref_id == change.object_ref)
            )
            if change.object_ref
            else None
        )
        create = change.create_spec or {}
        key = (
            (create.get("canonical_key") or "")
            if isinstance(create, dict)
            else getattr(create, "canonical_key", "") or ""
        )
        projection = self.for_event(event.ref_id) if event else None
        if projection is None and not key.startswith(NAMESPACE):
            return
        for candidate in changeset.tracked_context_changes:
            if candidate.get("object_type") != "work_calendar_projection":
                continue
            if candidate["action"] == "retract":
                if (
                    projection
                    and candidate.get("object_ref") == projection.ref_id
                    and event
                    and event.ref_id == projection.event_ref
                    and change.action == "retract"
                ):
                    self.head(projection.ref_id)
                    return
                continue
            spec = WorkCalendarSpec.model_validate(
                candidate.get("create_spec") or candidate.get("payload")
            )
            matches = (
                spec.event_change_id == change.change_id
                if change.action == "create"
                else bool(
                    projection
                    and candidate.get("object_ref") == projection.ref_id
                    and event
                    and event.ref_id == projection.event_ref
                )
            )
            if not matches:
                continue
            work = self.work.head(WorkSession, spec.work_ref, lock=True)
            if (
                work.version != spec.work_version
                or self.work.work_revision(work).ref_id != spec.source_revision_ref
            ):
                raise invalid("Refresh the exact work snapshot after a source correction.")
            return
        raise DocketError(
            code="work_calendar_read_only",
            message="A managed work snapshot requires its correlated projection command.",
            details={"field": "event_ref", "next_action": "docket_show_work_session"},
        )

    def view(self, row: WorkCalendarProjection) -> dict[str, Any]:
        self.head(row.ref_id)
        current = self.work.head(WorkSession, row.work_ref)
        revision = self.work.work_revision(current)
        refresh_supported = bool(
            current.lifecycle == "recorded"
            and not current.superseded
            and revision.start_at
            and revision.end_at
            and revision.duration_us
            and instant(revision.end_at) <= utc_now()
        )
        return {
            "ref": row.ref_id,
            "version": row.version,
            "work_ref": row.work_ref,
            **{key: getattr(row, key) for key in STATE_FIELDS},
            "meaning": "Revision-pinned display; accounting uses current WorkSession history.",
            "refresh": "docket_show_work_session" if refresh_supported else None,
            "hide": "docket_retract_record",
        }

    @staticmethod
    def check_version(row: WorkCalendarProjection, changeset: ChangeSet) -> None:
        if changeset.expected_versions.get(row.ref_id) != row.version:
            raise DocketError(
                code="work_calendar_version_conflict",
                message="Read current projection context and retry.",
                details={"ref": row.ref_id, "next_action": "docket_get_context"},
            )

    def retain(
        self, row: WorkCalendarProjection, changeset: ChangeSet, basis: list[str], reason: str
    ) -> None:
        revision = WorkCalendarRevision(
            projection_ref=row.ref_id,
            version=row.version,
            **{key: getattr(row, key) for key in STATE_FIELDS},
            reason=reason,
            basis_refs=basis,
            created_by_changeset_ref=changeset.ref_id,
        )
        self.session.add(revision)
        self.session.flush()
        self.session.add(
            AuditEvent(
                event_type="work_calendar.changed",
                entity_type="work_calendar_projection",
                entity_id=row.id,
                primary_ref=row.ref_id,
                actor_type="agent",
                affected_refs=[row.work_ref, row.event_ref, revision.ref_id],
                basis_refs=basis,
                data={
                    "version": row.version,
                    "reason": reason,
                    "created_by_changeset_ref": changeset.ref_id,
                },
            )
        )

    def invalidate(
        self, work: WorkSession, revision_ref: str, changeset: ChangeSet, basis: list[str]
    ) -> None:
        row = self.for_work(work.ref_id, lock=True)
        if row is None or not row.enabled or row.observed_work_revision_ref == revision_ref:
            return
        row.version += 1
        row.updated_at = utc_now()
        row.observed_work_revision_ref = revision_ref
        row.needs_refresh = True
        self.retain(
            row,
            changeset,
            basis,
            "source_retracted"
            if work.lifecycle == "retracted"
            else "source_superseded"
            if work.superseded
            else "source_corrected",
        )

    def render(self, work: WorkSession, lane: CalendarLane) -> StandaloneCalendarEventInput:
        spec = self.work.work_spec(work)
        value = spec.time
        if (
            work.superseded
            or work.lifecycle != "recorded"
            or value.precision not in {"interval", "timer"}
            or value.start_at is None
            or value.end_at is None
            or not value.duration_us
            or instant(value.end_at) > utc_now()
        ):
            raise invalid(
                "Display requires current recorded work with positive active time and completed "
                "clock bounds. Duration-only or incomplete work remains valid "
                "without a Calendar interval."
            )
        hours = format(Decimal(value.duration_us) / 3_600_000_000, ".6f").rstrip("0").rstrip(".")
        revision = self.work.work_revision(work)
        title = f"[Work snapshot v{work.version}, {hours} h] {spec.description}"[:512]
        timing = TimedEventTiming(
            kind="timed",
            start_local=instant(value.start_at).replace(tzinfo=None),
            end_local=instant(value.end_at).replace(tzinfo=None),
            timezone="UTC",
        )
        notes = (
            f"Actual-work report snapshot, not a scheduled commitment.\n"
            f"Work: {work.ref_id}\nRevision: {revision.ref_id}\nNet active hours: {hours}\n"
            f"Reported timezone: {value.timezone}\n"
            "The displayed span includes reported breaks; net active hours exclude them.\n"
            "Corrections in Docket mark this snapshot for refresh. Edit the work record in Docket; "
            "this Calendar display is not a second editable time or financial record."
        )
        return StandaloneCalendarEventInput(
            title=title,
            calendar_lane=lane.lane,
            timing=timing,
            location=spec.reported_location,
            notes=notes,
        )

    def apply(self, _session: Session, changeset: ChangeSet, change: Any) -> list[str]:
        if change.action == "retract":
            hidden = self.head(change.object_ref, lock=True)
            self.check_version(hidden, changeset)
            event = self.session.scalar(
                select(CanonicalEvent).where(CanonicalEvent.ref_id == hidden.event_ref)
            )
            if event is None or event.status != "cancelled":
                raise invalid("Hide the derived Event in the same exact proposal.")
            hidden.version += 1
            hidden.enabled, hidden.needs_refresh = False, False
            hidden.updated_at = utc_now()
            self.retain(hidden, changeset, list(change.basis_refs), "hidden")
            return [hidden.ref_id]
        spec = WorkCalendarSpec.model_validate(change.create_spec or change.payload)
        work = self.work.head(WorkSession, spec.work_ref, lock=True)
        if (
            work.version != spec.work_version
            or self.work.work_revision(work).ref_id != spec.source_revision_ref
        ):
            raise invalid(
                "The work changed. Read current work context before displaying its snapshot."
            )
        row = self.for_work(work.ref_id, lock=True)
        if change.action == "create" and row is not None:
            raise invalid("This work already has a projection; refresh its current reference.")
        if change.action != "create":
            if row is None or row.ref_id != change.object_ref:
                raise invalid("Select the work's existing projection.")
            self.check_version(row, changeset)
        assert spec.event_ref is not None
        event = self.session.scalar(
            select(CanonicalEvent).where(CanonicalEvent.ref_id == spec.event_ref)
        )
        lane = self.session.scalar(
            select(CalendarLane).where(CalendarLane.ref_id == spec.calendar_ref)
        )
        if event is None or lane is None or not lane.enabled:
            raise invalid("Select an enabled Calendar destination.", field="calendar_ref")
        expected = self.render(work, lane)
        if (
            event.status != "active"
            or event.lane_ref != lane.ref_id
            or event.title != expected.title
            or event.event_spec != expected.model_dump(mode="json")
            or not event.canonical_key.startswith(NAMESPACE + work.ref_id + ":")
            or event.context_labels != ["actual_work_snapshot"]
            or event.entity_refs
            or self.session.scalar(
                select(func.count())
                .select_from(EventItemLink)
                .where(EventItemLink.event_ref == event.ref_id)
            )
        ):
            raise invalid("The derived Event must match the exact work snapshot and destination.")
        if row is not None and row.event_ref != event.ref_id:
            previous_event = self.session.scalar(
                select(CanonicalEvent).where(CanonicalEvent.ref_id == row.event_ref)
            )
            if previous_event is None or previous_event.status != "cancelled":
                raise invalid("Cancel the previous display in the same exact projection proposal.")
        if row is None:
            row = WorkCalendarProjection(
                operator_ref=require_principal("commit").operator_ref,
                work_ref=work.ref_id,
                version=1,
                source_revision_ref=spec.source_revision_ref,
                observed_work_revision_ref=spec.source_revision_ref,
                event_ref=event.ref_id,
                calendar_ref=lane.ref_id,
                enabled=True,
                needs_refresh=False,
                created_by_changeset_ref=changeset.ref_id,
            )
            self.session.add(row)
            self.session.flush()
        else:
            row.version += 1
            row.source_revision_ref = row.observed_work_revision_ref = spec.source_revision_ref
            row.event_ref, row.calendar_ref, row.enabled, row.needs_refresh = (
                event.ref_id,
                lane.ref_id,
                True,
                False,
            )
            row.updated_at = utc_now()
        self.retain(row, changeset, list(change.basis_refs), "displayed")
        return [row.ref_id]


class WorkCalendarCommandService(NativeCommandService):
    def perform(
        self, command: str, arguments: dict[str, Any], request_ref: str, request_key: str
    ) -> dict[str, Any]:
        public = ShowWorkSession.model_validate(arguments)
        values = public.model_dump(mode="json", exclude_unset=True)

        def build(context: NativeContext) -> dict[str, Any]:
            service = WorkCalendarService(self.session)
            work = service.work.head(WorkSession, public.work_ref)
            lane = self.session.scalar(
                select(CalendarLane).where(CalendarLane.ref_id == public.calendar_ref)
            )
            if lane is None or not lane.enabled:
                raise invalid("Select an enabled Calendar destination.", field="calendar_ref")
            for row in (work, lane):
                if context.versions.get(row.ref_id) != row.version:
                    raise invalid(
                        "Read context for the selected work and Calendar destination.",
                        field="context_id",
                    )
            rendered = service.render(work, lane)
            revision = service.work.work_revision(work)
            projection = service.for_work(work.ref_id)
            versions = dict(context.versions)
            actions: list[BaseModel] = []
            old_event = None
            if projection:
                versions[projection.ref_id] = projection.version
                old_event = self.session.scalar(
                    select(CanonicalEvent).where(CanonicalEvent.ref_id == projection.event_ref)
                )
                assert old_event is not None
                versions[old_event.ref_id] = old_event.version
            if (
                projection
                and projection.enabled
                and old_event
                and projection.calendar_ref == lane.ref_id
            ):
                actions.append(
                    CanonicalEventModify(
                        change_id="actual-event",
                        action="update",
                        object_type="canonical_event",
                        object_ref=old_event.ref_id,
                        affected_fields=["event_spec"],
                        payload=CanonicalEventPatchSpec(
                            event_spec=EventFieldPatch(
                                title=rendered.title,
                                timing=rendered.timing.model_dump(mode="json"),
                                location=rendered.location,
                                notes=rendered.notes,
                            )
                        ),
                    )
                )
                event_fields = {"event_ref": old_event.ref_id}
            else:
                if projection and projection.enabled and old_event and old_event.status == "active":
                    actions.append(
                        CanonicalEventCancel(
                            change_id="retire-actual-event",
                            action="retract",
                            object_type="canonical_event",
                            object_ref=old_event.ref_id,
                            affected_fields=["status"],
                        )
                    )
                key = (
                    NAMESPACE
                    + work.ref_id
                    + ":"
                    + sha256_json([public.operation_id, require_principal("read").principal_ref])[
                        :20
                    ]
                )
                actions.append(
                    CanonicalEventCreate(
                        change_id="actual-event",
                        action="create",
                        object_type="canonical_event",
                        affected_fields=["title", "event_spec"],
                        create_spec=CanonicalEventCreateSpec(
                            canonical_key=key,
                            title=rendered.title,
                            event_spec=rendered,
                            lane_ref=lane.ref_id,
                            context_labels=["actual_work_snapshot"],
                        ),
                    )
                )
                actions.append(
                    LaneRoutingDecisionCreate(
                        change_id="actual-route",
                        action="create",
                        object_type="lane_routing_decision",
                        affected_fields=["lane_ref"],
                        create_spec=LaneRoutingDecisionCreateSpec(
                            lane_ref=lane.ref_id,
                            event_change_id="actual-event",
                            decision_kind="explicit_operator",
                            operator_confirmed=True,
                        ),
                    )
                )
                event_fields = {"event_change_id": "actual-event"}
            data = WorkCalendarSpec(
                work_ref=work.ref_id,
                work_version=work.version,
                source_revision_ref=revision.ref_id,
                calendar_ref=lane.ref_id,
                **event_fields,
            )
            variant = WorkCalendarModify if projection else WorkCalendarCreate
            actions.append(
                variant.model_validate(
                    {
                        "change_id": "actual-projection",
                        "action": "update" if projection else "create",
                        "object_type": "work_calendar_projection",
                        "object_ref": projection.ref_id if projection else None,
                        "affected_fields": ["source_revision_ref", "event_ref"],
                        "payload" if projection else "create_spec": data,
                    }
                )
            )
            observed = NativeContext(snapshot=context.snapshot, versions=versions)
            plan = mutation_plan(
                observed,
                [mutation_input_json(a, preserve_explicit_nulls=True) for a in actions],
                command,
                retained_refs={revision.ref_id},
            )
            plan["command_preview"] = {
                "work_ref": work.ref_id,
                "source_revision_ref": revision.ref_id,
                "title": rendered.title,
                "timing": rendered.timing.model_dump(mode="json"),
                "meaning": "Explicit revision-pinned snapshot; no added accounting hours.",
            }
            return plan

        return self.execute(
            command=command,
            arguments=values,
            request_ref=request_ref,
            request_key=request_key,
            build=build,
        )

    def hide(self, arguments: dict[str, Any], request_ref: str, request_key: str) -> dict[str, Any]:
        def build(context: NativeContext) -> dict[str, Any]:
            service = WorkCalendarService(self.session)
            row = service.head(arguments["target_ref"])
            if context.versions.get(row.ref_id) != row.version:
                raise invalid(
                    "Read current projection context before hiding it.", field="context_id"
                )
            event = self.session.scalar(
                select(CanonicalEvent).where(CanonicalEvent.ref_id == row.event_ref)
            )
            assert event is not None
            actions: list[BaseModel] = [
                WorkCalendarRetract(
                    change_id="actual-projection",
                    action="retract",
                    object_type="work_calendar_projection",
                    object_ref=row.ref_id,
                    affected_fields=["enabled"],
                )
            ]
            if event.status != "cancelled":
                actions.append(
                    CanonicalEventCancel(
                        change_id="actual-event",
                        action="retract",
                        object_type="canonical_event",
                        object_ref=event.ref_id,
                        affected_fields=["status"],
                    )
                )
            observed = NativeContext(
                snapshot=context.snapshot,
                versions={**context.versions, event.ref_id: event.version},
            )
            return mutation_plan(
                observed, [mutation_input_json(a) for a in actions], "docket_retract_record"
            )

        return self.execute(
            command="docket_retract_record",
            arguments=arguments,
            request_ref=request_ref,
            request_key=request_key,
            build=build,
        )
