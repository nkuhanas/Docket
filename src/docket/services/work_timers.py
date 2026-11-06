"""Frozen timer proposals; canonical handlers validate and apply them atomically."""

import hashlib
from datetime import datetime
from typing import Any, Literal, cast

from pydantic import BaseModel
from sqlalchemy import func, select

from docket.agent_auth import require_principal
from docket.domain.errors import DocketError
from docket.domain.work_time import elapsed_us, work_instant, work_zone
from docket.models import AuthenticatedRequest, NativeContext, WorkSession
from docket.schemas.authority import (
    WorkTimerStart,
    WorkTimerSwitch,
    WorkTimerTransition,
    mutation_input_json,
)
from docket.schemas.work import (
    StartWorkSession,
    StopWorkSession,
    SwitchWorkSession,
    WorkSessionSpec,
    WorkTimerStartSpec,
    WorkTimerSwitchSpec,
    WorkTimerTarget,
    WorkTimerTransitionSpec,
)
from docket.services.business_commands import mutation_plan
from docket.services.native_commands import NativeCommandService
from docket.services.work_attribution import WorkAttributionService, utc_instant

TIMER_COMMAND_MODELS: dict[str, type[BaseModel]] = {
    "docket_start_work_session": StartWorkSession,
    "docket_pause_work_session": WorkTimerTarget,
    "docket_resume_work_session": WorkTimerTarget,
    "docket_stop_work_session": StopWorkSession,
    "docket_switch_work_session": SwitchWorkSession,
}


def offset_at(instant: datetime, zone: str) -> int:
    value = instant.astimezone(work_zone(zone)).utcoffset()
    assert value is not None
    return elapsed_us(value) // 1_000_000


def timer_start(values: dict[str, Any], effective: datetime, zone: str) -> WorkSessionSpec:
    offset = offset_at(effective, zone)
    metadata = {key: value for key, value in values.items() if key in WorkSessionSpec.model_fields}
    return WorkSessionSpec.model_validate(
        {
            **metadata,
            "source_method": "timer",
            "status": "running",
            "time": {
                "precision": "timer",
                "timezone": zone,
                "start_at": effective,
                "start_offset_seconds": offset,
                "duration_us": 0,
                "segments": [
                    {"kind": "active", "start_at": effective, "start_offset_seconds": offset}
                ],
            },
        }
    )


def timer_transition(
    previous: WorkSessionSpec,
    transition: Literal["pause", "resume", "stop"],
    effective: datetime,
    offset: int,
    *,
    outcome: Any = ...,
    reported_end: bool = False,
) -> WorkSessionSpec:
    if reported_end and transition != "stop":
        raise DocketError(
            code="invalid_timer_transition", message="Only stopping accepts a reported end."
        )
    if previous.status not in {"running", "paused"} or previous.time.precision != "timer":
        raise DocketError(
            code="timer_not_open",
            message="Select the current open timer.",
            details={"next_action": "docket_get_work_context"},
        )
    if (transition == "pause" and previous.status != "running") or (
        transition == "resume" and previous.status != "paused"
    ):
        raise DocketError(
            code="invalid_timer_transition",
            message="Use the current timer's allowed action.",
            details={"status": previous.status, "next_action": "docket_get_work_context"},
        )
    segments = [segment.model_dump() for segment in previous.time.segments]
    last = segments[-1]
    if effective < last["start_at"] or last["end_at"] is not None:
        raise DocketError(
            code="timer_effective_time_conflict",
            message="The retained instant precedes the current segment. Start a new command.",
            details={"field": "operation_id"},
        )
    last.update(end_at=effective, end_offset_seconds=offset)
    if transition != "stop":
        segments.append(
            {
                "kind": "break" if transition == "pause" else "active",
                "start_at": effective,
                "end_at": None,
                "start_offset_seconds": offset,
                "end_offset_seconds": None,
            }
        )
    time = {
        **previous.time.model_dump(),
        "segments": segments,
        "duration_us": sum(
            elapsed_us(s["end_at"] - s["start_at"])
            for s in segments
            if s["kind"] == "active" and s["end_at"] is not None
        ),
    }
    if transition == "stop":
        time.update(precision="interval", end_at=effective, end_offset_seconds=offset)
    snapshot = {
        **previous.model_dump(),
        "time": time,
        "status": {"pause": "paused", "resume": "running", "stop": "recorded"}[transition],
    }
    if outcome is not ...:
        snapshot["outcome"] = outcome
    if reported_end:
        snapshot["source_method"] = "report"
    return WorkSessionSpec.model_validate(snapshot)


