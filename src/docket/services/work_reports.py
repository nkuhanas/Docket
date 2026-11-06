"""Bounded work queries and lossless informal exports over exact retained revisions."""

import base64
import csv
import io
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, ValidationError
from sqlalchemy import func, insert, literal, select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.config import Settings
from docket.domain.errors import DocketError
from docket.domain.work_time import work_zone
from docket.models import (
    Engagement,
    EngagementRevision,
    WorkReportMember,
    WorkReportSnapshot,
    WorkSession,
    WorkSessionRevision,
)
from docket.models.base import utc_now
from docket.schemas.common import StrictModel
from docket.schemas.work import QueryWorkSessions
from docket.services.work_attribution import WorkAttributionService, raw_time, utc_instant
from docket.services.work_report_query import WorkReportQuery, matching, period_bounds

MAX_OUTPUT = 16 * 1024
CALCULATION_VERSION = "work-report-v1"
METRICS = (
    "session_count",
    "known_quantity_count",
    "unknown_quantity_count",
    "unplaced_count",
    "classification_unknown_count",
    "open_timer_count",
    "timed_us",
    "duration_only_us",
    "unallocated_us",
    "estimate_us",
    "unique_timed_us",
    "overlap_us",
    "known_reported_us",
    "allocated_us",
    "known_reported_hours",
)
ENTRY_FIELDS = (
    "ref",
    "revision_ref",
    "revision",
    "description",
    "outcome",
    "category",
    "status",
    "time_precision",
    "duration_us",
    "start",
    "end",
    "timezone",
    "breaks",
    "work_date",
    "period_start",
    "period_end",
    "engagement_ref",
    "organization_ref",
    "project_ref",
    "item_ref",
    "task_ref",
    "event_ref",
    "place_ref",
    "attribution_label",
    "reported_compensation_class",
    "work_mode",
    "reported_location",
    "source_method",
    "uncertainty",
    "source_refs",
    "basis_refs",
    "changeset_ref",
)
CSV_FIELDS = (
    "record_type",
    "report_ref",
    "date_from",
    "date_to",
    "report_timezone",
    "as_of",
    "bucket",
    "label",
    *METRICS,
    *ENTRY_FIELDS,
)


class ReportCursor(StrictModel):
    report_ref: str = Field(pattern=r"^wrep_[0-9A-HJKMNP-TV-Z]{26}$")
    mode: Literal["summary", "json", "csv"] | None = None
    stage: int = Field(default=0, ge=0, le=5)
    group_after: str | None = Field(default=None, max_length=128)
    work_after: str | None = Field(default=None, max_length=40)
    groups_done: bool = False
    work_done: bool = False
    offset: int = Field(default=0, ge=0, le=1_000_000)

    def encode(self) -> str:
        return (
            base64.urlsafe_b64encode(
                json.dumps(self.model_dump(exclude_defaults=True), separators=(",", ":")).encode()
            )
            .decode()
            .rstrip("=")
        )


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def cursor_error() -> DocketError:
    return DocketError(
        code="invalid_work_report_cursor",
        message="Use this report's returned cursor.",
        details={"field": "cursor"},
    )


def metrics(row: Any, unique: int | None = None) -> dict[str, Any]:
    values = {
        key: int(value or 0) for key, value in dict(row).items() if key not in {"bucket", "label"}
    }
    if unique is not None:
        values["unique_timed_us"] = unique
    values["known_reported_us"] = values["timed_us"] + values["duration_only_us"]
    values["allocated_us"] = values["known_reported_us"] - values["unallocated_us"]
    values["overlap_us"] = values["timed_us"] - values["unique_timed_us"]
    result: dict[str, Any] = values
    result["known_reported_hours"] = format(
        Decimal(values["known_reported_us"]) / 3_600_000_000, ".6f"
    )
    return result


