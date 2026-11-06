"""Atomic split/merge business operations with server-derived work-unit lineage."""

from typing import Any

from pydantic import BaseModel
from sqlalchemy import select

from docket.domain.errors import DocketError
from docket.domain.work_time import elapsed_us, normalize_work_time, work_instant
from docket.models import ChangeSet, NativeContext, WorkLineageEdge, WorkQuantityOrigin, WorkSession
from docket.schemas.authority import WorkSessionMerge, WorkSessionSplit, mutation_input_json
from docket.schemas.work import (
    MergeWorkSessions,
    SplitWorkSession,
    WorkLineageSpec,
    WorkSegmentSpec,
    WorkSessionSpec,
    WorkSplitPart,
    WorkTimeSpec,
)
from docket.services.business_commands import _named_refs, mutation_plan
from docket.services.native_commands import NativeCommandService
from docket.services.work_attribution import WorkAttributionService, frozen_time, validate_work_time
from docket.services.work_quantities import WorkQuantityService, WorkSpan, slice_spans

LINEAGE_COMMAND_MODELS: dict[str, type[BaseModel]] = {
    "docket_split_work_session": SplitWorkSession,
    "docket_merge_work_sessions": MergeWorkSessions,
}


def invalid(field: str, message: str) -> DocketError:
    return DocketError(code="work_lineage_invalid", message=message, details={"field": field})


def recorded(
    service: WorkAttributionService, refs: list[str], *, lock: bool = False
) -> list[WorkSession]:
    if len(set(refs)) != len(refs):
        raise invalid("work_refs", "Select each work session once.")
    heads = {ref: service.head(WorkSession, ref, lock=lock) for ref in sorted(refs)}
    if any(head.superseded or head.lifecycle != "recorded" for head in heads.values()):
        raise invalid(
            "work_refs", "Select current recorded work; stop timers before splitting or merging."
        )
    return [heads[ref] for ref in refs]


def split_part(parent: WorkSessionSpec, part: WorkSplitPart) -> WorkSessionSpec:
    fields = parent.model_dump(exclude={"time"})
    supplied = part.model_dump(exclude_unset=True)
    for name, value in supplied.items():
        if name not in {"start", "end", "duration_minutes"}:
            fields[name] = value
    if "engagement_ref" in supplied and "organization_ref" not in supplied:
        fields["organization_ref"] = None
    fields["source_method"] = "report"
    if parent.time.precision == "interval":
        if not part.start or not part.end or part.duration_minutes is not None:
            raise invalid(
                "parts",
                "For bounded work supply start and end for each part; breaks are inherited.",
            )
        a = work_instant(part.start, parent.time.timezone, "parts.start")
        b = work_instant(part.end, parent.time.timezone, "parts.end")
        if (
            a.instant >= b.instant
            or parent.time.start_at is None
            or parent.time.end_at is None
            or a.instant < parent.time.start_at
            or b.instant > parent.time.end_at
        ):
            raise invalid(
                "parts", "Each part must be a positive window within the original bounds."
            )
        segments = []
        for segment in parent.time.segments:
            assert segment.end_at is not None
            lo, hi = max(a.instant, segment.start_at), min(b.instant, segment.end_at)
            if lo < hi:
                segments.append(
                    WorkSegmentSpec(
                        kind=segment.kind,
                        start_at=lo,
                        end_at=hi,
                        start_offset_seconds=a.offset_seconds
                        if lo == a.instant
                        else segment.start_offset_seconds,
                        end_offset_seconds=b.offset_seconds
                        if hi == b.instant
                        else segment.end_offset_seconds,
                    )
                )
        quantity = sum(
            elapsed_us(s.end_at - s.start_at) for s in segments if s.kind == "active" and s.end_at
        )
        if quantity <= 0:
            raise invalid("parts", "Each part must contain some actual work.")
        time = WorkTimeSpec(
            precision="interval",
            timezone=parent.time.timezone,
            start_at=a.instant,
            end_at=b.instant,
            start_offset_seconds=a.offset_seconds,
            end_offset_seconds=b.offset_seconds,
            duration_us=quantity,
            segments=segments,
        )
        for key in ("work_date", "period_start", "period_end"):
            fields[key] = None
    elif parent.time.precision == "duration":
        if part.duration_minutes is None or part.start or part.end:
            raise invalid("parts", "For duration-only work supply duration_minutes for each part.")
        time = frozen_time(
            normalize_work_time(
                start=None,
                end=None,
                duration_minutes=part.duration_minutes,
                timezone=parent.time.timezone,
                breaks=[],
            ),
            parent.time.timezone,
        )
        if "work_date" in supplied:
            fields.update(period_start=None, period_end=None)
    else:
        raise invalid("work_ref", "Specify the unknown quantity before splitting this record.")
    return WorkSessionSpec.model_validate({**fields, "time": time})


