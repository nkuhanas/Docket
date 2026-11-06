"""Typed performed-work effects applied inside the shared canonical transaction."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from docket.agent_auth import agent_call_context, require_principal
from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.domain.work_time import WorkTime, elapsed_us, normalize_work_time, work_zone
from docket.models import (
    Affiliation,
    AuditEvent,
    CanonicalEvent,
    ChangeSet,
    Engagement,
    EngagementRevision,
    Entity,
    Item,
    Source,
    Task,
    WorkLineageEdge,
    WorkSegment,
    WorkSession,
    WorkSessionRevision,
)
from docket.models.base import utc_now
from docket.schemas.work import EngagementSpec, WorkMetadata, WorkSessionSpec, WorkTimeSpec
from docket.services.work_quantities import WorkQuantityService, WorkSpan


def frozen_time(value: WorkTime, zone: str) -> WorkTimeSpec:
    return WorkTimeSpec.model_validate(
        {
            "precision": value.precision,
            "timezone": zone,
            "start_at": value.start.instant if value.start else None,
            "end_at": value.end.instant if value.end else None,
            "start_offset_seconds": value.start.offset_seconds if value.start else None,
            "end_offset_seconds": value.end.offset_seconds if value.end else None,
            "duration_us": value.duration_us,
            "segments": [
                {
                    "kind": segment.kind,
                    "start_at": segment.start.instant,
                    "end_at": segment.end.instant,
                    "start_offset_seconds": segment.start.offset_seconds,
                    "end_offset_seconds": segment.end.offset_seconds,
                }
                for segment in value.intervals
            ],
        }
    )


def reported_bound(value: datetime | None, offset: int | None) -> str | None:
    if value is None:
        return None
    if offset is None or abs(offset) >= 86400:
        raise DocketError(code="invalid_work_offset", message="Retain the supplied UTC offset.")
    # SQLite omits tzinfo on read; all persisted instants are UTC.
    return (
        value.replace(tzinfo=value.tzinfo or UTC)
        .astimezone(
            timezone(timedelta(seconds=offset)),
        )
        .isoformat()
    )


def utc_instant(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=value.tzinfo or UTC) if value else None


def read_operator() -> str:
    """Internal history readers retain configured-owner scope without an agent session."""
    return (
        require_principal("read").operator_ref
        if agent_call_context.get() is not None
        else get_settings().canonical_operator_ref
    )


def raw_time(value: WorkTimeSpec) -> dict[str, Any]:
    return {
        "start": reported_bound(value.start_at, value.start_offset_seconds),
        "end": reported_bound(value.end_at, value.end_offset_seconds),
        "timezone": value.timezone,
        "duration_minutes": (
            Decimal(value.duration_us) / 60_000_000
            if value.precision == "duration" and value.duration_us is not None
            else None
        ),
        "breaks": [
            (
                reported_bound(s.start_at, s.start_offset_seconds),
                reported_bound(s.end_at, s.end_offset_seconds),
            )
            for s in value.segments
            if s.kind == "break"
        ],
    }


class WorkAttributionService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def handlers(self) -> dict[str, Any]:
        return {"engagement": self.apply_engagement, "work_session": self.apply_work}

    def head(self, model: type[Any], ref: str, *, lock: bool = False) -> Any:
        statement = select(model).where(
            model.ref_id == ref,
            model.operator_ref == read_operator(),
        )
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        row = self.session.scalar(statement)
        if row is None:
            raise DocketError(
                code="work_record_unavailable",
                message="Select an owned work record.",
                details={"ref": ref},
            )
        return row

    def engagement_spec(self, head: Engagement) -> EngagementSpec:
        row = self.session.scalar(
            select(EngagementRevision).where(
                EngagementRevision.engagement_ref == head.ref_id,
                EngagementRevision.revision == head.version,
            )
        )
        assert row is not None
        return EngagementSpec.model_validate(
            {key: getattr(row, key) for key in EngagementSpec.model_fields}
        )

    def work_spec(self, head: WorkSession) -> WorkSessionSpec:
        return self.revision_spec(self.work_revision(head))

    def work_revision(self, head: WorkSession) -> WorkSessionRevision:
        row = self.session.scalar(
            select(WorkSessionRevision).where(
                WorkSessionRevision.work_ref == head.ref_id,
                WorkSessionRevision.revision == head.version,
            )
        )
        assert row is not None
        return row

    def revision_spec(self, row: WorkSessionRevision) -> WorkSessionSpec:
        segments = list(
            self.session.scalars(
                select(WorkSegment)
                .where(
                    WorkSegment.revision_id == row.id,
                )
                .order_by(WorkSegment.ordinal)
            )
        )
        return WorkSessionSpec.model_validate(
            {
                **{key: getattr(row, key) for key in WorkMetadata.model_fields},
                "description": row.description,
                "status": row.lifecycle,
                "time": {
                    "precision": row.time_precision,
                    "timezone": row.timezone,
                    "start_at": utc_instant(row.start_at),
                    "end_at": utc_instant(row.end_at),
                    "start_offset_seconds": row.start_offset_seconds,
                    "end_offset_seconds": row.end_offset_seconds,
                    "duration_us": row.duration_us,
                    "segments": [
                        {
                            "kind": s.kind,
                            "start_at": utc_instant(s.start_at),
                            "end_at": utc_instant(s.end_at),
                            "start_offset_seconds": s.start_offset_seconds,
                            "end_offset_seconds": s.end_offset_seconds,
                        }
                        for s in segments
                    ],
                },
            }
        )

    def view(self, head: Engagement | WorkSession) -> dict[str, Any]:
        if isinstance(head, Engagement):
            return self.engagement_spec(head).model_dump(mode="json", exclude_none=True)
        revision = self.work_revision(head)
        spec = self.revision_spec(revision)
        state = spec.model_dump(mode="json", exclude_none=True, exclude={"time"})
        raw = raw_time(spec.time)
        raw["breaks"] = [{"start": start, "end": end} for start, end in raw["breaks"]]
        state.update({key: value for key, value in raw.items() if value is not None})
        if state.get("duration_minutes") is not None:
            state["duration_minutes"] = str(state["duration_minutes"])
        state["time_precision"] = spec.time.precision
        state["duration_us"] = spec.time.duration_us
        state["revision_ref"] = revision.ref_id
        state["superseded"] = head.superseded
        parent_revision = aliased(WorkSessionRevision)
        child_revision = aliased(WorkSessionRevision)
        for direction, target, selected in (
            ("parents", parent_revision, child_revision),
            ("children", child_revision, parent_revision),
        ):
            refs = self.session.scalars(
                select(target.work_ref)
                .select_from(WorkLineageEdge)
                .join(
                    parent_revision, parent_revision.ref_id == WorkLineageEdge.parent_revision_ref
                )
                .join(child_revision, child_revision.ref_id == WorkLineageEdge.child_revision_ref)
                .where(selected.work_ref == head.ref_id)
                .distinct()
                .order_by(target.work_ref)
                .limit(26)
            ).all()
            if refs:
                state.setdefault("lineage", {})[direction] = list(refs[:25])
                if len(refs) > 25:
                    state["lineage"]["complete"] = False
        if spec.time.precision == "timer" and spec.status in {"running", "paused"}:
            observed = utc_now()
            state["as_of"] = observed.isoformat()
            state["estimated_duration_us"] = (spec.time.duration_us or 0) + sum(
                max(0, elapsed_us(observed - segment.start_at))
                for segment in spec.time.segments
                if segment.kind == "active" and segment.end_at is None
            )
        return state

    def validate_links(self, spec: EngagementSpec | WorkSessionSpec) -> None:
        kinds = {
            "organization_ref": {"organization", "institution"},
            "payer_ref": {"organization", "institution", "person"},
            "project_ref": {"project"},
            "place_ref": {"place"},
        }
        for key, allowed in kinds.items():
            ref = getattr(spec, key, None)
            if ref:
                row = self.session.scalar(select(Entity).where(Entity.ref_id == ref))
                if (
                    row is None
                    or row.entity_kind not in allowed
                    or row.canonical_status == "retracted"
                ):
                    raise DocketError(
                        code="invalid_work_reference",
                        message="Select an available reference of the required kind.",
                        details={"field": key, "allowed_kinds": sorted(allowed)},
                    )
        for key, model in (
            ("affiliation_ref", Affiliation),
            ("item_ref", Item),
            ("task_ref", Task),
            ("event_ref", CanonicalEvent),
        ):
            ref = getattr(spec, key, None)
            if ref and self.session.scalar(select(model).where(model.ref_id == ref)) is None:
                raise DocketError(
                    code="invalid_work_reference",
                    message="Select an existing record.",
                    details={"field": key},
                )
        if isinstance(spec, WorkSessionSpec):
            if spec.engagement_ref:
                engagement = self.engagement_spec(self.head(Engagement, spec.engagement_ref))
                if (
                    spec.organization_ref
                    and engagement.organization_ref
                    and spec.organization_ref != engagement.organization_ref
                ):
                    raise DocketError(
                        code="work_attribution_conflict",
                        message="Organization conflicts with the selected engagement.",
                        details={"field": "organization_ref"},
                    )
            for ref in spec.source_refs:
                if self.session.scalar(select(Source).where(Source.ref_id == ref)) is None:
                    raise DocketError(
                        code="invalid_work_reference",
                        message="Select retained source evidence.",
                        details={"field": "source_refs"},
                    )

    @staticmethod
    def check_version(head: Any, changeset: ChangeSet) -> None:
        if changeset.expected_versions.get(head.ref_id) != head.version:
            raise DocketError(
                code="work_version_conflict",
                message="Read current work context and retry.",
                details={"ref": head.ref_id, "next_action": "docket_get_work_context"},
            )

    def audit(self, head: Any, revision_ref: str, changeset: ChangeSet, change: Any) -> None:
        self.session.add(
            AuditEvent(
                event_type=change.object_type + "." + change.action,
                entity_type=change.object_type,
                entity_id=head.id,
                actor_type="operator",
                actor_id=require_principal("commit").operator_ref,
                primary_ref=head.ref_id,
                affected_refs=[head.ref_id, revision_ref],
                basis_refs=list(change.basis_refs),
                data={
                    "changeset_ref": changeset.ref_id,
                    "revision_ref": revision_ref,
                    "change_id": change.change_id,
                    "version": head.version,
                },
            )
        )

    def apply_engagement(self, session: Session, changeset: ChangeSet, change: Any) -> list[str]:
        spec = EngagementSpec.model_validate(change.create_spec or change.payload)
        self.validate_links(spec)
        if change.action == "create":
            head = Engagement(
                operator_ref=require_principal("commit").operator_ref,
                created_by_changeset_ref=changeset.ref_id,
                version=1,
            )
            session.add(head)
            session.flush()
        else:
            head = self.head(Engagement, change.object_ref, lock=True)
            self.check_version(head, changeset)
            head.version += 1
            head.updated_at = utc_now()
        revision = EngagementRevision(
            engagement_ref=head.ref_id,
            revision=head.version,
            **spec.model_dump(),
            basis_refs=list(change.basis_refs),
            created_by_changeset_ref=changeset.ref_id,
        )
        session.add(revision)
        session.flush()
        self.audit(head, revision.ref_id, changeset, change)
        return [head.ref_id]

    def apply_work(self, session: Session, changeset: ChangeSet, change: Any) -> list[str]:
        if change.mutation_type in {"work_session_split", "work_session_merge"}:
            from docket.services.work_lineage import apply_lineage

            return apply_lineage(self, changeset, change)
        if change.mutation_type.startswith("work_timer_"):
            from docket.services.work_timers import apply_timer

            return apply_timer(self, changeset, change)
        spec = WorkSessionSpec.model_validate(change.create_spec or change.payload)
        head = None
        if change.action == "create":
            if spec.status in {"running", "paused"} or spec.time.precision == "timer":
                raise DocketError(
                    code="timer_command_required", message="Use the timer start command."
                )
        else:
            head = self.head(WorkSession, change.object_ref, lock=True)
            self.check_version(head, changeset)
            if head.superseded:
                raise DocketError(code="work_transition_required", message="Select current work.")
            if head.lifecycle in {"running", "paused"}:
                previous = self.work_spec(head)
                if (
                    spec.time != previous.time
                    or spec.status not in {previous.status, "retracted"}
                    or spec.source_method != "timer"
                ):
                    raise DocketError(
                        code="timer_command_required",
                        message="Use a timer transition to change active time.",
                    )
        return self.persist_work(changeset, change, spec, head=head)

    def persist_work(
        self,
        changeset: ChangeSet,
        change: Any,
        spec: WorkSessionSpec,
        *,
        head: WorkSession | None = None,
        superseded: bool = False,
        quantity_spans: list[WorkSpan] | None = None,
    ) -> list[str]:
        self.validate_links(spec)
        validate_work_time(spec)
        session = self.session
        quantities = WorkQuantityService(session)
        if head is not None:
            quantities.ensure_history(head, changeset.ref_id)
        if head is None:
            head = WorkSession(
                operator_ref=require_principal("commit").operator_ref,
                created_by_changeset_ref=changeset.ref_id,
                version=1,
                lifecycle=spec.status,
                superseded=False,
            )
            session.add(head)
            session.flush()
        else:
            head.version += 1
            head.updated_at = utc_now()
            head.lifecycle = spec.status
        head.superseded = superseded
        metadata = spec.model_dump(exclude={"time", "status"})
        revision = WorkSessionRevision(
            work_ref=head.ref_id,
            revision=head.version,
            lifecycle=head.lifecycle,
            superseded=head.superseded,
            **metadata,
            time_precision=spec.time.precision,
            timezone=spec.time.timezone,
            start_at=spec.time.start_at,
            end_at=spec.time.end_at,
            start_offset_seconds=spec.time.start_offset_seconds,
            end_offset_seconds=spec.time.end_offset_seconds,
            duration_us=spec.time.duration_us,
            basis_refs=list(change.basis_refs),
            created_by_changeset_ref=changeset.ref_id,
        )
        session.add(revision)
        session.flush()
        for ordinal, segment in enumerate(spec.time.segments):
            session.add(
                WorkSegment(revision_id=revision.id, ordinal=ordinal, **segment.model_dump())
            )
        session.flush()
        quantities.record(head, revision, changeset.ref_id, spans=quantity_spans)
        self.audit(head, revision.ref_id, changeset, change)
        return [head.ref_id]


def validate_work_time(spec: WorkSessionSpec) -> None:
    """Validate normalized segments without reinterpreting time or inventing gaps."""
    value = spec.time
    work_zone(value.timezone)
    valid = True
    if value.precision == "duration":
        valid = (
            value.duration_us is not None
            and value.duration_us > 0
            and not value.segments
            and value.start_at is None
            and value.end_at is None
            and value.start_offset_seconds is None
            and value.end_offset_seconds is None
        )
    elif value.precision == "incomplete":
        valid = all(segment.end_at is not None for segment in value.segments)
        if valid:
            valid = frozen_time(normalize_work_time(**raw_time(value)), value.timezone) == value
    else:
        timer = value.precision == "timer"
        valid = value.start_at is not None and bool(value.segments)
        if timer:
            valid = (
                valid
                and spec.status in {"running", "paused", "retracted"}
                and spec.source_method == "timer"
                and value.end_at is None
                and value.end_offset_seconds is None
            )
        else:
            valid = valid and value.end_at is not None and spec.status in {"recorded", "retracted"}
        total, cursor = 0, value.start_at
        for index, segment in enumerate(value.segments):
            valid = (
                valid
                and segment.start_at == cursor
                and segment.start_at.tzinfo is not None
                and abs(segment.start_offset_seconds) < 86400
            )
            if not valid:
                break
            if segment.end_at is None:
                valid = (
                    valid
                    and timer
                    and index == len(value.segments) - 1
                    and segment.end_offset_seconds is None
                    and (
                        spec.status == "retracted"
                        or segment.kind == ("active" if spec.status == "running" else "break")
                    )
                )
            else:
                valid = (
                    valid
                    and segment.end_at.tzinfo is not None
                    and segment.end_at >= segment.start_at
                    and segment.end_offset_seconds is not None
                    and abs(segment.end_offset_seconds) < 86400
                )
                if not valid:
                    break
                if segment.kind == "active":
                    total += elapsed_us(segment.end_at - segment.start_at)
            cursor = segment.end_at
        valid = (
            valid
            and cursor == value.end_at
            and total == value.duration_us
            and value.start_offset_seconds == value.segments[0].start_offset_seconds
            and (timer or value.end_offset_seconds == value.segments[-1].end_offset_seconds)
        )
    if spec.status in {"running", "paused"} and value.precision != "timer":
        valid = False
    if not valid:
        raise DocketError(
            code="invalid_work_time_snapshot",
            message="Work time must retain its exact normalized basis.",
        )
