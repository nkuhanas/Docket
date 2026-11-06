"""Source-grounded planning packets; inference stays with the connected agent."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.config import Settings
from docket.domain.errors import DocketError
from docket.domain.work_time import work_zone
from docket.domain.workload_time import Interval, clipped, clock_window, quantity, subtract
from docket.models import (
    CompensationPolicy,
    CompensationPolicyRevision,
    CompensationRevisionTerm,
    CompensationTerm,
    Item,
    Task,
    TemporalBinding,
    WorkReportSnapshot,
    WorkSession,
    WorkSessionRevision,
)
from docket.models.base import utc_now
from docket.schemas.tracked_context import TemporalValue
from docket.schemas.work import QueryWorkSessions
from docket.schemas.workload import GetWorkloadContext, ProjectBudgetSpec, WorkloadPreferenceSpec
from docket.services.compensation_policies import date_boundary, instant, term_spec
from docket.services.financial_state import FinancialState, amount_text
from docket.services.native_context import NativeContextService
from docket.services.work_report_query import WorkReportQuery
from docket.services.work_reports import ReportCursor, WorkReportService, metrics
from docket.services.workload_calendar import WorkloadCalendarService
from docket.services.workload_preferences import WorkloadPreferenceService

HOUR = 3_600_000_000


def display_hours(value: int) -> str:
    return format(Decimal(value) / HOUR, ".6f")


class WorkloadContextService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session, self.settings = session, settings

    def preferences(
        self, project: str | None
    ) -> tuple[WorkloadPreferenceSpec, list[dict[str, Any]], list[str]]:
        service = WorkloadPreferenceService(self.session)
        if project:
            service.project(project, active=False)
        preferences, flags = [], []
        availability = WorkloadPreferenceSpec()
        for row in service.rules(project):
            try:
                spec = service.parse(row)
            except (DocketError, ValueError, ValidationError):
                flags.append("invalid_planning_preference")
                preferences.append({"ref": row.ref_id, "version": row.version, "invalid": True})
                continue
            if isinstance(spec, WorkloadPreferenceSpec):
                availability = spec
            values = spec.model_dump(mode="json", exclude_none=True)
            omitted = []
            if values.get("notes") and len(values["notes"]) > 160:
                values["notes"] = values["notes"][:160]
                omitted.append("notes")
            preferences.append(
                {
                    "ref": row.ref_id,
                    "version": row.version,
                    "kind": "availability"
                    if isinstance(spec, WorkloadPreferenceSpec)
                    else "project_budget",
                    "values": values,
                    "omitted_fields": omitted,
                }
            )
        return availability, preferences, flags

    def capacity(
        self,
        bounds: Interval,
        rules: WorkloadPreferenceSpec,
        calendar: Any,
        as_of: datetime,
        display_zone: str = "UTC",
    ) -> dict[str, Any]:
        zone = work_zone(display_zone)
        planning = (min(max(bounds[0], as_of), bounds[1]), bounds[1])
        configured = rules.available_start is not None and rules.weekdays is not None
        available: list[Interval] = []
        protected: list[Interval] = []
        gaps = []
        policy_zone_name = rules.timezone or self.settings.timezone
        policy_zone = work_zone(policy_zone_name)
        local_lower = planning[0].astimezone(policy_zone).replace(tzinfo=None)
        local_upper = planning[1].astimezone(policy_zone).replace(tzinfo=None)
        day = bounds[0].astimezone(policy_zone).date() - timedelta(days=1)
        through = bounds[1].astimezone(policy_zone).date() + timedelta(days=1)
        while day < through:
            for is_available, start, end in (
                (True, rules.available_start, rules.available_end),
                (False, rules.protected_start, rules.protected_end),
            ):
                if start is None or end is None:
                    continue
                if is_available and (rules.weekdays is None or day.weekday() not in rules.weekdays):
                    continue
                local_start = datetime.combine(day, start)
                local_end = datetime.combine(day + timedelta(days=int(end < start)), end)
                if (
                    local_end <= local_lower
                    or local_start >= local_upper
                    or planning[0] == planning[1]
                ):
                    continue
                try:
                    window = clock_window(day, start, end, policy_zone_name)
                    (available if is_available else protected).append(window)
                except DocketError:
                    gaps.append(day.isoformat())
            day += timedelta(days=1)
        busy = []
        travel = []
        for row in calendar.rows:
            if not row.all_day or row.travel:
                busy.append((row.start, row.end))
            if row.travel and rules.travel_buffer_minutes:
                buffer = timedelta(minutes=rules.travel_buffer_minutes)
                travel.extend([(row.start - buffer, row.start), (row.end, row.end + buffer)])
        busy = clipped(busy, planning)
        protected = clipped(protected, planning)
        travel = clipped(travel, planning)
        available = clipped(available, planning)
        potential = subtract(available, busy + protected + travel)
        source_free = subtract([planning], busy + protected + travel)
        return {
            "planning_start": planning[0].astimezone(zone).isoformat(),
            "planning_end": planning[1].astimezone(zone).isoformat(),
            "known_busy_union_us": quantity(busy),
            "known_busy_hours": display_hours(quantity(busy)),
            "commitment_overlap_excess_us": sum(
                quantity(clipped([(row.start, row.end)], planning))
                for row in calendar.rows
                if not row.all_day or row.travel
            )
            - quantity(busy),
            "firm_commitment_union_us": quantity(
                clipped(
                    [
                        (row.start, row.end)
                        for row in calendar.rows
                        if row.firmness == "firm" and (not row.all_day or row.travel)
                    ],
                    planning,
                )
            ),
            "tentative_commitment_union_us": quantity(
                clipped(
                    [
                        (row.start, row.end)
                        for row in calendar.rows
                        if row.firmness == "tentative" and (not row.all_day or row.travel)
                    ],
                    planning,
                )
            ),
            "protected_union_us": quantity(protected),
            "travel_buffer_union_us": quantity(travel),
            "calendar_unoccupied_us": quantity(source_free),
            "calendar_unoccupied_hours": display_hours(quantity(source_free)),
            "potential_available_us": quantity(potential) if configured and not gaps else None,
            "potential_available_hours": display_hours(quantity(potential))
            if configured and not gaps
            else None,
            "availability_configured": configured,
            "availability_timezone": policy_zone_name,
            "unresolved_clock_days": sorted(set(gaps)),
            "potential_windows": [
                {"start": a.astimezone(zone).isoformat(), "end": b.astimezone(zone).isoformat()}
                for a, b in potential[:5]
            ],
            "potential_window_count": len(potential),
            "all_day_unallocated_count": sum(
                row.all_day and not row.travel for row in calendar.rows
            ),
            "protected_commitment_overlap_us": quantity(protected)
            - quantity(subtract(protected, busy)),
            "meaning": "Upper bound under known constraints; "
            "unscheduled time is not guaranteed usable capacity.",
        }

    def deadlines(self, last: datetime, as_of: datetime, zone_name: str) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(TemporalBinding, Task, Item)
            .outerjoin(Task, Task.ref_id == TemporalBinding.subject_ref)
            .outerjoin(Item, Item.ref_id == TemporalBinding.subject_ref)
            .where(TemporalBinding.canonical_status == "active", TemporalBinding.role == "due_by")
        )
        result = []
        for binding, task, item in rows:
            target = task if task is not None else item
            if target is None or target.canonical_status != "active":
                continue
            if task and task.task_state in {"completed", "cancelled"}:
                continue
            try:
                value: Any = TypeAdapter(TemporalValue).validate_python(binding.temporal_value)
                zone = work_zone(value.timezone or zone_name)
                if value.kind == "datetime":
                    point = value.local_datetime.replace(
                        tzinfo=zone, fold=value.fold or 0
                    ).astimezone(UTC)
                    due_day = point.astimezone(zone).date()
                    overdue = point < as_of
                elif value.kind == "date":
                    due_day = value.date
                    point = date_boundary(due_day, str(zone), "deadline")
                    overdue = due_day < as_of.astimezone(zone).date()
                elif value.kind == "date_interval":
                    due_day = (
                        value.end_date
                        if value.end_inclusive
                        else value.end_date - timedelta(days=1)
                    )
                    point = date_boundary(value.start_date, str(zone), "deadline")
                    overdue = due_day < as_of.astimezone(zone).date()
                elif value.kind == "datetime_interval":
                    point = value.start_local.replace(tzinfo=zone, fold=value.fold or 0).astimezone(
                        UTC
                    )
                    final = value.end_local.replace(tzinfo=zone, fold=value.fold or 0).astimezone(
                        UTC
                    )
                    due_day = final.astimezone(zone).date()
                    overdue = final < as_of
                else:
                    continue
            except (DocketError, ValueError, ValidationError):
                continue
            if point < last:
                result.append(
                    {
                        "ref": target.ref_id,
                        "version": target.version,
                        "time_ref": binding.ref_id,
                        "time_version": binding.version,
                        "title": target.title[:180],
                        "due_on": due_day.isoformat(),
                        "due_at": point.isoformat() if value.kind == "datetime" else None,
                        "timezone": str(zone),
                        "time_value": value.model_dump(mode="json"),
                        "overdue": overdue,
                        "state": task.task_state if task else "tracked_item",
                    }
                )
        return sorted(result, key=lambda row: (not row["overdue"], row["due_on"], row["ref"]))

    def limits(self, first: datetime, last: datetime) -> list[dict[str, Any]]:
        policy, revision, membership, term = (
            CompensationPolicy,
            CompensationPolicyRevision,
            CompensationRevisionTerm,
            CompensationTerm,
        )
        rows = self.session.execute(
            select(policy, term)
            .join(
                revision,
                (revision.policy_ref == policy.ref_id) & (revision.revision == policy.version),
            )
            .join(membership, membership.revision_ref == revision.ref_id)
            .join(term, term.ref_id == membership.term_ref)
            .where(
                policy.operator_ref == require_principal("read").operator_ref,
                policy.kind == "reporting",
                revision.sealed.is_(True),
                term.start_at < last,
                or_(term.end_at.is_(None), term.end_at > first),
            )
        )
        return [
            {
                "engagement_ref": head.engagement_ref,
                "policy_ref": head.ref_id,
                "policy_version": head.version,
                "term_ref": entry.ref_id,
                **{
                    key: value
                    for key, value in term_spec(entry)
                    .model_dump(mode="json", exclude_none=True)
                    .items()
                    if key
                    in {
                        "start_at",
                        "end_at",
                        "timezone",
                        "hour_limit_week",
                        "week_starts_on",
                        "period_days",
                        "anchor_date",
                        "group_with_payer",
                    }
                },
            }
            for head, entry in rows
        ]

    def budget(
        self, data: dict[str, Any], first: date, last: date, default_zone: str
    ) -> dict[str, Any]:
        spec = ProjectBudgetSpec.model_validate(data["values"])
        zone = spec.timezone or self.settings.timezone
        budget_zone = work_zone(zone)
        local_first = date_boundary(first, default_zone, "date_from").astimezone(budget_zone).date()
        local_last = (
            (date_boundary(last, default_zone, "date_to") - timedelta(microseconds=1))
            .astimezone(budget_zone)
            .date()
        )
        first_week = local_first - timedelta(days=local_first.weekday())
        end_week = local_last + timedelta(days=7 - local_last.weekday())
        query = QueryWorkSessions(
            project_ref=spec.project_ref,
            date_from=first_week,
            date_to=end_week,
            timezone=zone,
            group_by="week",
            limit=100,
        )
        report = WorkReportService(self.session, self.settings)._capture(query)
        engine = WorkReportQuery(self.session, report)
        weeks = [
            {"bucket": row["bucket"], **metrics(row)}
            for row in self.session.execute(engine.stats(grouped=True)).mappings()
        ]
        limit = int(Fraction(spec.weekly_hours) * HOUR)
        exceeded = [row for row in weeks if row["allocated_us"] > limit]
        return {
            "ref": data["ref"],
            "version": data["version"],
            "project_ref": spec.project_ref,
            "weekly_hours": str(spec.weekly_hours),
            "budget_basis": "current reported project preference",
            "timezone": zone,
            "week_starts_on": 0,
            "evaluated_date_from": first_week.isoformat(),
            "evaluated_date_to": end_week.isoformat(),
            "over_budget_week_count": len(exceeded),
            "weeks": [
                {
                    key: row[key]
                    for key in (
                        "bucket",
                        "allocated_us",
                        "unallocated_us",
                        "unknown_quantity_count",
                        "overlap_us",
                    )
                }
                for row in weeks[:3]
            ],
            "week_count": len(weeks),
            "detail": {
                "tool": "docket_query_work_sessions",
                "cursor": ReportCursor(report_ref=report.ref_id).encode(),
            },
        }

    def money(self, as_of: datetime) -> dict[str, Any]:
        state = FinancialState(self.session, as_of=as_of)
        values: dict[str, Fraction] = {}
        for part in state.parts:
            values[part.currency] = (
                values.get(part.currency, Fraction(0)) + state.match.outstanding[part.ref]
            )
        overdue = []
        for ref, target in state.sources.targets.items():
            if (
                target.get("due_on")
                and target["due_on"]
                < as_of.astimezone(work_zone(target.get("timezone") or self.settings.timezone))
                .date()
                .isoformat()
                and any(state.match.outstanding[p.ref] > 0 for p in state.target_parts(ref))
            ):
                overdue.append(
                    {"ref": ref, "version": target["version"], "due_on": target["due_on"]}
                )
        return {
            "outstanding_by_currency": [
                {"currency": code, "amount": amount_text(amount)}
                for code, amount in sorted(values.items())[:3]
            ],
            "currency_count": len(values),
            "overdue_document_count": len(overdue),
            "overdue_documents": overdue[:3],
            "unmatched_entry_count": sum(bool(x) for x in state.match.unmatched.values()),
            "detail": {"tool": "docket_get_financial_context"},
            "meaning": "Posted shared obligations, separate from "
            "estimated earnings and received money.",
        }

    def activity_candidates(self, calendar: Any, as_of: datetime) -> list[dict[str, Any]]:
        candidates = [
            row
            for row in calendar.rows
            if row.work_related
            and row.source == "canonical"
            and row.ref
            and row.end <= as_of
            and not row.all_day
        ]
        linked_rows = list(
            self.session.scalars(
                select(WorkSessionRevision)
                .join(
                    WorkSession,
                    (WorkSession.ref_id == WorkSessionRevision.work_ref)
                    & (WorkSession.version == WorkSessionRevision.revision),
                )
                .where(
                    WorkSession.operator_ref == require_principal("read").operator_ref,
                    WorkSession.lifecycle != "retracted",
                    WorkSession.superseded.is_(False),
                    WorkSessionRevision.event_ref.in_({row.ref for row in candidates}),
                )
            )
        )
        result = []
        for row in candidates:
            linked = [work for work in linked_rows if work.event_ref == row.ref]
            matched = bool(linked) if row.occurrence_date is None else False
            for work in linked:
                if work.start_at and work.end_at:
                    matched |= instant(work.start_at) < row.end and instant(work.end_at) > row.start
                elif work.work_date:
                    work_zone_value = work_zone(work.timezone)
                    same_day = [
                        candidate
                        for candidate in candidates
                        if candidate.ref == row.ref
                        and candidate.start.astimezone(work_zone_value).date() == work.work_date
                    ]
                    matched |= len(same_day) == 1 and row in same_day
            if matched:
                continue
            result.append(
                {
                    "ref": row.ref,
                    "version": row.version,
                    "title": row.title[:160],
                    "scheduled_start": row.start.isoformat(),
                    "scheduled_end": row.end.isoformat(),
                    "occurrence_date": row.occurrence_date,
                    "linked_work_refs": [work.work_ref for work in linked[:3]],
                    "meaning": "Work-related plan without a clearly matched current work report. "
                    "Review activity; this is not evidence of attendance or performed/paid hours.",
                }
            )
        return result

    def query(self, public: GetWorkloadContext) -> dict[str, Any]:
        require_principal("read")
        if self.session.get_bind().dialect.name == "postgresql":
            self.session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        as_of = utc_now()
        rules, preferences, flags = self.preferences(public.project_ref)
        zone_name = public.timezone or rules.timezone or self.settings.timezone
        zone = work_zone(zone_name)
        first_day = public.date_from or as_of.astimezone(zone).date()
        last_day = public.date_to or first_day + timedelta(days=7)
        if not 0 < (last_day - first_day).days <= 366:
            raise DocketError(
                code="invalid_workload_period",
                message="Use an exclusive date_to within 366 days after date_from.",
            )
        first, last = (
            date_boundary(first_day, zone_name, "date_from"),
            date_boundary(last_day, zone_name, "date_to"),
        )
        calendar = WorkloadCalendarService(self.session, self.settings).capture(first, last, as_of)
        candidates = self.activity_candidates(calendar, as_of)
        report = WorkReportService(self.session, self.settings).query(
            QueryWorkSessions(
                date_from=first_day,
                date_to=last_day,
                timezone=zone_name,
                project_ref=public.project_ref,
                as_of=as_of,
                limit=3,
            )
        )
        retained = self.session.scalar(
            select(WorkReportSnapshot).where(WorkReportSnapshot.ref_id == report["report"]["ref"])
        )
        assert retained is not None
        work = WorkReportQuery(self.session, retained)
        outcomes = [
            {
                "ref": row["work_ref"],
                "version": row["revision"],
                "revision_ref": row["ref_id"],
                "activity": row["description"][:180],
                "asserted_outcome": row["outcome"][:240],
                "project_ref": row["project_ref"],
                "engagement_ref": row["engagement_ref"],
            }
            for row in self.session.execute(
                select(work.rows)
                .where(work.rows.c.outcome.is_not(None), work.rows.c.outcome != "")
                .order_by(work.rows.c.created_at.desc())
                .limit(10)
            ).mappings()
        ]
        deadlines = self.deadlines(last, as_of, zone_name)
        limits = self.limits(first, last)
        budget_rules = [entry for entry in preferences if entry.get("kind") == "project_budget"]
        budgets = [
            self.budget(entry, first_day, last_day, zone_name) for entry in budget_rules[:10]
        ]
        snapshot: dict[str, Any] = {
            "as_of": as_of.isoformat(),
            "date_from": first_day.isoformat(),
            "date_to": last_day.isoformat(),
            "timezone": zone_name,
            "capacity": self.capacity((first, last), rules, calendar, as_of, zone_name),
            "commitments": [row.public(zone_name) for row in calendar.rows[: public.limit]],
            "commitment_count": len(calendar.rows),
            "activity_review_candidates": candidates[:3],
            "activity_review_candidate_count": len(candidates),
            "deadlines": deadlines[: public.limit],
            "deadline_count": len(deadlines),
            "work": {
                "totals": report["totals"],
                "by_engagement": report["groups"],
                "engagement_group_count": self.session.scalar(
                    select(func.count()).select_from(work.stats(grouped=True).subquery())
                ),
                "detail": {"tool": "docket_query_work_sessions", "cursor": report["report_cursor"]},
            },
            "outcomes": outcomes,
            "outcome_count": self.session.scalar(
                select(func.count())
                .select_from(work.rows)
                .where(work.rows.c.outcome.is_not(None), work.rows.c.outcome != "")
            ),
            "preferences": preferences[:10],
            "preference_count": len(preferences),
            "reporting_limits": limits[:10],
            "reporting_limit_count": len(limits),
            "project_budgets": budgets,
            "project_budget_count": len(budget_rules),
            "money": self.money(as_of),
            "coverage": {
                "canonical_calendar_complete": calendar.canonical_complete,
                "provider_calendars": calendar.coverage[:10],
                "provider_calendar_count": len(calendar.coverage),
                "provider_calendar_complete": bool(calendar.coverage)
                and all(row["complete"] for row in calendar.coverage)
                and not calendar.flags.intersection(
                    {
                        "unresolved_provider_calendar_timing",
                        "unresolved_provider_snapshot_generation",
                    }
                ),
                "flags": sorted(set(flags) | calendar.flags),
                "scope": "Calendar includes all known commitments; "
                "project filters actual work/budgets only.",
                "actual": "Reported work/outcomes only; missing records never prove "
                "inactivity or completeness.",
                "limits": "Configured weekly limits retain their effective basis; "
                "this range may omit part of an employer week.",
                "calendar": "Cached observations; no refresh. "
                "All-day dates without travel are unallocated constraints.",
                "dimensions": "planned_activity and firmness are independent "
                "reported labels. Unknown stays unknown.",
            },
            "guide": "Use event context_labels planned_work, firm, tentative or travel "
            "only when reported. Set availability/protected windows when useful. "
            "Calendar attendance and payment are never inferred. "
            "Use work query for full outcomes/history and financial context for money detail.",
        }
        while len(json.dumps(snapshot, ensure_ascii=False).encode()) > 6900:
            lists = [
                snapshot[key]
                for key in (
                    "commitments",
                    "deadlines",
                    "outcomes",
                    "reporting_limits",
                    "project_budgets",
                    "activity_review_candidates",
                )
            ] + [
                snapshot["coverage"]["provider_calendars"],
                snapshot["work"]["by_engagement"],
                snapshot["capacity"]["potential_windows"],
            ]
            if not public.project_ref:
                lists.append(snapshot["preferences"])
            reducible_lists = [values for values in lists if len(values) > 1]
            if reducible_lists:
                max(
                    reducible_lists,
                    key=lambda values: len(json.dumps(values, ensure_ascii=False).encode()),
                ).pop()
            else:
                raise DocketError(
                    code="workload_context_too_large", message="Select a narrower workload period."
                )
        versions = {
            row["ref"]: row["version"]
            for key in (
                "commitments",
                "deadlines",
                "outcomes",
                "preferences",
                "activity_review_candidates",
            )
            for row in snapshot[key]
            if row.get("ref") and row.get("version")
        }
        versions.update({row["time_ref"]: row["time_version"] for row in snapshot["deadlines"]})
        if public.project_ref:
            project = WorkloadPreferenceService(self.session).project(
                public.project_ref, active=False
            )
            snapshot["project"] = {
                "ref": project.ref_id,
                "version": project.version,
                "display_name": project.display_name[:160],
            }
            versions[project.ref_id] = project.version
        return NativeContextService(self.session, self.settings).retain(
            "workload", snapshot, versions
        )
