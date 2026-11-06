"""Canonical commitments plus bounded, honestly covered Calendar observations."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import or_, select, tuple_
from sqlalchemy.orm import Session

from docket.config import Settings
from docket.domain.errors import DocketError
from docket.domain.work_time import work_zone
from docket.models import (
    CalendarEventCache,
    CalendarLane,
    CalendarSyncState,
    CanonicalEvent,
    EventOccurrence,
    ProviderAccount,
    ProviderEventBinding,
)
from docket.schemas.calendar import StandaloneCalendarEventInput, TimedEventTiming
from docket.services.compensation_policies import instant
from docket.services.event_occurrences import _recurrence_dates


@dataclass
class Commitment:
    ref: str | None
    version: int | None
    title: str
    start: datetime
    end: datetime
    source: str
    lane_ref: str | None = None
    location: str | None = None
    all_day: bool = False
    planned_activity: bool | None = None
    firmness: str = "unknown"
    travel: bool = False
    occurrence_date: str | None = None
    work_related: bool | None = None

    def public(self, display_zone: str = "UTC") -> dict[str, Any]:
        zone = work_zone(display_zone)
        return {
            "ref": self.ref,
            "version": self.version,
            "title": self.title[:180],
            "start": self.start.astimezone(zone).isoformat(),
            "end": self.end.astimezone(zone).isoformat(),
            "source": self.source,
            "lane_ref": self.lane_ref,
            "location": self.location[:180] if self.location else None,
            "all_day": self.all_day,
            "planned_activity": self.planned_activity,
            "firmness": self.firmness,
            "travel": self.travel,
            "occurrence_date": self.occurrence_date,
            "work_related": self.work_related,
        }


@dataclass
class CalendarWorkload:
    rows: list[Commitment] = field(default_factory=list)
    coverage: list[dict[str, Any]] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    canonical_complete: bool = True


class WorkloadCalendarService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session, self.settings = session, settings

    def capture(self, first: datetime, last: datetime, as_of: datetime) -> CalendarWorkload:
        result = CalendarWorkload()
        # Canonical recurrence is authoritative even before Google delivery.
        events = list(
            self.session.scalars(
                select(CanonicalEvent)
                .where(
                    CanonicalEvent.status == "active",
                )
                .order_by(CanonicalEvent.ref_id)
                .limit(10001)
            )
        )
        if len(events) > 10000:
            result.canonical_complete = False
            result.flags.add("canonical_calendar_scan_incomplete")
        retained = {row.ref_id for row in events[:10000]}
        overrides = set(
            self.session.execute(
                select(EventOccurrence.series_ref, EventOccurrence.original_local_date)
            ).all()
        )
        for row in events[:10000]:
            try:
                spec = StandaloneCalendarEventInput.model_validate(
                    row.event_spec, context={"allow_explicit_priority": True}
                )
                timing = spec.timing
                zone = ZoneInfo(timing.timezone or self.settings.timezone)
                seed = timing.start_date if timing.kind == "all_day" else timing.start_local.date()
                dates = {seed}
                if spec.recurrence:
                    dates = set(
                        _recurrence_dates(seed, spec.recurrence, last.astimezone(zone).date())
                    )
                    dates -= set(spec.recurrence.excluded_dates)
                    dates |= set(spec.recurrence.additional_dates)
                    dates = {day for day in dates if (row.ref_id, day) not in overrides}
                labels = set(row.context_labels) | set(spec.operator_tags)
                firmness = "unknown"
                if ("firm" in labels) != ("tentative" in labels):
                    firmness = "firm" if "firm" in labels else "tentative"
                elif "firm" in labels:
                    result.flags.add("conflicting_reported_commitment_firmness")
                span_days = (
                    (timing.end_date - timing.start_date).days
                    if timing.kind == "all_day"
                    else (timing.end_local.date() - timing.start_local.date()).days
                )
                minimum_day = first.astimezone(zone).date() - timedelta(days=span_days)
                maximum_day = last.astimezone(zone).date()
                for day in sorted(day for day in dates if minimum_day <= day <= maximum_day):
                    if timing.kind == "all_day":
                        start = datetime.combine(day, time(), zone).astimezone(UTC)
                        end = datetime.combine(
                            day + (timing.end_date - timing.start_date), time(), zone
                        ).astimezone(UTC)
                    else:
                        local = datetime.combine(day, timing.start_local.time())
                        local_end = local + (timing.end_local - timing.start_local)
                        if local_end <= first.astimezone(zone).replace(
                            tzinfo=None
                        ) or local >= last.astimezone(zone).replace(tzinfo=None):
                            continue
                        try:
                            TimedEventTiming(
                                kind="timed",
                                start_local=local,
                                end_local=local_end,
                                timezone=str(zone),
                                fold=timing.fold,
                            )
                        except ValidationError:
                            result.canonical_complete = False
                            result.flags.add("unresolved_canonical_calendar_timing")
                            continue
                        start = local.replace(tzinfo=zone, fold=timing.fold or 0).astimezone(UTC)
                        end = (
                            (local + (timing.end_local - timing.start_local))
                            .replace(tzinfo=zone, fold=timing.fold or 0)
                            .astimezone(UTC)
                        )
                    if start < last and end > first:
                        result.rows.append(
                            Commitment(
                                row.ref_id,
                                row.version,
                                row.title,
                                start,
                                end,
                                "canonical",
                                row.lane_ref,
                                spec.location,
                                timing.kind == "all_day",
                                True
                                if labels & {"planned", "planned_work", "work_block"}
                                else None,
                                firmness,
                                "travel" in labels,
                                day.isoformat() if spec.recurrence else None,
                                True if labels & {"work", "planned_work", "work_block"} else None,
                            )
                        )
            except (DocketError, ValidationError, ValueError, OverflowError):
                result.canonical_complete = False
                result.flags.add("unresolved_canonical_calendar_timing")
        lanes = list(
            self.session.scalars(
                select(CalendarLane)
                .join(
                    ProviderAccount,
                    ProviderAccount.id == CalendarLane.account_id,
                )
                .where(
                    CalendarLane.enabled.is_(True),
                    CalendarLane.status == "active",
                    CalendarLane.calendar_id.is_not(None),
                    ProviderAccount.enabled.is_(True),
                )
            )
        )
        states = {
            (s.account_id, s.calendar_id): s
            for s in self.session.scalars(select(CalendarSyncState))
        }
        for lane in lanes:
            assert lane.calendar_id is not None
            state = states.get((lane.account_id, lane.calendar_id))
            complete = bool(
                state
                and state.status == "current"
                and state.last_success_at
                and state.snapshot_generation is not None
                and instant(state.window_start) <= first
                and instant(state.window_end) >= last
                and (as_of - instant(state.last_success_at)).total_seconds()
                <= self.settings.calendar_stale_seconds
            )
            result.coverage.append(
                {
                    "lane_ref": lane.ref_id,
                    "complete": complete,
                    "last_success_at": instant(state.last_success_at).isoformat()
                    if state and state.last_success_at
                    else None,
                    "window_start": instant(state.window_start).isoformat() if state else None,
                    "window_end": instant(state.window_end).isoformat() if state else None,
                }
            )
        if not lanes:
            result.flags.add("provider_calendar_coverage_unavailable")
            return result
        targets = {(lane.account_id, lane.calendar_id): lane.ref_id for lane in lanes}
        coarse_first, coarse_last = (
            first.date() - timedelta(days=2),
            last.date() + timedelta(days=2),
        )
        cached = list(
            self.session.scalars(
                select(CalendarEventCache).where(
                    tuple_(CalendarEventCache.account_id, CalendarEventCache.calendar_id).in_(
                        targets
                    ),
                    CalendarEventCache.status != "cancelled",
                    or_(
                        (CalendarEventCache.start_at < last) & (CalendarEventCache.end_at > first),
                        (CalendarEventCache.start_date < coarse_last)
                        & (CalendarEventCache.end_date > coarse_first),
                    ),
                )
            )
        )
        bindings = {
            (b.account_id, b.calendar_id, b.provider_event_id): b
            for b in self.session.scalars(
                select(ProviderEventBinding).where(
                    tuple_(ProviderEventBinding.account_id, ProviderEventBinding.calendar_id).in_(
                        targets
                    )
                )
            )
        }
        inactive = set(
            self.session.scalars(
                select(CanonicalEvent.ref_id).where(CanonicalEvent.status != "active")
            )
        )
        for cached_row in cached:
            state = states.get((cached_row.account_id, cached_row.calendar_id))
            if state is None or state.snapshot_generation != cached_row.snapshot_generation:
                result.flags.add("unresolved_provider_snapshot_generation")
            binding = bindings.get(
                (cached_row.account_id, cached_row.calendar_id, cached_row.provider_event_id)
            )
            if binding is None and cached_row.recurring_event_id:
                binding = bindings.get(
                    (cached_row.account_id, cached_row.calendar_id, cached_row.recurring_event_id)
                )
            if binding and binding.canonical_target_ref in retained | inactive:
                if binding.status == "diverged":
                    result.flags.add("provider_divergence_needs_reconciliation")
                if binding.canonical_target_ref in inactive and binding.status != "cancelled":
                    result.flags.add("provider_observation_disagrees_with_inactive_canonical")
                continue
            if binding and binding.canonical_target_ref.startswith("work_"):
                continue  # An actual-work visualization is not another commitment.
            if cached_row.is_all_day:
                try:
                    zone = work_zone(cached_row.timezone or self.settings.timezone)
                except DocketError:
                    result.flags.add("unresolved_provider_calendar_timing")
                    continue
                if cached_row.start_date is None or cached_row.end_date is None:
                    result.flags.add("unresolved_provider_calendar_timing")
                    continue
                start = datetime.combine(cached_row.start_date, time(), zone).astimezone(UTC)
                end = datetime.combine(cached_row.end_date, time(), zone).astimezone(UTC)
            else:
                if cached_row.start_at is None or cached_row.end_at is None:
                    result.flags.add("unresolved_provider_calendar_timing")
                    continue
                start, end = instant(cached_row.start_at), instant(cached_row.end_at)
            if start < last and end > first:
                tags = set(cached_row.operator_tags)
                result.rows.append(
                    Commitment(
                        binding.canonical_target_ref if binding else None,
                        None,
                        cached_row.summary or "Calendar observation",
                        start,
                        end,
                        "provider_observation",
                        targets[(cached_row.account_id, cached_row.calendar_id)],
                        cached_row.location,
                        cached_row.is_all_day,
                        True if tags & {"planned", "planned_work", "work_block"} else None,
                        "tentative" if cached_row.status == "tentative" else "unknown",
                        "travel" in tags,
                    )
                )
        result.rows.sort(key=lambda row: (row.start, row.ref or "", row.title))
        return result
