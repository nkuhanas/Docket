from __future__ import annotations

from calendar import monthrange
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.errors import DocketError
from docket.models import CalendarDateBinding, CanonicalEvent, EventOccurrence, OperatorUtterance
from docket.schemas.calendar import (
    AllDayEventTiming,
    CalendarEventTiming,
    CalendarRecurrenceInput,
    StandaloneCalendarEventInput,
    TimedEventTiming,
)
from docket.schemas.event_occurrences import (
    OccurrenceEditPlan,
    OccurrenceIdentity,
    OccurrenceReplacement,
    ResolvedCalendarDate,
)

_WEEKDAYS = {name: i for i, name in enumerate(("MO", "TU", "WE", "TH", "FR", "SA", "SU"))}
_MAX_RECURRENCE_PERIODS = 12_000


def resolve_calendar_date(
    *,
    utterance_ref: str,
    message_instant: datetime,
    timezone: str,
    relative_day: Literal["today", "tomorrow"],
) -> ResolvedCalendarDate:
    """Resolve once; callers persist this value instead of resolving again on retry."""
    if message_instant.tzinfo is None:
        raise ValueError("calendar date resolution requires an authenticated absolute instant")
    selected = message_instant.astimezone(ZoneInfo(timezone)).date()
    if relative_day == "tomorrow":
        selected += timedelta(days=1)
    return ResolvedCalendarDate(
        date=selected,
        timezone=timezone,
        source_utterance_ref=utterance_ref,
        relative_day=relative_day,
    )


def bind_calendar_date(
    session: Session,
    *,
    utterance_ref: str,
    timezone: str,
    relative_day: Literal["today", "tomorrow"],
) -> ResolvedCalendarDate:
    # Lock the parent before the first insert, including on independent readers.
    utterance = session.scalar(
        select(OperatorUtterance).where(OperatorUtterance.ref_id == utterance_ref).with_for_update()
    )
    if utterance is None:
        raise DocketError(
            code="utterance_not_found", message="Calendar context requires a captured utterance."
        )
    existing = session.scalar(
        select(CalendarDateBinding).where(
            CalendarDateBinding.utterance_ref == utterance_ref,
            CalendarDateBinding.relative_day == relative_day,
        )
    )
    if existing is not None:
        return ResolvedCalendarDate(
            date=existing.resolved_date,
            timezone=existing.timezone,
            source_utterance_ref=utterance_ref,
            relative_day=relative_day,
        )
    instant = utterance.said_at
    if instant.tzinfo is None:  # SQLite fixture; PostgreSQL retains the absolute instant.
        instant = instant.replace(tzinfo=UTC)
    resolved = resolve_calendar_date(
        utterance_ref=utterance_ref,
        message_instant=instant,
        timezone=timezone,
        relative_day=relative_day,
    )
    session.add(
        CalendarDateBinding(
            utterance_ref=utterance_ref,
            relative_day=relative_day,
            resolved_date=resolved.date,
            timezone=resolved.timezone,
        )
    )
    session.flush()
    return resolved


def _recurrence_dates(
    start: date, recurrence: CalendarRecurrenceInput, through: date
) -> Iterator[date]:
    """Bounded supported RRULE enumeration; COUNT is evaluated before exclusions."""
    emitted = 0
    until = min(through, recurrence.until_date or date.max)
    week_start = start - timedelta(days=start.weekday())
    for period in range(_MAX_RECURRENCE_PERIODS):
        offset = period * recurrence.interval
        try:
            if recurrence.frequency == "daily":
                candidates = [start + timedelta(days=offset)]
                period_start = candidates[0]
            elif recurrence.frequency == "weekly":
                period_start = week_start + timedelta(weeks=offset)
                candidates = [
                    period_start + timedelta(days=_WEEKDAYS[day])
                    for day in sorted(recurrence.weekdays, key=_WEEKDAYS.__getitem__)
                ]
            else:
                month = start.year * 12 + start.month - 1 + offset
                year, zero_month = divmod(month, 12)
                period_start = date(year, zero_month + 1, 1)
                last_day = monthrange(year, zero_month + 1)[1]
                candidates = [
                    date(year, zero_month + 1, day)
                    for day in recurrence.month_days
                    if day <= last_day
                ]
        except (OverflowError, ValueError):
            return
        if period_start > until:
            return
        for candidate in candidates:
            if candidate < start:
                continue
            if candidate > until:
                return
            emitted += 1
            yield candidate
            if recurrence.count is not None and emitted >= recurrence.count:
                return
    raise DocketError(
        code="recurrence_workload_limit",
        message="Occurrence lookup exceeds the bounded recurrence horizon.",
        details={"limit_periods": _MAX_RECURRENCE_PERIODS},
    )


def occurrence_timing(
    event: StandaloneCalendarEventInput,
    selected_date: date,
    *,
    fold: Literal[0, 1] | None = None,
) -> CalendarEventTiming:
    if event.recurrence is None:
        raise DocketError(code="event_is_not_recurring", message="Target is not a series.")
    seed = event.timing
    start = seed.start_date if isinstance(seed, AllDayEventTiming) else seed.start_local.date()
    recurrence = event.recurrence
    exists = selected_date in recurrence.additional_dates or any(
        candidate == selected_date
        for candidate in _recurrence_dates(start, recurrence, selected_date)
    )
    if not exists:
        raise DocketError(
            code="occurrence_not_found",
            message="The selected original date is not an occurrence of this series.",
            details={"original_date": selected_date.isoformat()},
        )
    if isinstance(seed, AllDayEventTiming):
        return AllDayEventTiming(
            kind="all_day",
            start_date=selected_date,
            end_date=selected_date + (seed.end_date - seed.start_date),
            timezone=seed.timezone,
        )
    start_local = datetime.combine(selected_date, seed.start_local.time())
    return TimedEventTiming(
        kind="timed",
        start_local=start_local,
        end_local=start_local + (seed.end_local - seed.start_local),
        timezone=seed.timezone,
        fold=fold,
    )


