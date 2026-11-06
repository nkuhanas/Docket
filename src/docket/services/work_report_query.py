"""Server-side exact work aggregates over a retained revision set."""

import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    String,
    and_,
    case,
    cast,
    distinct,
    exists,
    func,
    literal,
    or_,
    select,
    union_all,
)
from sqlalchemy.orm import Session

from docket.domain.work_time import elapsed_us
from docket.models import (
    EngagementRevision,
    WorkReportMember,
    WorkReportSnapshot,
    WorkSegment,
    WorkTimeComponent,
)
from docket.models import WorkSessionRevision as Revision
from docket.services.work_attribution import utc_instant
from docket.services.work_composition import time_leaves


def period_bounds(parameters: dict[str, Any]) -> tuple[datetime, datetime]:
    if "utc_from" in parameters:
        return (
            datetime.fromisoformat(parameters["utc_from"]),
            datetime.fromisoformat(parameters["utc_to"]),
        )
    zone = ZoneInfo(parameters["timezone"])
    return tuple(
        datetime.combine(date.fromisoformat(parameters[key]), time(), zone).astimezone(UTC)
        for key in ("date_from", "date_to")
    )  # type: ignore[return-value]


def unplaced(rows: Any) -> Any:
    return and_(
        rows.work_date.is_(None),
        rows.period_start.is_(None),
        rows.start_at.is_(None),
        rows.end_at.is_(None),
    )


def time_matching(rows: Any, parameters: dict[str, Any]) -> Any:
    start, end = period_bounds(parameters)
    return or_(
        and_(
            rows.time_precision.in_(["interval", "timer"]),
            rows.start_at < end,
            or_(rows.end_at > start, and_(rows.time_precision == "timer", rows.end_at.is_(None))),
        ),
        and_(
            rows.work_date >= date.fromisoformat(parameters["date_from"]),
            rows.work_date < date.fromisoformat(parameters["date_to"]),
        ),
        and_(
            rows.period_start < date.fromisoformat(parameters["date_to"]),
            rows.period_end > date.fromisoformat(parameters["date_from"]),
        ),
        and_(
            rows.time_precision == "incomplete",
            or_(
                and_(rows.start_at >= start, rows.start_at < end),
                and_(rows.end_at > start, rows.end_at <= end),
            ),
        ),
        and_(literal(parameters["include_unplaced"]), unplaced(rows)),
    )


def matching(rows: Any, parameters: dict[str, Any]) -> Any:
    roots = (
        select(WorkTimeComponent.revision_ref.label("ref_id"))
        .where(
            WorkTimeComponent.revision_ref == rows.ref_id,
        )
        .distinct()
        .correlate(rows)
        .subquery()
    )
    leaves = time_leaves(roots)
    return or_(
        and_(rows.time_precision != "composite", time_matching(rows, parameters)),
        and_(
            rows.time_precision == "composite",
            exists(
                select(1).select_from(leaves).where(time_matching(leaves.c, parameters)),
            ),
        ),
    )