class WorkTimerCommandService(NativeCommandService):
    def perform(
        self, command: str, arguments: dict[str, Any], request_ref: str, request_key: str
    ) -> dict[str, Any]:
        public = TIMER_COMMAND_MODELS[command].model_validate(arguments)
        values = public.model_dump(mode="json", exclude_unset=True)
        service = WorkAttributionService(self.session)

        def build(context: NativeContext) -> dict[str, Any]:
            mutation: BaseModel
            request = self.session.scalar(
                select(AuthenticatedRequest).where(
                    AuthenticatedRequest.ref_id == request_ref,
                )
            )
            assert request is not None
            effective = utc_instant(request.admitted_at)
            assert effective is not None
            request_at = effective
            zone = values.get("timezone") or request.admitted_timezone or self.settings.timezone
            data: dict[str, Any] = {
                "change_id": "timer",
                "object_type": "work_session",
                "affected_fields": [key for key in values if key in WorkSessionSpec.model_fields]
                + ["time", "status"]
                + (["outcome"] if "previous_outcome" in values else []),
            }
            if command == "docket_start_work_session":
                snapshot = timer_start(values, effective, zone)
                service.validate_links(snapshot)
                mutation = WorkTimerStart.model_validate(
                    {
                        **data,
                        "action": "create",
                        "create_spec": {"effective_at": effective, "snapshot": snapshot},
                    }
                )
            else:
                ref = values.get("from_work_ref") or values["work_ref"]
                previous = service.work_spec(service.head(WorkSession, ref))
                offset = offset_at(effective, previous.time.timezone)
                if command == "docket_stop_work_session" and values.get("end") is not None:
                    reported = work_instant(values["end"], previous.time.timezone, "end")
                    if reported.instant > request_at:
                        raise DocketError(
                            code="future_timer_stop",
                            message="Use an observed stop time, or stop later.",
                            details={"field": "end"},
                        )
                    if reported.instant < previous.time.segments[-1].start_at:
                        raise DocketError(
                            code="invalid_timer_end",
                            message="end precedes the current segment; correct its history.",
                            details={"field": "end"},
                        )
                    effective, offset = reported.instant, reported.offset_seconds
                if command == "docket_switch_work_session":
                    stopped = timer_transition(
                        previous,
                        "stop",
                        effective,
                        offset,
                        **(
                            {"outcome": values["previous_outcome"]}
                            if "previous_outcome" in values
                            else {}
                        ),
                    )
                    snapshot = timer_start(values, effective, zone)
                    service.validate_links(snapshot)
                    mutation = WorkTimerSwitch.model_validate(
                        {
                            **data,
                            "action": "update",
                            "object_ref": ref,
                            "payload": {
                                "effective_at": effective,
                                "snapshot": snapshot,
                                "previous_snapshot": stopped,
                                "previous_offset_seconds": offset,
                            },
                        }
                    )
                else:
                    transition = cast(
                        Literal["pause", "resume", "stop"],
                        {
                            "docket_pause_work_session": "pause",
                            "docket_resume_work_session": "resume",
                            "docket_stop_work_session": "stop",
                        }[command],
                    )
                    proposal = WorkTimerTransitionSpec.model_validate(
                        {
                            "transition": transition,
                            "effective_at": effective,
                            "offset_seconds": offset,
                            "reported_end": values.get("end") is not None,
                            "snapshot": timer_transition(
                                previous,
                                transition,
                                effective,
                                offset,
                                reported_end=values.get("end") is not None,
                                **({"outcome": values["outcome"]} if "outcome" in values else {}),
                            ),
                        }
                    )
                    snapshot = proposal.snapshot
                    mutation = WorkTimerTransition.model_validate(
                        {**data, "action": "update", "object_ref": ref, "payload": proposal}
                    )
            plan = mutation_plan(context, [mutation_input_json(mutation)], command)
            if command == "docket_switch_work_session":
                plan["assembly_scope"]["planned_create_types"] = ["work_session"]
            plan["command_preview"] = {
                "request_at": request_at.isoformat(),
                "effective_at": effective.isoformat(),
                "timer_status": snapshot.status,
                "duration_us": snapshot.time.duration_us,
            }
            return plan

        return self.execute(
            command=command,
            arguments=values,
            request_ref=request_ref,
            request_key=request_key,
            build=build,
        )