def merged_time(parents: list[WorkSessionSpec]) -> WorkTimeSpec:
    first = parents[0].time
    if all(p.time.precision == "duration" for p in parents):
        placement = (
            parents[0].work_date,
            parents[0].period_start,
            parents[0].period_end,
            first.timezone,
        )
        if any(
            (p.work_date, p.period_start, p.period_end, p.time.timezone) != placement
            for p in parents
        ):
            raise invalid(
                "work_refs",
                "These duration reports have different date placement; retain separate records.",
            )
        return first.model_copy(
            update={"duration_us": sum(p.time.duration_us or 0 for p in parents)}
        )
    if not all(p.time.precision == "interval" for p in parents):
        raise invalid(
            "work_refs",
            "Merge requires compatible known time; retain mixed or incomplete records separately.",
        )
    active = sorted(
        (s for p in parents for s in p.time.segments if s.kind == "active"),
        key=lambda s: s.start_at,
    )
    start = min((p.time for p in parents), key=lambda t: t.start_at)  # type: ignore[arg-type,return-value]
    finish = max((p.time for p in parents), key=lambda t: t.end_at)  # type: ignore[arg-type,return-value]
    assert start.start_at is not None and finish.end_at is not None
    segments: list[WorkSegmentSpec] = []
    cursor, offset = start.start_at, start.start_offset_seconds
    for segment in active:
        assert segment.end_at is not None
        if segment.start_at < cursor:
            raise invalid(
                "work_refs",
                "Active intervals overlap. Correct or retract the duplicate before merging.",
            )
        if segment.start_at > cursor:
            assert offset is not None
            segments.append(
                WorkSegmentSpec(
                    kind="break",
                    start_at=cursor,
                    end_at=segment.start_at,
                    start_offset_seconds=offset,
                    end_offset_seconds=segment.start_offset_seconds,
                )
            )
        segments.append(segment)
        cursor, offset = segment.end_at, segment.end_offset_seconds
    if cursor < finish.end_at:
        assert offset is not None
        segments.append(
            WorkSegmentSpec(
                kind="break",
                start_at=cursor,
                end_at=finish.end_at,
                start_offset_seconds=offset,
                end_offset_seconds=finish.end_offset_seconds,
            )
        )
    return WorkTimeSpec(
        precision="interval",
        timezone=first.timezone,
        start_at=start.start_at,
        end_at=finish.end_at,
        start_offset_seconds=start.start_offset_seconds,
        end_offset_seconds=finish.end_offset_seconds,
        duration_us=sum(p.time.duration_us or 0 for p in parents),
        segments=segments,
    )


