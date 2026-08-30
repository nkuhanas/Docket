from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import Settings, get_settings
from docket.domain.errors import DocketError
from docket.models import (
    AuditEvent,
    CalendarEventCache,
    CalendarLane,
    CalendarSyncState,
    CanonicalEvent,
    ProviderAccount,
    ProviderEventBinding,
    ReminderPlan,
    TemporalCalendarProjection,
)
from docket.models.base import utc_now
from docket.providers.google.calendar import (
    CalendarProviderError,
    CalendarReadProvider,
    CalendarSnapshotEvent,
)
from docket.services.calendar_lanes import CalendarLaneService


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _iso(value: datetime | None) -> str | None:
    return _aware(value).astimezone(UTC).isoformat() if value is not None else None


class CalendarSyncService:
    """Bounded provider snapshot ingestion into Docket's non-canonical read cache."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: CalendarReadProvider,
        settings: Settings | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.session_factory = session_factory
        self.provider = provider
        self.settings = settings or get_settings()
        self.clock = clock

    def _window(self, now: datetime) -> tuple[datetime, datetime]:
        zone = ZoneInfo(self.settings.timezone)
        today = _aware(now).astimezone(zone).date()
        start = datetime.combine(
            today - timedelta(days=self.settings.calendar_sync_past_days),
            time.min,
            tzinfo=zone,
        )
        end = datetime.combine(
            today + timedelta(days=self.settings.calendar_sync_future_days + 1),
            time.min,
            tzinfo=zone,
        )
        return start.astimezone(UTC), end.astimezone(UTC)

    def _validate_target(self, session: Session, account_id: uuid.UUID, calendar_id: str) -> None:
        account = session.get(ProviderAccount, account_id)
        if account is None or account.provider != "google" or not account.enabled:
            raise DocketError(
                code="calendar_account_not_available",
                message="The selected Google account is not enabled.",
            )
        CalendarLaneService(session, self.settings).require_active(
            account_id, calendar_id=calendar_id
        )

    def ensure_state(self, account_id: uuid.UUID, calendar_id: str) -> uuid.UUID:
        now = _aware(self.clock()).astimezone(UTC)
        window_start, window_end = self._window(now)
        with self.session_factory.begin() as session:
            self._validate_target(session, account_id, calendar_id)
            state = session.scalar(
                select(CalendarSyncState).where(
                    CalendarSyncState.account_id == account_id,
                    CalendarSyncState.calendar_id == calendar_id,
                )
            )
            if state is None:
                state = CalendarSyncState(
                    account_id=account_id,
                    calendar_id=calendar_id,
                    window_start=window_start,
                    window_end=window_end,
                    status="pending",
                )
                session.add(state)
                session.flush()
            return state.id

    def _claim(
        self, account_id: uuid.UUID, calendar_id: str, *, force: bool
    ) -> tuple[uuid.UUID, uuid.UUID, datetime, datetime] | None:
        state_id = self.ensure_state(account_id, calendar_id)
        now = _aware(self.clock()).astimezone(UTC)
        window_start, window_end = self._window(now)
        with self.session_factory.begin() as session:
            state = session.scalar(
                select(CalendarSyncState).where(CalendarSyncState.id == state_id).with_for_update()
            )
            assert state is not None
            if (
                state.status == "syncing"
                and state.leased_until is not None
                and _aware(state.leased_until) > now
            ):
                return None
            due = (
                state.last_attempt_at is None
                or _aware(state.last_attempt_at)
                + timedelta(seconds=self.settings.calendar_sync_interval_seconds)
                <= now
                or state.status in {"pending", "stale", "failed"}
            )
            if not force and not due:
                return None
            lease_token = uuid.uuid4()
            state.status = "syncing"
            state.last_attempt_at = now
            state.lease_token = lease_token
            state.leased_until = now + timedelta(seconds=self.settings.calendar_sync_lease_seconds)
            state.last_error_code = None
            return state.id, lease_token, window_start, window_end

    @staticmethod
    def _validate_event(event: CalendarSnapshotEvent) -> None:
        if not event.provider_event_id or len(event.provider_event_id) > 1024:
            raise CalendarProviderError(
                "calendar_snapshot_invalid_event",
                "Calendar snapshot contained an invalid event identifier.",
                transient=False,
            )
        if event.status not in {"confirmed", "tentative", "cancelled"}:
            raise CalendarProviderError(
                "calendar_snapshot_invalid_event",
                "Calendar snapshot contained an invalid event status.",
                transient=False,
            )
        if event.status == "cancelled" and all(
            value is None
            for value in (event.start_at, event.end_at, event.start_date, event.end_date)
        ):
            return
        timed = event.start_at is not None or event.end_at is not None
        dated = event.start_date is not None or event.end_date is not None
        if event.is_all_day:
            valid = (
                not timed
                and event.start_date is not None
                and event.end_date is not None
                and event.end_date > event.start_date
            )
        else:
            valid = (
                not dated
                and event.start_at is not None
                and event.end_at is not None
                and event.start_at.tzinfo is not None
                and event.end_at.tzinfo is not None
                and event.end_at > event.start_at
            )
        if not valid:
            raise CalendarProviderError(
                "calendar_snapshot_invalid_event",
                "Calendar snapshot event timing was incomplete.",
                transient=False,
            )

    def _fetch(
        self, calendar_id: str, window_start: datetime, window_end: datetime
    ) -> list[CalendarSnapshotEvent]:
        events: list[CalendarSnapshotEvent] = []
        identities: set[str] = set()
        page_token: str | None = None
        seen_tokens: set[str] = set()
        for _ in range(self.settings.calendar_snapshot_max_pages):
            page = self.provider.list_events_page(
                calendar_id=calendar_id,
                time_min=window_start,
                time_max=window_end,
                page_token=page_token,
            )
            for event in page.events:
                self._validate_event(event)
                if event.provider_event_id in identities:
                    raise CalendarProviderError(
                        "calendar_snapshot_duplicate_event",
                        "Calendar snapshot repeated an event identifier.",
                        transient=False,
                    )
                identities.add(event.provider_event_id)
                events.append(event)
                if len(events) > self.settings.calendar_snapshot_max_events:
                    raise CalendarProviderError(
                        "calendar_snapshot_too_large",
                        "Calendar snapshot exceeded its event bound.",
                        transient=False,
                    )
            page_token = page.next_page_token
            if page_token is None:
                return events
            if page_token in seen_tokens:
                raise CalendarProviderError(
                    "calendar_snapshot_page_loop",
                    "Calendar snapshot repeated a page token.",
                    transient=False,
                )
            seen_tokens.add(page_token)
        raise CalendarProviderError(
            "calendar_snapshot_too_many_pages",
            "Calendar snapshot exceeded its page bound.",
            transient=False,
        )

    def _promote(
        self,
        state_id: uuid.UUID,
        lease_token: uuid.UUID,
        window_start: datetime,
        window_end: datetime,
        events: list[CalendarSnapshotEvent],
    ) -> None:
        now = _aware(self.clock()).astimezone(UTC)
        generation = uuid.uuid4()
        with self.session_factory.begin() as session:
            state = session.scalar(
                select(CalendarSyncState).where(CalendarSyncState.id == state_id).with_for_update()
            )
            if (
                state is None
                or state.status != "syncing"
                or state.lease_token != lease_token
                or state.leased_until is None
                or _aware(state.leased_until) < now
            ):
                raise DocketError(
                    code="calendar_sync_lease_lost",
                    message="Calendar synchronization lease was lost before promotion.",
                )
            existing = {
                row.provider_event_id: row
                for row in session.scalars(
                    select(CalendarEventCache).where(
                        CalendarEventCache.account_id == state.account_id,
                        CalendarEventCache.calendar_id == state.calendar_id,
                    )
                )
            }
            bindings = {
                row.provider_event_id: row
                for row in session.scalars(
                    select(ProviderEventBinding).where(
                        ProviderEventBinding.account_id == state.account_id,
                        ProviderEventBinding.calendar_id == state.calendar_id,
                    )
                )
            }
            seen: set[str] = set()
            for event in events:
                seen.add(event.provider_event_id)
                row = existing.get(event.provider_event_id)
                if row is None:
                    row = CalendarEventCache(
                        account_id=state.account_id,
                        calendar_id=state.calendar_id,
                        provider_event_id=event.provider_event_id,
                        snapshot_generation=generation,
                        status=event.status,
                        is_all_day=event.is_all_day,
                        synced_at=now,
                    )
                    session.add(row)
                row.snapshot_generation = generation
                row.event_type = event.event_type
                row.recurring_event_id = event.recurring_event_id
                row.original_start_at = event.original_start_at
                row.status = event.status
                row.summary = event.summary
                row.location = event.location
                row.is_all_day = event.is_all_day
                row.start_at = event.start_at
                row.end_at = event.end_at
                row.start_date = event.start_date
                row.end_date = event.end_date
                row.timezone = event.timezone
                row.has_attendees = event.has_attendees
                row.organizer_is_self = event.organizer_is_self
                row.recurrence_kind = event.recurrence_kind
                row.system_tags = list(event.system_tags) or [
                    event.recurrence_kind,
                    "all_day" if event.is_all_day else "timed",
                    "external",
                ]
                row.operator_tags = []
                row.priority = "normal"
                row.priority_basis = "default"
                row.provider_reminders = dict(event.provider_reminders or {})
                row.provider_etag = event.provider_etag
                row.provider_updated_at = event.provider_updated_at
                row.synced_at = now
                binding = bindings.get(event.provider_event_id)
                if (
                    binding is not None
                    and binding.status == "active"
                    and binding.provider_etag is not None
                    and event.provider_etag is not None
                    and binding.provider_etag != event.provider_etag
                ):
                    previous_etag = binding.provider_etag
                    binding.status = "diverged"
                    binding.provider_etag = event.provider_etag
                    binding.provider_snapshot = {
                        "status": event.status,
                        "summary": event.summary,
                        "location": event.location,
                        "start_at": _iso(event.start_at),
                        "end_at": _iso(event.end_at),
                        "start_date": event.start_date.isoformat() if event.start_date else None,
                        "end_date": event.end_date.isoformat() if event.end_date else None,
                        "timezone": event.timezone,
                        "reminders": dict(event.provider_reminders or {}),
                    }
                    binding.independently_modified_at = now
                    binding.version += 1
                    session.add(
                        AuditEvent(
                            event_type="calendar.provider_binding_diverged",
                            entity_type="provider_event_binding",
                            entity_id=binding.id,
                            actor_type="provider",
                            actor_id="google_calendar",
                            primary_ref=binding.canonical_target_ref,
                            affected_refs=[binding.canonical_target_ref],
                            data={"previous_provider_etag": previous_etag},
                        )
                    )
            removed_ids = [row.id for event_id, row in existing.items() if event_id not in seen]
            if removed_ids:
                session.execute(
                    delete(CalendarEventCache).where(CalendarEventCache.id.in_(removed_ids))
                )
            state.snapshot_generation = generation
            state.window_start = window_start
            state.window_end = window_end
            state.status = "current"
            state.last_success_at = now
            state.last_error_code = None
            state.lease_token = None
            state.leased_until = None

    def _mark_failed(self, state_id: uuid.UUID, lease_token: uuid.UUID, code: str) -> None:
        with self.session_factory.begin() as session:
            state = session.get(CalendarSyncState, state_id)
            if state is None or state.lease_token != lease_token:
                return
            state.status = "stale" if state.last_success_at is not None else "failed"
            state.last_error_code = code[:128]
            state.lease_token = None
            state.leased_until = None

    def sync_target(self, account_id: uuid.UUID, calendar_id: str, *, force: bool = False) -> bool:
        claim = self._claim(account_id, calendar_id, force=force)
        if claim is None:
            return False
        state_id, lease_token, window_start, window_end = claim
        try:
            events = self._fetch(calendar_id, window_start, window_end)
            self._promote(state_id, lease_token, window_start, window_end, events)
        except CalendarProviderError as exc:
            self._mark_failed(state_id, lease_token, exc.code)
        except Exception:
            self._mark_failed(state_id, lease_token, "calendar_sync_internal_error")
            raise
        return True

    def run_due_once(self) -> bool:
        if not self.settings.calendar_reads_enabled:
            return False
        with self.session_factory() as session:
            target = session.execute(
                select(CalendarLane.account_id, CalendarLane.calendar_id)
                .join(ProviderAccount, ProviderAccount.id == CalendarLane.account_id)
                .where(
                    ProviderAccount.provider == "google",
                    ProviderAccount.enabled.is_(True),
                    CalendarLane.status == "active",
                    CalendarLane.enabled.is_(True),
                    CalendarLane.calendar_id.is_not(None),
                )
                .order_by(CalendarLane.updated_at)
                .limit(1)
            ).first()
        if target is None or target.calendar_id is None:
            return False
        return self.sync_target(target.account_id, target.calendar_id)

    def require_fresh(self, account_id: uuid.UUID, calendar_id: str) -> None:
        if self.settings.calendar_reads_enabled:
            self.sync_target(account_id, calendar_id, force=True)

    def evaluate_staleness(self) -> int:
        now = _aware(self.clock()).astimezone(UTC)
        changed = 0
        with self.session_factory.begin() as session:
            for state in session.scalars(select(CalendarSyncState)):
                if (
                    state.last_success_at is None
                    or (now - _aware(state.last_success_at)).total_seconds()
                    > self.settings.calendar_stale_seconds
                ) and state.status == "current":
                    state.status = "stale"
                    changed += 1
        return changed

    def recover_expired_leases(self) -> int:
        now = _aware(self.clock()).astimezone(UTC)
        recovered = 0
        with self.session_factory.begin() as session:
            for state in session.scalars(
                select(CalendarSyncState).where(
                    CalendarSyncState.status == "syncing",
                    CalendarSyncState.leased_until < now,
                )
            ):
                state.status = "stale" if state.last_success_at else "failed"
                state.last_error_code = "calendar_sync_lease_expired"
                state.lease_token = None
                state.leased_until = None
                recovered += 1
        return recovered


class CalendarReadService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        sync_service: CalendarSyncService,
        settings: Settings | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.session_factory = session_factory
        self.sync_service = sync_service
        self.settings = settings or get_settings()
        self.clock = clock

    def _status_at(self, state: CalendarSyncState | None, now: datetime) -> dict[str, Any]:
        stale = (
            state is None
            or state.last_success_at is None
            or (now - _aware(state.last_success_at)).total_seconds()
            > self.settings.calendar_stale_seconds
            or state.status != "current"
        )
        return {
            "status": state.status if state is not None else "pending",
            "window_start": _iso(state.window_start) if state is not None else None,
            "window_end": _iso(state.window_end) if state is not None else None,
            "last_attempt_at": _iso(state.last_attempt_at) if state is not None else None,
            "last_success_at": _iso(state.last_success_at) if state is not None else None,
            "stale": stale,
            "last_error_code": state.last_error_code if state is not None else None,
        }

    def get_sync_status(self, account_id: uuid.UUID, calendar_id: str) -> dict[str, Any]:
        self.sync_service.ensure_state(account_id, calendar_id)
        with self.session_factory() as session:
            account = session.get(ProviderAccount, account_id)
            state = session.scalar(
                select(CalendarSyncState).where(
                    CalendarSyncState.account_id == account_id,
                    CalendarSyncState.calendar_id == calendar_id,
                )
            )
            if account is None or not account.enabled:
                raise DocketError(
                    code="calendar_account_not_available",
                    message="The selected Google account is not enabled.",
                )
            return {
                "account_ref": account.ref_id,
                "calendar_id": calendar_id,
                **self._status_at(state, _aware(self.clock()).astimezone(UTC)),
            }

    def _range(
        self,
        start: datetime | None,
        end: datetime | None,
        relative_day: str | None,
    ) -> tuple[datetime, datetime, dict[str, Any]]:
        now = _aware(self.clock()).astimezone(UTC)
        zone = ZoneInfo(self.settings.timezone)
        local_date: date | None = None
        if relative_day is not None:
            if relative_day not in {"today", "tomorrow"} or start is not None or end is not None:
                raise DocketError(
                    code="invalid_calendar_range",
                    message="Relative day must stand alone and be today or tomorrow.",
                )
            local_date = now.astimezone(zone).date()
            if relative_day == "tomorrow":
                local_date += timedelta(days=1)
            start = datetime.combine(local_date, time.min, tzinfo=zone)
            end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=zone)
            mode = "relative_day"
        elif start is None and end is None:
            start, end, mode = now, now + timedelta(days=7), "default"
        elif start is None or end is None:
            raise DocketError(
                code="invalid_calendar_range",
                message="Calendar start and end must be supplied together.",
            )
        else:
            mode = "explicit"
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise DocketError(
                code="invalid_calendar_range",
                message="Calendar bounds must be ordered timezone-aware instants.",
            )
        if end - start > timedelta(days=31):
            raise DocketError(
                code="calendar_range_too_large",
                message="Calendar lookups are limited to 31 days.",
            )
        return (
            start.astimezone(UTC),
            end.astimezone(UTC),
            {
                "mode": mode,
                "relative_day": relative_day,
                "local_date": local_date.isoformat() if local_date else None,
                "timezone": self.settings.timezone,
                "as_of": now.isoformat(),
            },
        )

    def list_events(
        self,
        *,
        account_id: uuid.UUID,
        calendar_id: str,
        start: datetime | None,
        end: datetime | None,
        relative_day: str | None = None,
        text_filter: str | None,
        limit: int,
        freshness: str,
        result_view: str = "occurrences",
        offset: int = 0,
    ) -> dict[str, Any]:
        start_utc, end_utc, resolution = self._range(start, end, relative_day)
        if freshness == "require_fresh":
            self.sync_service.require_fresh(account_id, calendar_id)
        elif freshness != "prefer_cache":
            raise DocketError(
                code="invalid_freshness",
                message="Calendar freshness must be prefer_cache or require_fresh.",
            )
        zone = ZoneInfo(self.settings.timezone)
        start_date = start_utc.astimezone(zone).date()
        end_date = end_utc.astimezone(zone).date() + timedelta(days=1)
        with self.session_factory() as session:
            account = session.get(ProviderAccount, account_id)
            state = session.scalar(
                select(CalendarSyncState).where(
                    CalendarSyncState.account_id == account_id,
                    CalendarSyncState.calendar_id == calendar_id,
                )
            )
            if account is None or not account.enabled:
                raise DocketError(
                    code="calendar_account_not_available",
                    message="The selected Google account is not enabled.",
                )
            query = select(CalendarEventCache).where(
                CalendarEventCache.account_id == account_id,
                CalendarEventCache.calendar_id == calendar_id,
                CalendarEventCache.status != "cancelled",
                or_(
                    CalendarEventCache.is_all_day.is_(False)
                    & (CalendarEventCache.start_at < end_utc)
                    & (CalendarEventCache.end_at > start_utc),
                    CalendarEventCache.is_all_day.is_(True)
                    & (CalendarEventCache.start_date < end_date)
                    & (CalendarEventCache.end_date > start_date),
                ),
            )
            if text_filter:
                query = query.where(
                    or_(
                        func.lower(CalendarEventCache.summary).contains(
                            text_filter.casefold(), autoescape=True
                        ),
                        func.lower(CalendarEventCache.location).contains(
                            text_filter.casefold(), autoescape=True
                        ),
                    )
                )
            rows = list(session.scalars(query))
            rows.sort(
                key=lambda row: (
                    _aware(row.start_at).astimezone(UTC)
                    if row.start_at
                    else datetime.combine(row.start_date or date.max, time.min, tzinfo=zone),
                    row.provider_event_id,
                )
            )
            counts: dict[str, int] = {}
            if result_view == "series":
                unique: dict[str, CalendarEventCache] = {}
                for row in rows:
                    key = row.recurring_event_id or row.provider_event_id
                    counts[key] = counts.get(key, 0) + 1
                    unique.setdefault(key, row)
                rows = list(unique.values())
            elif result_view != "occurrences":
                raise DocketError(
                    code="invalid_calendar_result_view",
                    message="Calendar result view must be occurrences or series.",
                )
            total = len(rows)
            selected = rows[offset : offset + limit]
            ids = {
                identity
                for row in selected
                for identity in (row.provider_event_id, row.recurring_event_id)
                if identity is not None
            }
            bindings = {
                row.provider_event_id: row
                for row in session.scalars(
                    select(ProviderEventBinding).where(
                        ProviderEventBinding.account_id == account_id,
                        ProviderEventBinding.calendar_id == calendar_id,
                        ProviderEventBinding.provider_event_id.in_(ids),
                    )
                )
            }
            canonical_refs = {row.canonical_target_ref for row in bindings.values()}
            canonicals = {
                event.ref_id: event
                for event in session.scalars(
                    select(CanonicalEvent).where(CanonicalEvent.ref_id.in_(canonical_refs))
                )
            }
            temporal_projections = {
                projection.ref_id: projection
                for projection in session.scalars(
                    select(TemporalCalendarProjection).where(
                        TemporalCalendarProjection.ref_id.in_(canonical_refs)
                    )
                )
            }
            reminder_subject_refs = {
                *canonicals,
                *(
                    projection.temporal_binding_ref
                    for projection in temporal_projections.values()
                ),
            }
            plans = {
                plan.subject_ref: plan
                for plan in session.scalars(
                    select(ReminderPlan).where(
                        ReminderPlan.subject_ref.in_(reminder_subject_refs),
                        ReminderPlan.canonical_status == "active",
                    )
                )
            }

            def projection(row: CalendarEventCache) -> dict[str, Any]:
                series_identity = row.recurring_event_id or row.provider_event_id
                binding = bindings.get(row.provider_event_id) or bindings.get(series_identity)
                canonical = (
                    canonicals.get(binding.canonical_target_ref) if binding is not None else None
                )
                temporal_projection = (
                    temporal_projections.get(binding.canonical_target_ref)
                    if binding is not None
                    else None
                )
                plan_subject_ref = (
                    canonical.ref_id
                    if canonical is not None
                    else temporal_projection.temporal_binding_ref
                    if temporal_projection is not None
                    else None
                )
                plan = plans.get(plan_subject_ref) if plan_subject_ref is not None else None
                canonical_ref = binding.canonical_target_ref if binding is not None else None
                target_kind = binding.target_kind if binding is not None else None
                lane_ref = (
                    canonical.lane_ref
                    if canonical is not None
                    else temporal_projection.lane_ref
                    if temporal_projection is not None
                    else None
                )
                return {
                    "provider_event_id": (
                        series_identity if result_view == "series" else row.provider_event_id
                    ),
                    **(
                        {
                            "ref": canonical_ref,
                            "lane_ref": lane_ref,
                            "target_kind": target_kind,
                        }
                        if canonical_ref is not None
                        else {}
                    ),
                    "recurring_event_id": row.recurring_event_id,
                    "status": row.status,
                    "summary": row.summary,
                    "location": row.location,
                    "is_all_day": row.is_all_day,
                    "start_at": _iso(row.start_at),
                    "end_at": _iso(row.end_at),
                    "start_local": (
                        _aware(row.start_at).astimezone(zone).isoformat() if row.start_at else None
                    ),
                    "end_local": (
                        _aware(row.end_at).astimezone(zone).isoformat() if row.end_at else None
                    ),
                    "local_timezone": self.settings.timezone,
                    "start_date": row.start_date.isoformat() if row.start_date else None,
                    "end_date": row.end_date.isoformat() if row.end_date else None,
                    "timezone": row.timezone,
                    "event_type": row.event_type,
                    "recurrence_kind": row.recurrence_kind,
                    **(
                        {
                            "scope": "series" if row.recurring_event_id else "event",
                            "occurrences_in_range": counts[series_identity],
                        }
                        if result_view == "series"
                        else {}
                    ),
                    "reminder_plan": (
                        {
                            "state": "canonical",
                            "ref": plan.ref_id,
                            "delivery_channels": plan.delivery_channels,
                            "lead_seconds": plan.lead_seconds,
                        }
                        if plan is not None
                        else {"state": "external_unmanaged"}
                    ),
                }

            events = [projection(row) for row in selected]
            status = self._status_at(state, _aware(self.clock()).astimezone(UTC))
            covered = bool(
                state is not None
                and state.snapshot_generation is not None
                and _aware(state.window_start) <= start_utc
                and _aware(state.window_end) >= end_utc
            )
            return {
                "account_ref": account.ref_id,
                "calendar_id": calendar_id,
                "range_start": start_utc.isoformat(),
                "range_end": end_utc.isoformat(),
                "range_resolution": resolution,
                "result_view": result_view,
                "events": events,
                "count": len(events),
                "total_if_known": total,
                "truncated": offset + len(events) < total,
                "cursor": str(offset + len(events)) if offset + len(events) < total else None,
                "freshness": {**status, "covered": covered},
                "refresh_pending": False,
                "refresh_disabled": freshness == "require_fresh"
                and not self.settings.calendar_reads_enabled,
            }

    def list_events_across_calendars(
        self,
        *,
        account_id: uuid.UUID,
        calendar_ids: list[str],
        start: datetime | None,
        end: datetime | None,
        relative_day: str | None = None,
        text_filter: str | None,
        limit: int,
        freshness: str,
        result_view: str = "occurrences",
        offset: int = 0,
    ) -> dict[str, Any]:
        ordered_calendar_ids = list(dict.fromkeys(calendar_ids))
        if not ordered_calendar_ids:
            raise DocketError(
                code="calendar_lanes_not_available",
                message="The selected account has no active Calendar lanes.",
            )
        if len(ordered_calendar_ids) > 25:
            raise DocketError(
                code="calendar_lane_limit_exceeded",
                message="Aggregate Calendar reads are limited to 25 active lanes.",
            )
        if offset + limit > 10_000:
            raise DocketError(
                code="calendar_aggregate_cursor_too_large",
                message="Aggregate Calendar pagination is limited to 10,000 events.",
            )
        needed = offset + limit
        pages: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        for calendar_id in ordered_calendar_ids:
            calendar_offset = 0
            collected = 0
            while collected < needed:
                page = self.list_events(
                    account_id=account_id,
                    calendar_id=calendar_id,
                    start=start,
                    end=end,
                    relative_day=relative_day,
                    text_filter=text_filter,
                    limit=min(100, needed - calendar_offset),
                    freshness=freshness if calendar_offset == 0 else "prefer_cache",
                    result_view=result_view,
                    offset=calendar_offset,
                )
                if calendar_offset == 0:
                    pages.append(page)
                page_events = [
                    {"calendar_id": calendar_id, **event} for event in page["events"]
                ]
                events.extend(page_events)
                collected += len(page_events)
                next_cursor = page.get("cursor")
                if next_cursor is None or not page_events:
                    break
                calendar_offset = int(next_cursor)
        events.sort(
            key=lambda event: (
                str(event.get("start_local") or event.get("start_date") or ""),
                str(event["calendar_id"]),
                str(event["provider_event_id"]),
            )
        )
        total = sum(int(page["total_if_known"]) for page in pages)
        selected = events[offset : offset + limit]
        return {
            "account_ref": pages[0]["account_ref"],
            "calendar_ids": ordered_calendar_ids,
            "range_start": pages[0]["range_start"],
            "range_end": pages[0]["range_end"],
            "range_resolution": pages[0]["range_resolution"],
            "result_view": result_view,
            "events": selected,
            "count": len(selected),
            "total_if_known": total,
            "truncated": offset + len(selected) < total,
            "cursor": str(offset + len(selected)) if offset + len(selected) < total else None,
            "freshness_by_calendar": {page["calendar_id"]: page["freshness"] for page in pages},
            "refresh_pending": any(page["refresh_pending"] for page in pages),
            "refresh_disabled": any(page["refresh_disabled"] for page in pages),
        }