class WorkReportQuery:
    """PostgreSQL performs aggregation/window unions; SQLite mirrors math in fast fixtures."""

    def __init__(self, session: Session, report: WorkReportSnapshot) -> None:
        self.session, self.report = session, report
        self.parameters = report.parameters_json
        self.zone = self.parameters["timezone"]
        self.group_by = self.parameters["group_by"]
        self.postgres = session.get_bind().dialect.name == "postgresql"
        if not self.postgres:
            self._sqlite_functions()
        self.start, self.end = period_bounds(self.parameters)
        self.rows = (
            select(
                *Revision.__table__.c,
                func.coalesce(Revision.organization_ref, EngagementRevision.organization_ref).label(
                    "organization"
                ),
                EngagementRevision.label.label("engagement_label"),
            )
            .join(WorkReportMember, WorkReportMember.revision_ref == Revision.ref_id)
            .outerjoin(
                EngagementRevision,
                EngagementRevision.ref_id == WorkReportMember.engagement_revision_ref,
            )
            .where(WorkReportMember.report_id == report.id)
            .cte("reported_work")
        )
        leaves = time_leaves(select(self.rows.c.ref_id).subquery())
        time_fields = {
            "id",
            "time_precision",
            "duration_us",
            "start_at",
            "end_at",
            "start_offset_seconds",
            "end_offset_seconds",
            "timezone",
            "work_date",
            "period_start",
            "period_end",
        }
        self.time_rows = (
            select(
                *(
                    getattr(leaves.c, column.name) if column.name in time_fields else column
                    for column in self.rows.c
                ),
                leaves.c.ref_id.label("source_ref"),
            )
            .select_from(self.rows.join(leaves, leaves.c.root_ref == self.rows.c.ref_id))
            .where(
                time_matching(leaves.c, self.parameters),
            )
            .cte("reported_time_parts")
        )
        self.intervals = self._intervals()
        self.group_intervals = self._group_intervals()
        self.parts = self._parts()

    def _sqlite_functions(self) -> None:
        connection = self.session.connection().connection.driver_connection
        assert isinstance(connection, sqlite3.Connection)

        def instant(value: str) -> datetime:
            return datetime.fromisoformat(value).replace(
                tzinfo=datetime.fromisoformat(value).tzinfo or UTC,
            )

        connection.create_function(
            "docket_work_elapsed",
            2,
            lambda end, start: elapsed_us(instant(end) - instant(start)),
            deterministic=True,
        )
        connection.create_function(
            "docket_work_day",
            2,
            lambda value, zone: (
                instant(value).astimezone(ZoneInfo(zone)).date().isoformat() if value else None
            ),
            deterministic=True,
        )
        connection.create_function(
            "docket_work_week",
            1,
            lambda value: (
                (
                    date.fromisoformat(value[:10])
                    - timedelta(days=date.fromisoformat(value[:10]).weekday())
                ).isoformat()
                if value
                else None
            ),
            deterministic=True,
        )
        connection.create_function(
            "docket_work_add_day",
            1,
            lambda value: (date.fromisoformat(value[:10]) + timedelta(days=1)).isoformat(),
            deterministic=True,
        )
        connection.create_function(
            "docket_work_last_day",
            1,
            lambda value: (
                (date.fromisoformat(value[:10]) - timedelta(days=1)).isoformat() if value else None
            ),
            deterministic=True,
        )
        connection.create_function(
            "docket_work_midnight",
            2,
            lambda value, zone: (
                datetime.combine(date.fromisoformat(value[:10]), time(), ZoneInfo(zone))
                .astimezone(UTC)
                .strftime("%Y-%m-%d %H:%M:%S.%f")
            ),
            deterministic=True,
        )

    def elapsed(self, end: Any, start: Any) -> Any:
        if self.postgres:
            return cast(func.extract("epoch", end - start) * 1_000_000, BigInteger)
        return func.docket_work_elapsed(end, start, type_=BigInteger)

    def day(self, instant: Any) -> Any:
        return (
            cast(cast(func.timezone(self.zone, instant), Date), String)
            if self.postgres
            else func.docket_work_day(instant, self.zone)
        )

    def week(self, day: Any) -> Any:
        return (
            cast(cast(func.date_trunc("week", cast(day, DateTime)), Date), String)
            if self.postgres
            else func.docket_work_week(day)
        )

    def last_day(self, day: Any) -> Any:
        return (
            cast(cast(day, Date) - 1, String) if self.postgres else func.docket_work_last_day(day)
        )

    def _dimension(self, rows: Any) -> Any:
        return func.coalesce(
            {
                "engagement": rows.engagement_ref,
                "organization": rows.organization,
                "project": rows.project_ref,
            }[self.group_by],
            "unknown",
        )

    def _intervals(self) -> Any:
        r = self.time_rows.c
        end: Any = func.coalesce(WorkSegment.end_at, utc_instant(self.report.as_of))
        start = case((WorkSegment.start_at > self.start, WorkSegment.start_at), else_=self.start)
        end = case((end < self.end, end), else_=self.end)
        return (
            select(
                r.work_ref,
                r.source_ref,
                start.label("start_at"),
                end.label("end_at"),
                (r.lifecycle != "recorded").label("estimate"),
                r.engagement_ref,
                r.project_ref,
                r.organization,
            )
            .join(
                WorkSegment,
                WorkSegment.revision_id == r.id,
            )
            .where(WorkSegment.kind == "active", end > start)
            .cte("work_intervals")
        )

    def _group_intervals(self) -> Any:
        i = self.intervals.c
        if self.group_by not in {"day", "week"}:
            return select(*i, self._dimension(i).label("bucket")).cte("group_intervals")
        days = select(literal(self.parameters["date_from"]).label("day")).cte(
            "report_days", recursive=True
        )
        next_day = (
            cast(cast(days.c.day, Date) + 1, String)
            if self.postgres
            else func.docket_work_add_day(days.c.day)
        )
        days = days.union_all(select(next_day).where(next_day < self.parameters["date_to"]))

        def midnight(day: Any) -> Any:
            if self.postgres:
                return func.timezone(self.zone, cast(day, DateTime), type_=DateTime(timezone=True))
            return func.docket_work_midnight(day, self.zone, type_=DateTime(timezone=True))

        tomorrow = (
            cast(cast(days.c.day, Date) + 1, String)
            if self.postgres
            else func.docket_work_add_day(days.c.day)
        )
        lower, upper = midnight(days.c.day), midnight(tomorrow)
        start = case((i.start_at > lower, i.start_at), else_=lower)
        end = case((i.end_at < upper, i.end_at), else_=upper)
        return (
            select(
                i.work_ref,
                i.source_ref,
                start.label("start_at"),
                end.label("end_at"),
                i.estimate,
                (days.c.day if self.group_by == "day" else self.week(days.c.day)).label("bucket"),
            )
            .join(days, and_(i.start_at < upper, i.end_at > lower))
            .cte("group_intervals")
        )

    def _parts(self) -> Any:
        r, i = self.time_rows.c, self.group_intervals.c
        zero = literal(0, BigInteger)
        elapsed = self.elapsed(i.end_at, i.start_at)
        timed = select(
            i.work_ref,
            i.bucket,
            case((i.estimate.is_(False), elapsed), else_=zero).label("timed_us"),
            zero.label("duration_only_us"),
            zero.label("unallocated_us"),
            case((i.estimate.is_(True), elapsed), else_=zero).label("estimate_us"),
            i.estimate.is_(False).label("known"),
            literal(False).label("unknown"),
            literal(False).label("unplaced"),
            i.estimate.label("open"),
        )
        allocated = or_(
            r.work_date.is_not(None),
            and_(
                r.period_start >= date.fromisoformat(self.parameters["date_from"]),
                r.period_end <= date.fromisoformat(self.parameters["date_to"]),
            ),
        )
        quantity = case(
            (
                and_(r.time_precision == "duration", r.lifecycle == "recorded"),
                func.coalesce(r.duration_us, zero),
            ),
            else_=zero,
        )
        if self.group_by in {"day", "week"}:
            first = func.coalesce(
                cast(r.work_date, String),
                cast(r.period_start, String),
                self.day(func.coalesce(r.start_at, r.end_at)),
            )
            last = func.coalesce(self.last_day(r.period_end), first)
            if self.group_by == "week":
                first, last = self.week(first), self.week(last)
            bucket = case(
                (r.time_precision == "interval", literal("no_active_time")),
                (r.lifecycle != "recorded", literal("open")),
                (unplaced(r), literal("unplaced")),
                (
                    and_(r.period_start.is_not(None), or_(first != last, ~allocated)),
                    literal("unallocated"),
                ),
                else_=first,
            )
        else:
            bucket = self._dimension(r)
        other = select(
            r.work_ref,
            func.coalesce(bucket, "unplaced").label("bucket"),
            zero.label("timed_us"),
            quantity.label("duration_only_us"),
            case((allocated, zero), else_=quantity).label("unallocated_us"),
            zero.label("estimate_us"),
            and_(r.lifecycle == "recorded", r.duration_us.is_not(None)).label("known"),
            and_(r.lifecycle == "recorded", r.duration_us.is_(None)).label("unknown"),
            unplaced(r).label("unplaced"),
            (r.lifecycle != "recorded").label("open"),
        ).where(
            or_(
                r.time_precision != "interval",
                r.duration_us == 0,
                ~exists(select(1).where(self.intervals.c.source_ref == r.source_ref)),
            )
        )
        return union_all(timed, other).cte("work_quantities")

    def _unique(self, intervals: Any, *, grouped: bool) -> Any:
        i = intervals.c
        prior = func.max(i.end_at).over(
            partition_by=i.bucket if grouped else None,
            order_by=(i.start_at, i.end_at, i.work_ref),
            rows=(None, -1),
        )
        ordered = select(*i, prior.label("prior_end")).where(i.estimate.is_(False)).cte()
        o = ordered.c
        fresh = case(
            (
                or_(o.prior_end.is_(None), o.prior_end <= o.start_at),
                self.elapsed(o.end_at, o.start_at),
            ),
            (o.end_at <= o.prior_end, literal(0, BigInteger)),
            else_=self.elapsed(o.end_at, o.prior_end),
        )
        if grouped:
            return (
                select(o.bucket, func.sum(fresh).label("unique_timed_us"))
                .group_by(o.bucket)
                .subquery()
            )
        return select(func.coalesce(func.sum(fresh), 0))

    def stats(self, *, grouped: bool = False) -> Any:
        p, r = self.parts.c, self.rows.c
        counters = {
            "known_quantity_count": p.known,
            "unknown_quantity_count": p.unknown,
            "unplaced_count": p.unplaced,
            "classification_unknown_count": or_(
                r.reported_compensation_class.is_(None), r.reported_compensation_class == "unknown"
            ),
            "open_timer_count": p.open,
        }
        columns = [
            func.count(distinct(p.work_ref)).label("session_count"),
            *(
                func.count(distinct(case((condition, p.work_ref)))).label(key)
                for key, condition in counters.items()
            ),
            *(
                func.coalesce(func.sum(getattr(p, key)), 0).label(key)
                for key in ("timed_us", "duration_only_us", "unallocated_us", "estimate_us")
            ),
        ]
        if grouped and self.group_by == "engagement":
            columns.append(func.max(r.engagement_label).label("label"))
        query = select(*([p.bucket] if grouped else []), *columns).select_from(
            self.parts.join(self.rows, r.work_ref == p.work_ref),
        )
        if grouped:
            query = query.group_by(p.bucket)
            groups = query.subquery()
            unique = self._unique(self.group_intervals, grouped=True)
            return select(
                *groups.c, func.coalesce(unique.c.unique_timed_us, 0).label("unique_timed_us")
            ).outerjoin(
                unique,
                unique.c.bucket == groups.c.bucket,
            )
        return query

    def total_unique(self) -> int:
        return int(self.session.scalar(self._unique(self.intervals, grouped=False)) or 0)