def merged_spec(parents: list[WorkSessionSpec], values: dict[str, Any]) -> WorkSessionSpec:
    fields: dict[str, Any] = {}
    time = merged_time(parents)
    for key in WorkSessionSpec.model_fields:
        if key in {"time", "status", "source_method"}:
            continue
        if key == "source_refs":
            # Evidence remains complete even when the client did not copy it.
            fields[key] = sorted(
                {ref for p in parents for ref in p.source_refs} | set(values.get(key, []))
            )
        elif key in values:
            fields[key] = values[key]
        elif key in {"work_date", "period_start", "period_end"} and time.precision == "interval":
            fields[key] = None
        else:
            candidates = [getattr(p, key) for p in parents]
            if all(value == candidates[0] for value in candidates):
                fields[key] = candidates[0]
            elif key in {"description", "outcome", "uncertainty"}:
                joined = "\n".join(dict.fromkeys(value for value in candidates if value))
                limit = 2000 if key == "uncertainty" else 4000
                if len(joined) > limit:
                    raise invalid(key, "Supply a concise merged value for this field.")
                fields[key] = joined or None
            else:
                raise invalid(
                    key, f"Selected work differs in {key}; supply the intended merged value."
                )
    return WorkSessionSpec.model_validate(
        {**fields, "time": time, "source_method": "report", "status": "recorded"}
    )


class WorkLineageCommandService(NativeCommandService):
    def perform(
        self, command: str, arguments: dict[str, Any], request_ref: str, request_key: str
    ) -> dict[str, Any]:
        public = (
            SplitWorkSession.model_validate(arguments)
            if command == "docket_split_work_session"
            else MergeWorkSessions.model_validate(arguments)
        )
        values = public.model_dump(mode="json", exclude_unset=True)
        service = WorkAttributionService(self.session)

        def build(context: NativeContext) -> dict[str, Any]:
            splitting = isinstance(public, SplitWorkSession)
            refs = [public.work_ref] if isinstance(public, SplitWorkSession) else public.work_refs
            heads = recorded(service, refs)
            parents = [service.work_spec(head) for head in heads]
            children = (
                [split_part(parents[0], part) for part in public.parts]
                if isinstance(public, SplitWorkSession)
                else [merged_spec(parents, values)]
            )
            for child in children:
                service.validate_links(child)
                validate_work_time(child)
            validate_conservation(parents, children)
            variant = WorkSessionSplit if splitting else WorkSessionMerge
            action = variant(
                change_id="work_lineage",
                action="update",
                object_type="work_session",
                object_ref=refs[0],
                affected_fields=["lineage"],
                payload=WorkLineageSpec(parent_refs=refs, children=children),
            )
            for ref in refs:
                if ref not in context.versions:
                    raise invalid(
                        "context_id", "Read all selected work sessions before changing them."
                    )
            retained = {
                ref for parent in parents for ref in _named_refs(parent.model_dump(mode="json"))
            }
            return mutation_plan(
                context, [mutation_input_json(action)], command, retained_refs=retained
            )

        return self.execute(
            command=command,
            arguments=values,
            request_ref=request_ref,
            request_key=request_key,
            build=build,
        )


def active_windows(specs: list[WorkSessionSpec]) -> list[tuple[Any, Any]]:
    return sorted(
        (s.start_at, s.end_at) for p in specs for s in p.time.segments if s.kind == "active"
    )


def validate_conservation(parents: list[WorkSessionSpec], children: list[WorkSessionSpec]) -> None:
    quantities = [p.time.duration_us for p in parents + children]
    if any(q is None for q in quantities) or sum(p.time.duration_us or 0 for p in parents) != sum(
        c.time.duration_us or 0 for c in children
    ):
        raise invalid("parts", "The parts must preserve exactly the original known work quantity.")
    if all(p.time.precision == "interval" for p in parents + children):

        def canonical(specs: list[WorkSessionSpec]) -> list[tuple[Any, Any]]:
            result: list[tuple[Any, Any]] = []
            for start, end in active_windows(specs):
                if result and start < result[-1][1]:
                    raise invalid("parts", "Parts cannot count the same active time twice.")
                if result and start == result[-1][1]:
                    result[-1] = (result[-1][0], end)
                else:
                    result.append((start, end))
            return result

        if canonical(parents) != canonical(children):
            raise invalid("parts", "Parts must retain every original active interval and its gaps.")
    elif not all(p.time.precision == "duration" for p in parents + children):
        raise invalid("parts", "Splitting/merging must preserve the reported time precision.")