class WorkReportService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session, self.settings = session, settings

    def _parameters(self, public: QueryWorkSessions) -> tuple[dict[str, Any], datetime]:
        now = utc_now()
        zone = work_zone(public.timezone or self.settings.timezone)
        today = now.astimezone(zone).date()
        first = public.date_from or today - timedelta(days=today.weekday())
        last = public.date_to or first + timedelta(days=7)
        if last <= first:
            raise DocketError(
                code="invalid_work_report_period",
                message="date_to must follow date_from.",
                details={"field": "date_to"},
            )
        as_of = public.as_of or now
        if as_of.tzinfo is None or as_of > now:
            raise DocketError(
                code="invalid_work_report_as_of",
                message="Use an offset timestamp no later than now.",
                details={"field": "as_of"},
            )
        parameters = public.model_dump(mode="json", exclude={"as_of", "cursor", "format", "limit"})
        parameters.update(date_from=first.isoformat(), date_to=last.isoformat(), timezone=str(zone))
        lower, upper = period_bounds(parameters)
        parameters.update(
            utc_from=lower.isoformat(),
            utc_to=upper.isoformat(),
            calculation_version=CALCULATION_VERSION,
        )
        return parameters, as_of.astimezone(UTC)

    def _capture(self, public: QueryWorkSessions) -> WorkReportSnapshot:
        principal = require_principal("read")
        parameters, as_of = self._parameters(public)
        if public.engagement_ref:
            WorkAttributionService(self.session).head(Engagement, public.engagement_ref)
        report = WorkReportSnapshot(
            principal_ref=principal.principal_ref,
            operator_ref=principal.operator_ref,
            parameters_json=parameters,
            as_of=as_of,
        )
        self.session.add(report)
        self.session.flush()
        # One INSERT/SELECT captures one visible revision set. Later commits with an
        # earlier timestamp cannot enter an already returned report's continuation.
        r, e = WorkSessionRevision, EngagementRevision
        latest = (
            select(
                r.ref_id,
                func.row_number()
                .over(partition_by=r.work_ref, order_by=r.revision.desc())
                .label("position"),
            )
            .join(WorkSession, WorkSession.ref_id == r.work_ref)
            .where(
                WorkSession.operator_ref == principal.operator_ref,
                r.created_at <= as_of,
            )
            .cte("latest_reported_work")
        )
        engagement = (
            select(e.ref_id)
            .where(e.engagement_ref == r.engagement_ref, e.created_at <= as_of)
            .order_by(e.revision.desc())
            .limit(1)
            .correlate(r)
            .scalar_subquery()
        )
        selected = (
            select(literal(report.id), r.work_ref, r.ref_id, engagement)
            .join(
                latest,
                latest.c.ref_id == r.ref_id,
            )
            .where(
                latest.c.position == 1,
                r.lifecycle.in_(["recorded", "running", "paused"]),
                r.superseded.is_(False),
                matching(r, parameters),
            )
        )
        for key in (
            "engagement_ref",
            "organization_ref",
            "project_ref",
            "reported_compensation_class",
        ):
            if parameters[key] is not None:
                if key == "organization_ref":
                    pinned_org = (
                        select(e.organization_ref)
                        .where(e.ref_id == engagement)
                        .correlate(r)
                        .scalar_subquery()
                    )
                    selected = selected.where(
                        func.coalesce(r.organization_ref, pinned_org) == parameters[key]
                    )
                else:
                    selected = selected.where(getattr(r, key) == parameters[key])
        self.session.execute(
            insert(WorkReportMember).from_select(
                ["report_id", "work_ref", "revision_ref", "engagement_revision_ref"],
                selected,
            )
        )
        report.sealed_at = utc_now()
        self.session.flush()
        return report

    def _resume(self, public: QueryWorkSessions) -> tuple[WorkReportSnapshot, ReportCursor]:
        try:
            raw = public.cursor or ""
            state = ReportCursor.model_validate_json(
                base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
            )
        except (ValueError, ValidationError) as exc:
            raise cursor_error() from exc
        principal = require_principal("read")
        report = self.session.scalar(
            select(WorkReportSnapshot).where(
                WorkReportSnapshot.ref_id == state.report_ref,
                WorkReportSnapshot.principal_ref == principal.principal_ref,
                WorkReportSnapshot.operator_ref == principal.operator_ref,
                WorkReportSnapshot.sealed_at.is_not(None),
            )
        )
        if report is None:
            raise DocketError(
                code="work_report_unavailable", message="Read your own retained work report."
            )
        supplied = public.model_dump(mode="json", exclude_unset=True)
        for key in public.model_fields_set - {"cursor", "format", "limit"}:
            expected = report.as_of.isoformat() if key == "as_of" else report.parameters_json[key]
            actual = supplied[key]
            if actual is None and key in {"date_from", "date_to", "timezone", "as_of"}:
                continue
            if key == "as_of" and public.as_of is not None:
                expected, actual = utc_instant(report.as_of), utc_instant(public.as_of)
            if actual != expected:
                raise DocketError(
                    code="work_report_filter_changed",
                    message="Start a new query for changed filters.",
                    details={"field": key},
                )
        mode = public.format if "format" in public.model_fields_set else state.mode or public.format
        if state.mode is not None and state.mode != mode:
            raise DocketError(
                code="work_report_format_changed",
                message="Use report_cursor to restart in another format.",
                details={"field": "cursor"},
            )
        return report, state.model_copy(update={"mode": mode})

    def _metadata(self, report: WorkReportSnapshot) -> dict[str, Any]:
        as_of, captured = utc_instant(report.as_of), utc_instant(report.captured_at)
        assert as_of is not None and captured is not None
        return {
            "ref": report.ref_id,
            "kind": "informal_reported_work",
            **report.parameters_json,
            "as_of": as_of.isoformat(),
            "captured_at": captured.isoformat(),
            "basis": "exact retained work and engagement revisions",
            "coverage": "reported records only; absence never proves inactivity or completeness",
            "quantities": "integer microseconds; hours are display-only at six decimals",
            "totals": "known subtotal includes unallocated reports; estimates are separate; "
            "eligibility is unverified",
        }

    def _entry(self, row: Any, *, full: bool) -> dict[str, Any]:
        values = dict(row)
        state = {
            "ref": values["work_ref"],
            "revision_ref": values["ref_id"],
            "revision": values["revision"],
            "status": values["lifecycle"],
            "time_precision": values["time_precision"],
            "duration_us": values["duration_us"],
        }
        keys = (
            ENTRY_FIELDS
            if full
            else (
                "description",
                "engagement_ref",
                "organization_ref",
                "project_ref",
                "work_date",
                "period_start",
                "period_end",
                "reported_compensation_class",
                "event_ref",
                "work_mode",
                "reported_location",
                "attribution_label",
            )
        )
        for key in keys:
            if key in values and values[key] is not None and key not in state:
                value = values[key]
                state[key] = value.isoformat() if isinstance(value, date | datetime) else value
        if full:
            record = self.session.scalar(
                select(WorkSessionRevision).where(
                    WorkSessionRevision.ref_id == values["ref_id"],
                )
            )
            assert record is not None
            spec = WorkAttributionService(self.session).revision_spec(record)
            raw = raw_time(spec.time)
            state.update({key: value for key, value in raw.items() if value is not None})
            state["breaks"] = [{"start": a, "end": b} for a, b in raw["breaks"]]
            state["changeset_ref"] = record.created_by_changeset_ref
        else:
            omitted = []
            for key in ("description", "reported_location", "attribution_label"):
                if key in state and len(state[key]) > 160:
                    state[key] = state[key][:160]
                    omitted.append(key)
            if omitted:
                state["omitted_fields"] = omitted
        return state

    @staticmethod
    def _groups(query: WorkReportQuery, after: str | None, limit: int) -> list[dict[str, Any]]:
        groups = query.stats(grouped=True).subquery()
        statement = select(groups).order_by(groups.c.bucket).limit(limit)
        if after is not None:
            statement = statement.where(groups.c.bucket > after)
        return [
            {
                "bucket": row["bucket"],
                **({"label": row["label"]} if row.get("label") else {}),
                **metrics(row),
            }
            for row in query.session.execute(statement).mappings()
        ]

    def _entries(
        self, query: WorkReportQuery, after: str | None, limit: int, *, full: bool
    ) -> list[dict[str, Any]]:
        statement = select(query.rows).order_by(query.rows.c.work_ref).limit(limit)
        if after is not None:
            statement = statement.where(query.rows.c.work_ref > after)
        return [self._entry(row, full=full) for row in self.session.execute(statement).mappings()]

    def _summary(
        self, base: dict[str, Any], query: WorkReportQuery, cursor: ReportCursor, limit: int
    ) -> dict[str, Any]:
        output = {**base, "groups": [], "items": []}
        state = cursor.model_copy()
        for kind, after, done in (
            ("groups", cursor.group_after, cursor.groups_done),
            ("items", cursor.work_after, cursor.work_done),
        ):
            if done:
                continue
            candidates = (
                self._groups(query, after, limit + 1)
                if kind == "groups"
                else self._entries(query, after, limit + 1, full=False)
            )
            for item in candidates[:limit]:
                output[kind].append(item)
                if len(compact(output).encode()) > MAX_OUTPUT - 1536:
                    output[kind].pop()
                    break
            consumed = output[kind]
            finished = len(candidates) <= limit and len(consumed) == len(candidates)
            if kind == "groups":
                state.group_after = consumed[-1]["bucket"] if consumed else after
                state.groups_done = finished
            else:
                state.work_after = consumed[-1]["ref"] if consumed else after
                state.work_done = finished
        output["next_cursor"] = None if state.groups_done and state.work_done else state.encode()
        return output

    @staticmethod
    def _csv_row(values: dict[str, Any]) -> str:
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(
            [
                compact(values[key])
                if isinstance(values.get(key), list | dict)
                else values.get(key, "")
                for key in CSV_FIELDS
            ]
        )
        return stream.getvalue()

    def _pieces(
        self, base: dict[str, Any], query: WorkReportQuery, state: ReportCursor, limit: int
    ) -> Any:
        mode = state.mode
        metadata = base["report"]
        common = {
            "report_ref": metadata["ref"],
            "date_from": metadata["date_from"],
            "date_to": metadata["date_to"],
            "report_timezone": metadata["timezone"],
            "as_of": metadata["as_of"],
        }
        if state.stage == 0:
            header = (
                "{"
                + '"report":'
                + compact(metadata)
                + ',"totals":'
                + compact(base["totals"])
                + ',"groups":['
                if mode == "json"
                else ",".join(CSV_FIELDS)
                + "\n"
                + self._csv_row(
                    {
                        **common,
                        "record_type": "total",
                        **base["totals"],
                    }
                )
            )
            next_state = state.model_copy(update={"stage": 1, "offset": 0})
            yield header, next_state
            state = next_state
        if state.stage == 1:
            groups = self._groups(query, state.group_after, limit)
            for group in groups:
                value = (
                    ("," if state.group_after is not None else "") + compact(group)
                    if mode == "json"
                    else self._csv_row({**common, "record_type": "group", **group})
                )
                next_state = state.model_copy(update={"group_after": group["bucket"], "offset": 0})
                yield value, next_state
                state = next_state
            if len(groups) == limit:
                return
            state = state.model_copy(update={"stage": 2})
        if state.stage == 2:
            next_state = state.model_copy(update={"stage": 3, "offset": 0})
            yield '],"entries":[' if mode == "json" else "", next_state
            state = next_state
        if state.stage == 3:
            entries = self._entries(query, state.work_after, limit, full=True)
            for entry in entries:
                value = (
                    ("," if state.work_after is not None else "") + compact(entry)
                    if mode == "json"
                    else self._csv_row({**common, "record_type": "work", **entry})
                )
                next_state = state.model_copy(update={"work_after": entry["ref"], "offset": 0})
                yield value, next_state
                state = next_state
            if len(entries) == limit:
                return
            state = state.model_copy(update={"stage": 4})
        if state.stage == 4:
            yield (
                "]}\n" if mode == "json" else "",
                state.model_copy(update={"stage": 5, "offset": 0}),
            )

    def _export(
        self, base: dict[str, Any], query: WorkReportQuery, cursor: ReportCursor, limit: int
    ) -> dict[str, Any]:
        state = cursor.model_copy()
        output = {
            **base,
            "content_type": "application/json" if state.mode == "json" else "text/csv",
            "content": "",
        }
        for piece, next_state in self._pieces(base, query, state, limit):
            try:
                raw = piece.encode()
                if state.offset > len(raw):
                    raise ValueError("offset exceeds this record")
                remaining = raw[state.offset :].decode()
            except (ValueError, UnicodeDecodeError) as exc:
                raise cursor_error() from exc
            lower, upper = 0, len(remaining)
            while lower < upper:
                middle = (lower + upper + 1) // 2
                candidate = {**output, "content": output["content"] + remaining[:middle]}
                if len(compact(candidate).encode()) <= MAX_OUTPUT - 1536:
                    lower = middle
                else:
                    upper = middle - 1
            output["content"] += remaining[:lower]
            if lower != len(remaining):
                state.offset += len(remaining[:lower].encode())
                break
            state = next_state
        output["next_cursor"] = None if state.stage == 5 else state.encode()
        output["complete"] = state.stage == 5
        return output

    def query(self, public: QueryWorkSessions) -> dict[str, Any]:
        require_principal("read")
        if public.cursor:
            report, state = self._resume(public)
        else:
            report = self._capture(public)
            state = ReportCursor(report_ref=report.ref_id, mode=public.format)
        if report.parameters_json.get("calculation_version") != CALCULATION_VERSION:
            raise DocketError(
                code="work_report_calculation_unavailable",
                message="Start a new report with the current calculation version.",
            )
        query = WorkReportQuery(self.session, report)
        row = self.session.execute(query.stats()).mappings().one()
        total = metrics(row, query.total_unique())
        base = {
            "ok": True,
            "report": self._metadata(report),
            "totals": total,
            "report_cursor": ReportCursor(report_ref=report.ref_id).encode(),
        }
        result = (
            self._summary(base, query, state, public.limit)
            if state.mode == "summary"
            else self._export(base, query, state, public.limit)
        )
        if len(compact(result).encode()) > MAX_OUTPUT:
            raise DocketError(
                code="work_report_budget_exceeded", message="Request a smaller report page."
            )
        return result