def identity_for_timing(series_ref: str, timing: CalendarEventTiming) -> OccurrenceIdentity:
    if isinstance(timing, AllDayEventTiming):
        return OccurrenceIdentity(
            series_ref=series_ref, original_date=timing.start_date, timezone=timing.timezone
        )
    return OccurrenceIdentity(
        series_ref=series_ref,
        original_date=timing.start_local.date(),
        timezone=timing.timezone,
        original_start_local=timing.start_local,
        fold=timing.fold,
    )


class EventOccurrenceService:
    """Plan exact occurrence effects without executing providers or granting authority."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, identity: OccurrenceIdentity) -> EventOccurrence | None:
        return self.session.scalar(
            select(EventOccurrence).where(
                EventOccurrence.series_ref == identity.series_ref,
                EventOccurrence.original_start_key == identity.coordinate_key,
            )
        )

    def plan(
        self, identity: OccurrenceIdentity, replacement: OccurrenceReplacement | None = None
    ) -> OccurrenceEditPlan:
        series = self.session.scalar(
            select(CanonicalEvent).where(CanonicalEvent.ref_id == identity.series_ref)
        )
        if series is None or series.status != "active":
            raise DocketError(
                code="series_unavailable", message="The selected series is not active."
            )
        event = StandaloneCalendarEventInput.model_validate(series.event_spec)
        if event.recurrence is None:
            raise DocketError(code="event_is_not_recurring", message="Target is not a series.")
        prior = self.get(identity)
        if prior is not None:
            original = OccurrenceIdentity.model_validate(prior.identity_json)
            if identity != original:
                raise DocketError(
                    code="occurrence_identity_mismatch",
                    message="Recovery must use the original occurrence timezone and coordinate.",
                )
            original_timing = prior.original_timing_json
        else:
            timing = occurrence_timing(event, identity.original_date, fold=identity.fold)
            if identity_for_timing(series.ref_id, timing) != identity:
                raise DocketError(
                    code="occurrence_identity_mismatch",
                    message="Selected coordinate does not match the canonical original start.",
                )
            original_timing = timing.model_dump(mode="json")
        recurrence = event.recurrence.model_dump(mode="json")
        selected = identity.original_date.isoformat()
        already_excluded = selected in recurrence["excluded_dates"]
        # Removing RDATE is the precise exclusion when this date was added; a
        # profile cannot contain the same date in both RDATE and EXDATE.
        if selected in recurrence["additional_dates"]:
            recurrence["additional_dates"].remove(selected)
        # A previously moved RDATE may already have disappeared from the master.
        # Do not introduce unrelated EXDATEs when modifying that replacement.
        seed = event.timing
        start = seed.start_date if isinstance(seed, AllDayEventTiming) else seed.start_local.date()
        rule_date = any(
            d == identity.original_date
            for d in _recurrence_dates(start, event.recurrence, identity.original_date)
        )
        if rule_date and not already_excluded:
            recurrence["excluded_dates"].append(selected)
        recurrence["excluded_dates"].sort()
        master_after = {**series.event_spec, "recurrence": recurrence}
        # Validate the complete result, including the explicit exception budget.
        StandaloneCalendarEventInput.model_validate(master_after)
        replacement_after: dict[str, Any] | None = None
        if replacement is not None:
            replacement_after = {
                **event.model_dump(mode="json"),
                **replacement.model_dump(mode="json"),
                "recurrence": None,
            }
            StandaloneCalendarEventInput.model_validate(replacement_after)
        no_op = False
        if replacement is None:
            no_op = prior is not None and prior.status == "cancelled"
        elif prior is not None and prior.status == "replaced":
            child = self.session.scalar(
                select(CanonicalEvent).where(CanonicalEvent.ref_id == prior.replacement_event_ref)
            )
            no_op = child is not None and child.event_spec == replacement_after
        return OccurrenceEditPlan(
            identity=identity,
            original_timing=original_timing,
            series_version=series.version,
            occurrence_version=prior.version if prior else None,
            master_after=master_after,
            replacement_event_ref=prior.replacement_event_ref if prior else None,
            replacement_after=replacement_after,
            status="replaced" if replacement is not None else "cancelled",
            no_op=no_op,
        )

    def record_applied(
        self,
        plan: OccurrenceEditPlan,
        *,
        changeset_ref: str,
        basis_refs: list[str],
        replacement_event_ref: str | None,
    ) -> EventOccurrence:
        """Called in the canonical transaction after the plan's ordinary effects apply."""
        prior = self.get(plan.identity)
        if (prior.version if prior else None) != plan.occurrence_version:
            raise DocketError(
                code="occurrence_version_conflict", message="The occurrence changed after staging."
            )
        if prior is not None and plan.no_op:
            return prior
        if prior is None:
            prior = EventOccurrence(
                series_ref=plan.identity.series_ref,
                original_start_key=plan.identity.coordinate_key,
                original_local_date=plan.identity.original_date,
                original_timezone=plan.identity.timezone,
                identity_json=plan.identity.model_dump(mode="json"),
                original_timing_json=plan.original_timing,
                version=1,
            )
            self.session.add(prior)
        else:
            prior.version += 1
        prior.status = plan.status
        prior.replacement_event_ref = replacement_event_ref
        prior.last_changeset_ref = changeset_ref
        prior.basis_refs = list(basis_refs)
        self.session.flush()
        return prior