def interval_spans(
    parent: WorkSessionSpec, child: WorkSessionSpec, spans: list[WorkSpan]
) -> list[WorkSpan]:
    result: list[WorkSpan] = []
    cursor = 0
    for original in parent.time.segments:
        if original.kind != "active":
            continue
        assert original.end_at is not None
        for segment in child.time.segments:
            if segment.kind != "active":
                continue
            assert segment.end_at is not None
            lo, hi = max(original.start_at, segment.start_at), min(original.end_at, segment.end_at)
            if lo < hi:
                a = cursor + elapsed_us(lo - original.start_at)
                result.extend(slice_spans(spans, a, a + elapsed_us(hi - lo)))
        cursor += elapsed_us(original.end_at - original.start_at)
    return result


def apply_lineage(service: WorkAttributionService, changeset: ChangeSet, change: Any) -> list[str]:
    spec = WorkLineageSpec.model_validate(change.payload)
    splitting = change.mutation_type == "work_session_split"
    if (
        change.object_ref != spec.parent_refs[0]
        or (splitting and (len(spec.parent_refs) != 1 or len(spec.children) < 2))
        or (not splitting and (len(spec.parent_refs) < 2 or len(spec.children) != 1))
    ):
        raise invalid("work_refs", "Invalid work lineage shape.")
    heads = recorded(service, spec.parent_refs, lock=True)
    for head in heads:
        service.check_version(head, changeset)
    parents = [service.work_spec(head) for head in heads]
    validate_conservation(parents, spec.children)
    quantities = WorkQuantityService(service.session)
    revisions = [service.work_revision(head) for head in heads]
    for head in heads:
        quantities.ensure_history(head, changeset.ref_id)
    mappings = [quantities.spans(revision.ref_id) for revision in revisions]
    service.session.scalars(
        select(WorkQuantityOrigin)
        .where(
            WorkQuantityOrigin.ref_id.in_({s.origin_ref for mapping in mappings for s in mapping})
        )
        .order_by(WorkQuantityOrigin.ref_id)
        .with_for_update()
    ).all()
    result = []
    for head, parent, spans in zip(heads, parents, mappings, strict=True):
        result += service.persist_work(
            changeset, change, parent, head=head, superseded=True, quantity_spans=spans
        )
    cursor = 0
    child_refs: list[str] = []
    for child in spec.children:
        if splitting:
            quantity = child.time.duration_us or 0
            spans = (
                interval_spans(parents[0], child, mappings[0])
                if child.time.precision == "interval"
                else slice_spans(mappings[0], cursor, cursor + quantity)
            )
            cursor += quantity
        elif child.time.precision == "interval":
            # Ordered segments determine coordinate order, independently of input ref order.
            spans = []
            for segment in child.time.segments:
                if segment.kind == "active":
                    selected = child.model_copy(
                        update={"time": child.time.model_copy(update={"segments": [segment]})}
                    )
                    for parent, mapping in zip(parents, mappings, strict=True):
                        spans += interval_spans(parent, selected, mapping)
        else:
            spans = [span for mapping in mappings for span in mapping]
        refs = service.persist_work(changeset, change, child, quantity_spans=spans)
        child_refs += refs
        new_head = service.head(WorkSession, refs[0])
        revision = service.work_revision(new_head)
        for parent_revision in revisions:
            service.session.add(
                WorkLineageEdge(
                    parent_revision_ref=parent_revision.ref_id,
                    child_revision_ref=revision.ref_id,
                    relation="split" if splitting else "merge",
                    created_by_changeset_ref=changeset.ref_id,
                )
            )
    service.session.flush()
    return child_refs + result
