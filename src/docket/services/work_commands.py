"""Compile flat work fields into the existing exact-proposal command engine."""

from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from docket.domain.errors import DocketError
from docket.domain.work_time import normalize_work_time, work_instant
from docket.models import Engagement, NativeContext, WorkSession
from docket.schemas.authority import (
    EngagementCreate,
    EngagementModify,
    WorkSessionCreate,
    WorkSessionModify,
    mutation_input_json,
)
from docket.schemas.work import (
    CreateEngagement,
    EngagementSpec,
    LogWorkSession,
    UpdateEngagement,
    UpdateWorkSession,
    WorkSessionSpec,
    WorkTimeFields,
)
from docket.services.business_commands import _named_refs, mutation_plan
from docket.services.native_commands import NativeCommandService
from docket.services.work_attribution import WorkAttributionService, frozen_time, raw_time

WORK_COMMAND_MODELS: dict[str, type[BaseModel]] = {
    "docket_create_engagement": CreateEngagement,
    "docket_update_engagement": UpdateEngagement,
    "docket_log_work_session": LogWorkSession,
    "docket_update_work_session": UpdateWorkSession,
}


class WorkCommandService(NativeCommandService):
    def perform(
        self, command: str, arguments: dict[str, Any], request_ref: str, request_key: str
    ) -> dict[str, Any]:
        public = WORK_COMMAND_MODELS[command].model_validate(arguments)
        values = public.model_dump(mode="json", exclude_unset=True)
        service = WorkAttributionService(self.session)

        def build(context: NativeContext) -> dict[str, Any]:
            mutation: BaseModel
            retained_refs: set[str] = set()
            if command in {"docket_create_engagement", "docket_update_engagement"}:
                ref = (
                    values.get("engagement_ref") if command == "docket_update_engagement" else None
                )
                fields = (
                    service.engagement_spec(service.head(Engagement, ref)).model_dump()
                    if ref
                    else {}
                )
                patch = {
                    key: value
                    for key, value in values.items()
                    if key in EngagementSpec.model_fields
                }
                spec = EngagementSpec.model_validate({**fields, **patch})
                service.validate_links(spec)
                variant = EngagementModify if ref else EngagementCreate
                mutation = variant.model_validate(
                    {
                        "change_id": "engagement",
                        "action": "update" if ref else "create",
                        "object_type": "engagement",
                        "object_ref": ref,
                        "affected_fields": list(patch) or ["label"],
                        "payload" if ref else "create_spec": spec,
                    }
                )
            else:
                ref = values.get("work_ref")
                previous = service.work_spec(service.head(WorkSession, ref)) if ref else None
                if previous:
                    retained_refs = _named_refs(previous.model_dump(mode="json"))
                fields = previous.model_dump() if previous else {}
                patch = {
                    key: value
                    for key, value in values.items()
                    if key in WorkSessionSpec.model_fields
                }
                time_patch = {
                    key: value
                    for key, value in values.items()
                    if key in WorkTimeFields.model_fields
                }
                if (
                    previous
                    and previous.status in {"running", "paused"}
                    and previous.time.precision == "timer"
                    and time_patch
                ):
                    raise DocketError(
                        code="timer_command_required",
                        message="Stop the timer before correcting its bounds.",
                    )
                if previous and not time_patch:
                    time = previous.time
                elif (
                    previous
                    and previous.time.precision == "composite"
                    and set(time_patch) <= {"timezone"}
                ):
                    time = previous.time.model_copy(
                        update={"timezone": time_patch["timezone"] or self.settings.timezone}
                    )
                else:
                    raw = (
                        raw_time(previous.time)
                        if previous
                        else {
                            "timezone": self.settings.timezone,
                            "start": None,
                            "end": None,
                            "duration_minutes": None,
                            "breaks": [],
                        }
                    )
                    # Supplied duration selects unbounded time; bounds select an interval.
                    if time_patch.get("duration_minutes") is not None:
                        raw.update(start=None, end=None, breaks=[])
                    elif time_patch.get("start") is not None or time_patch.get("end") is not None:
                        raw["duration_minutes"] = None
                    raw.update(time_patch)
                    raw["timezone"] = raw["timezone"] or self.settings.timezone
                    if raw.get("duration_minutes") is not None:
                        raw["duration_minutes"] = Decimal(str(raw["duration_minutes"]))
                    raw["breaks"] = [
                        (b["start"], b["end"]) if isinstance(b, dict) else b
                        for b in (raw["breaks"] or [])
                    ]
                    if previous and previous.time.precision == "timer":
                        if raw["end"] is None and raw["duration_minutes"] is None:
                            raise DocketError(
                                code="work_end_required",
                                message="Provide end or duration to correct an abandoned timer.",
                                details={"field": "end"},
                            )
                        if raw["end"] is not None:
                            final = work_instant(raw["end"], raw["timezone"], "end").instant
                            raw["breaks"] = [
                                (a, b or raw["end"])
                                for a, b in raw["breaks"]
                                if b is not None
                                or work_instant(a, raw["timezone"], "breaks.start").instant != final
                            ]
                    time = frozen_time(normalize_work_time(**raw), raw["timezone"])
                if time.precision == "interval":
                    # Known bounds replace earlier unbounded placement; callers need not
                    # reconstruct internal cleanup. Explicit conflicting placement still errors.
                    for key in ("work_date", "period_start", "period_end"):
                        if key not in patch:
                            fields.pop(key, None)
                if (
                    time_patch
                    and previous
                    and previous.source_method == "timer"
                    and "source_method" not in patch
                ):
                    patch["source_method"] = "report"
                work_spec = WorkSessionSpec.model_validate({**fields, **patch, "time": time})
                service.validate_links(work_spec)
                work_variant = WorkSessionModify if ref else WorkSessionCreate
                mutation = work_variant.model_validate(
                    {
                        "change_id": "work",
                        "action": "update" if ref else "create",
                        "object_type": "work_session",
                        "object_ref": ref,
                        "affected_fields": list(patch) + list(time_patch) or ["description"],
                        "payload" if ref else "create_spec": work_spec,
                    }
                )
            if ref and not patch and not (command == "docket_update_work_session" and time_patch):
                raise DocketError(
                    code="empty_work_update", message="Supply at least one changed field."
                )
            return mutation_plan(
                context, [mutation_input_json(mutation)], command, retained_refs=retained_refs
            )

        return self.execute(
            command=command,
            arguments=values,
            request_ref=request_ref,
            request_key=request_key,
            build=build,
        )
