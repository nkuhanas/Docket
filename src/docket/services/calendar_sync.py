from __future__ import annotations

import time as monotonic_time
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from threading import Thread
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import Settings, get_settings
from docket.domain.enums import OutboxStatus
from docket.domain.errors import DocketError
from docket.models import (
    Account,
    CalendarEventCache,
    CalendarSyncState,
    OutboxEvent,
    ScheduledNotification,
)
from docket.models.base import utc_now
from docket.providers.google.calendar import (
    CalendarProviderError,
    CalendarReadProvider,
    CalendarSnapshotEvent,
)

logger = structlog.get_logger(__name__)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _iso(value: datetime | None) -> str | None:
    return _aware(value).astimezone(UTC).isoformat() if value is not None else None


class CalendarSyncService:
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
            datetime_time.min,
            tzinfo=zone,
        )
        end = datetime.combine(
            today + timedelta(days=self.settings.calendar_sync_future_days + 1),
            datetime_time.min,
            tzinfo=zone,
        )
        return start.astimezone(UTC), end.astimezone(UTC)

    def _configured_account(self, session: Session, account_id: uuid.UUID) -> Account:
        account = session.get(Account, account_id)
        if account is None or not account.enabled or account.provider != "google":
            raise DocketError(
                code="calendar_account_not_available",
                message="The selected Google account is not enabled.",
                details={"account_id": str(account_id)},
            )
        return account

    def _validate_target(self, session: Session, account_id: uuid.UUID, calendar_id: str) -> None:
        self._configured_account(session, account_id)
        if calendar_id != self.settings.google_calendar_id:
            raise DocketError(
                code="calendar_not_allowed",
                message="The selected calendar is not the configured Docket calendar.",
                details={"calendar_id": calendar_id},
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
                "Calendar snapshot contained an invalid event identity.",
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
                "Calendar snapshot contained invalid event time bounds.",
                transient=False,
            )

    def _fetch(
        self, calendar_id: str, window_start: datetime, window_end: datetime
    ) -> list[CalendarSnapshotEvent]:
        events: list[CalendarSnapshotEvent] = []
        identities: set[str] = set()
        seen_tokens: set[str] = set()
        page_token: str | None = None
        for _page_number in range(self.settings.calendar_snapshot_max_pages):
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
                        "Calendar snapshot repeated an event identity.",
                        transient=False,
                    )
                identities.add(event.provider_event_id)
                events.append(event)
                if len(events) > self.settings.calendar_snapshot_max_events:
                    raise CalendarProviderError(
                        "calendar_snapshot_too_large",
                        "Calendar snapshot exceeded its configured event bound.",
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
            "Calendar snapshot exceeded its configured page bound.",
            transient=False,
        )

    @staticmethod
    def _cancel_notification(session: Session, notification: ScheduledNotification) -> None:
        notification.status = "cancelled"
        notification.last_error_code = "calendar_event_removed"
        if notification.outbox_event_id is not None:
            event = session.get(OutboxEvent, notification.outbox_event_id)
            if event is not None and event.status == OutboxStatus.PENDING.value:
                event.status = OutboxStatus.FAILED.value
                event.last_error_code = "notification_cancelled"

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
                ).all()
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
                row.provider_etag = event.provider_etag
                row.provider_updated_at = event.provider_updated_at
                row.synced_at = now
            removed = [row for event_id, row in existing.items() if event_id not in seen]
            if removed:
                removed_ids = [row.id for row in removed]
                notifications = session.scalars(
                    select(ScheduledNotification).where(
                        ScheduledNotification.calendar_event_id.in_(removed_ids)
                    )
                ).all()
                for notification in notifications:
                    if notification.status in {"pending", "delivering"}:
                        self._cancel_notification(session, notification)
                    notification.calendar_event_id = None
                session.execute(
                    delete(CalendarEventCache).where(CalendarEventCache.id.in_(removed_ids))
                )
            session.flush()
            from docket.services.reminders import materialize_reminders

            materialize_reminders(session, now=now)
            state.snapshot_generation = generation
            state.window_start = window_start
            state.window_end = window_end
            state.status = "current"
            state.last_success_at = now
            state.last_error_code = None
            state.lease_token = None
            state.leased_until = None

    def _mark_failed(self, state_id: uuid.UUID, lease_token: uuid.UUID, code: str) -> None:
        now = _aware(self.clock()).astimezone(UTC)
        with self.session_factory.begin() as session:
            state = session.get(CalendarSyncState, state_id)
            if state is None or state.lease_token != lease_token:
                return
            state.status = "stale" if state.last_success_at is not None else "failed"
            state.last_error_code = code[:128]
            state.lease_token = None
            state.leased_until = None
            self._ensure_stale_alert(session, state, now)

    def _ensure_stale_alert(
        self, session: Session, state: CalendarSyncState, now: datetime
    ) -> bool:
        stale = (
            state.last_success_at is None
            or (now - _aware(state.last_success_at)).total_seconds()
            > self.settings.calendar_stale_seconds
        )
        if not stale:
            return False
        episode = (
            _aware(state.last_success_at).astimezone(UTC).isoformat()
            if state.last_success_at is not None
            else "never"
        )
        key = f"discord_system_alert:calendar_stale:{state.id}:{episode}"
        if session.scalar(select(OutboxEvent).where(OutboxEvent.deduplication_key == key)) is None:
            alert_id = uuid.uuid5(uuid.NAMESPACE_URL, key)
            session.add(
                OutboxEvent(
                    id=alert_id,
                    event_type="discord.system_alert.requested",
                    aggregate_type="calendar_sync_state",
                    aggregate_id=state.id,
                    deduplication_key=key,
                    payload={
                        "title": "Docket Calendar synchronization is stale",
                        "summary": (
                            "Calendar lookups may be outdated. Docket retained the last complete "
                            "snapshot and did not promote partial provider data."
                        ),
                        "error_code": state.last_error_code or "calendar_sync_stale",
                        "occurred_at": now.isoformat(),
                    },
                    status=OutboxStatus.PENDING.value,
                )
            )
        return True

    def evaluate_staleness(self) -> int:
        now = _aware(self.clock()).astimezone(UTC)
        count = 0
        with self.session_factory.begin() as session:
            states = session.scalars(select(CalendarSyncState)).all()
            for state in states:
                if self._ensure_stale_alert(session, state, now):
                    if state.status == "current":
                        state.status = "stale"
                    count += 1
        return count

    def sync_target(self, account_id: uuid.UUID, calendar_id: str, *, force: bool = False) -> bool:
        claimed = self._claim(account_id, calendar_id, force=force)
        if claimed is None:
            return False
        state_id, lease_token, window_start, window_end = claimed
        try:
            events = self._fetch(calendar_id, window_start, window_end)
            self._promote(state_id, lease_token, window_start, window_end, events)
        except CalendarProviderError as exc:
            self._mark_failed(state_id, lease_token, exc.code)
        except Exception:
            self._mark_failed(state_id, lease_token, "calendar_sync_unexpected")
            raise
        return True

    def run_due_once(self) -> bool:
        with self.session_factory() as session:
            accounts = session.scalars(
                select(Account)
                .where(Account.provider == "google", Account.enabled.is_(True))
                .order_by(Account.id)
            ).all()
        for account in accounts:
            if self.sync_target(account.id, self.settings.google_calendar_id):
                return True
        return False

    def require_fresh(self, account_id: uuid.UUID, calendar_id: str) -> None:
        started = _aware(self.clock()).astimezone(UTC)

        def refresh() -> None:
            try:
                self.sync_target(account_id, calendar_id, force=True)
            except Exception:
                logger.exception(
                    "calendar_require_fresh_refresh_failed",
                    account_id=str(account_id),
                    calendar_id=calendar_id,
                )

        Thread(target=refresh, name="docket-calendar-refresh", daemon=True).start()
        deadline = monotonic_time.monotonic() + self.settings.calendar_require_fresh_wait_seconds
        while monotonic_time.monotonic() < deadline:
            with self.session_factory() as session:
                state = session.scalar(
                    select(CalendarSyncState).where(
                        CalendarSyncState.account_id == account_id,
                        CalendarSyncState.calendar_id == calendar_id,
                    )
                )
                if (
                    state is not None
                    and state.status == "current"
                    and state.last_success_at is not None
                    and _aware(state.last_success_at) >= started
                ):
                    return
                if (
                    state is not None
                    and state.status in {"failed", "stale"}
                    and state.last_attempt_at is not None
                    and _aware(state.last_attempt_at) >= started
                ):
                    return
            monotonic_time.sleep(0.05)

    def recover_expired_leases(self) -> int:
        now = _aware(self.clock()).astimezone(UTC)
        with self.session_factory.begin() as session:
            states = session.scalars(
                select(CalendarSyncState).where(
                    CalendarSyncState.status == "syncing",
                    CalendarSyncState.leased_until < now,
                )
            ).all()
            for state in states:
                state.status = "stale" if state.last_success_at is not None else "failed"
                state.last_error_code = "calendar_sync_lease_expired"
                state.lease_token = None
                state.leased_until = None
            return len(states)


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

    def _status(self, state: CalendarSyncState | None) -> dict[str, Any]:
        now = _aware(self.clock()).astimezone(UTC)
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
            state = session.scalar(
                select(CalendarSyncState).where(
                    CalendarSyncState.account_id == account_id,
                    CalendarSyncState.calendar_id == calendar_id,
                )
            )
            account = session.get(Account, account_id)
            assert account is not None
            return {
                "account_id": str(account_id),
                "account_label": account.display_name
                or account.email_address
                or account.external_account_id,
                "calendar_id": calendar_id,
                **self._status(state),
            }

    def list_events(
        self,
        *,
        account_id: uuid.UUID,
        calendar_id: str,
        start: datetime,
        end: datetime,
        text_filter: str | None,
        limit: int,
        freshness: str,
    ) -> dict[str, Any]:
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise DocketError(
                code="invalid_calendar_range",
                message="Calendar lookup bounds must be ordered timezone-aware instants.",
            )
        if end - start > timedelta(days=31):
            raise DocketError(
                code="calendar_range_too_large",
                message="Calendar lookups are limited to 31 days.",
            )
        if not 1 <= limit <= 100:
            raise DocketError(code="invalid_limit", message="Calendar limit must be from 1 to 100.")
        if text_filter is not None and len(text_filter) > 200:
            raise DocketError(
                code="calendar_filter_too_large",
                message="Calendar text filters are limited to 200 characters.",
            )
        self.sync_service.ensure_state(account_id, calendar_id)
        if freshness == "require_fresh":
            if self.settings.calendar_reads_enabled:
                self.sync_service.require_fresh(account_id, calendar_id)
        elif freshness != "prefer_cache":
            raise DocketError(
                code="invalid_freshness",
                message="Calendar freshness must be prefer_cache or require_fresh.",
            )

        start_utc = start.astimezone(UTC)
        end_utc = end.astimezone(UTC)
        zone = ZoneInfo(self.settings.timezone)
        start_date = start.astimezone(zone).date()
        end_local = end.astimezone(zone)
        end_date = end_local.date()
        if any((end_local.hour, end_local.minute, end_local.second, end_local.microsecond)):
            end_date += timedelta(days=1)
        with self.session_factory() as session:
            state = session.scalar(
                select(CalendarSyncState).where(
                    CalendarSyncState.account_id == account_id,
                    CalendarSyncState.calendar_id == calendar_id,
                )
            )
            statement = select(CalendarEventCache).where(
                CalendarEventCache.account_id == account_id,
                CalendarEventCache.calendar_id == calendar_id,
                CalendarEventCache.status != "cancelled",
                or_(
                    (
                        CalendarEventCache.is_all_day.is_(False)
                        & (CalendarEventCache.start_at < end_utc)
                        & (CalendarEventCache.end_at > start_utc)
                    ),
                    (
                        CalendarEventCache.is_all_day.is_(True)
                        & (CalendarEventCache.start_date < end_date)
                        & (CalendarEventCache.end_date > start_date)
                    ),
                ),
            )
            if text_filter:
                statement = statement.where(
                    or_(
                        func.lower(CalendarEventCache.summary).contains(
                            text_filter.casefold(), autoescape=True
                        ),
                        func.lower(CalendarEventCache.location).contains(
                            text_filter.casefold(), autoescape=True
                        ),
                    )
                )
            rows = list(session.scalars(statement).all())
            rows.sort(
                key=lambda row: (
                    row.start_at.astimezone(UTC)
                    if row.start_at is not None
                    else datetime.combine(
                        row.start_date or date.max, datetime_time.min, tzinfo=zone
                    ).astimezone(UTC),
                    row.provider_event_id,
                )
            )
            result = [
                {
                    "provider_event_id": row.provider_event_id,
                    "recurring_event_id": row.recurring_event_id,
                    "status": row.status,
                    "summary": row.summary,
                    "location": row.location,
                    "is_all_day": row.is_all_day,
                    "start_at": _iso(row.start_at),
                    "end_at": _iso(row.end_at),
                    "start_date": row.start_date.isoformat() if row.start_date else None,
                    "end_date": row.end_date.isoformat() if row.end_date else None,
                    "timezone": row.timezone,
                }
                for row in rows[:limit]
            ]
            status = self._status(state)
            covered = bool(
                state is not None
                and state.snapshot_generation is not None
                and state.last_success_at is not None
                and _aware(state.window_start) <= start_utc
                and _aware(state.window_end) >= end_utc
            )
            return {
                "account_id": str(account_id),
                "calendar_id": calendar_id,
                "range_start": start_utc.isoformat(),
                "range_end": end_utc.isoformat(),
                "events": result,
                "freshness": {**status, "covered": covered},
                "refresh_pending": bool(
                    freshness == "require_fresh"
                    and self.settings.calendar_reads_enabled
                    and (status["stale"] or not covered)
                ),
                "refresh_disabled": bool(
                    freshness == "require_fresh" and not self.settings.calendar_reads_enabled
                ),
            }