def timer_lock(service: WorkAttributionService) -> None:
    operator = require_principal("commit").operator_ref
    if service.session.get_bind().dialect.name == "postgresql":
        key = int.from_bytes(
            hashlib.sha256(("docket.work.timer:" + operator).encode()).digest()[:8],
            "big",
            signed=True,
        )
        service.session.execute(select(func.pg_advisory_xact_lock(key)))


def require_empty_timer(service: WorkAttributionService) -> None:
    current = service.session.scalar(
        select(WorkSession)
        .where(
            WorkSession.operator_ref == require_principal("commit").operator_ref,
            WorkSession.lifecycle.in_(["running", "paused"]),
            WorkSession.superseded.is_(False),
        )
        .limit(1)
    )
    if current is not None:
        raise DocketError(
            code="primary_timer_exists",
            message="A primary timer is already open.",
            details={
                "work_ref": current.ref_id,
                "status": current.lifecycle,
                "next_action": "docket_get_work_context",
                "available_action": "docket_switch_work_session",
            },
        )


def apply_timer(service: WorkAttributionService, changeset: Any, change: Any) -> list[str]:
    timer_lock(service)
    if change.mutation_type == "work_timer_start":
        proposal = WorkTimerStartSpec.model_validate(change.create_spec)
        require_empty_timer(service)
        snapshot = proposal.snapshot
        expected = timer_start(snapshot.model_dump(), proposal.effective_at, snapshot.time.timezone)
        if expected != snapshot:
            raise DocketError(
                code="invalid_timer_snapshot", message="Start the exact retained timer proposal."
            )
        return service.persist_work(changeset, change, snapshot)
    head = service.head(WorkSession, change.object_ref, lock=True)
    service.check_version(head, changeset)
    previous = service.work_spec(head)
    if head.superseded:
        raise DocketError(code="work_transition_required", message="Select current work.")
    if change.mutation_type == "work_timer_switch":
        proposal = WorkTimerSwitchSpec.model_validate(change.payload)
        stopped = timer_transition(
            previous,
            "stop",
            proposal.effective_at,
            proposal.previous_offset_seconds,
            outcome=proposal.previous_snapshot.outcome,
        )
        started = timer_start(
            proposal.snapshot.model_dump(), proposal.effective_at, proposal.snapshot.time.timezone
        )
        if stopped != proposal.previous_snapshot or started != proposal.snapshot:
            raise DocketError(
                code="invalid_timer_snapshot", message="Apply the exact retained switch proposal."
            )
        refs = service.persist_work(changeset, change, stopped, head=head)
        require_empty_timer(service)
        return [*refs, *service.persist_work(changeset, change, started)]
    proposal = WorkTimerTransitionSpec.model_validate(change.payload)
    expected = timer_transition(
        previous,
        proposal.transition,
        proposal.effective_at,
        proposal.offset_seconds,
        outcome=proposal.snapshot.outcome,
        reported_end=proposal.reported_end,
    )
    if expected != proposal.snapshot:
        raise DocketError(
            code="invalid_timer_snapshot", message="Apply the exact retained timer transition."
        )
    return service.persist_work(changeset, change, proposal.snapshot, head=head)
