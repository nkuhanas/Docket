from datetime import date, datetime

import pytest
from pydantic import ValidationError

from docket.schemas.calendar import (
    CalendarProfileInput,
    CalendarRecurrenceInput,
    CalendarReminderPlanInput,
    StandaloneCalendarEventInput,
    TimedEventTiming,
)


def test_reminder_plan_is_canonical_and_accepts_explicit_disable() -> None:
    plan = CalendarReminderPlanInput(
        delivery_channels=["docket_queue", "google_popup"],
        lead_seconds=[600, 0, 300],
    )

    assert plan.delivery_channels == ["google_popup", "docket_queue"]
    assert plan.lead_seconds == [0, 300, 600]
    assert CalendarReminderPlanInput(lead_seconds=[]).lead_seconds == []


@pytest.mark.parametrize("lead_seconds", [[30], [2_419_260], [60, 60]])
def test_reminder_plan_rejects_non_provider_leads(lead_seconds: list[int]) -> None:
    with pytest.raises(ValidationError, match="reminder leads"):
        CalendarReminderPlanInput(lead_seconds=lead_seconds)


def test_reminder_plan_requires_both_delivery_projections() -> None:
    with pytest.raises(ValidationError, match="at least 2 items"):
        CalendarReminderPlanInput(delivery_channels=["google_popup"])


def test_timed_event_rejects_dst_gap() -> None:
    with pytest.raises(ValidationError, match="nonexistent daylight-saving"):
        TimedEventTiming(
            kind="timed",
            start_local=datetime(2026, 3, 8, 2, 15),
            end_local=datetime(2026, 3, 8, 2, 45),
            timezone="America/Los_Angeles",
        )


def test_timed_event_requires_fold_for_ambiguous_time() -> None:
    with pytest.raises(ValidationError, match="requires fold"):
        TimedEventTiming(
            kind="timed",
            start_local=datetime(2026, 11, 1, 1, 15),
            end_local=datetime(2026, 11, 1, 1, 45),
            timezone="America/Los_Angeles",
        )

    timing = TimedEventTiming(
        kind="timed",
        start_local=datetime(2026, 11, 1, 1, 15),
        end_local=datetime(2026, 11, 1, 1, 45),
        timezone="America/Los_Angeles",
        fold=1,
    )
    assert timing.fold == 1


def test_recurrence_requires_bound_and_matching_selector() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        CalendarRecurrenceInput(frequency="daily")
    with pytest.raises(ValidationError, match="weekly recurrence requires weekdays"):
        CalendarRecurrenceInput(frequency="weekly", count=5)
    with pytest.raises(ValidationError, match="valid only for weekly"):
        CalendarRecurrenceInput(frequency="daily", count=5, weekdays=["MO"])


def test_standalone_event_derives_classification_and_normalizes_tags() -> None:
    event = StandaloneCalendarEventInput.model_validate(
        {
            "title": "Check my email",
            "timing": {
                "kind": "timed",
                "start_local": "2026-07-30T12:00:00",
                "end_local": "2026-07-30T12:15:00",
                "timezone": "America/Los_Angeles",
            },
            "operator_tags": [" Work ", "EMAIL"],
            "recurrence": {
                "frequency": "weekly",
                "weekdays": ["TH"],
                "until_date": "2026-08-27",
            },
        }
    )

    assert event.operator_tags == ["email", "work"]
    assert event.recurrence_kind == "recurring"
    assert event.system_tags == ["recurring", "timed", "standalone"]


def test_standalone_initial_priority_is_conservative() -> None:
    with pytest.raises(ValidationError, match="authenticated Priority control"):
        StandaloneCalendarEventInput.model_validate(
            {
                "title": "Check my email",
                "timing": {
                    "kind": "all_day",
                    "start_date": date(2026, 7, 30),
                    "end_date": date(2026, 7, 31),
                },
                "priority": "urgent",
            }
        )


def test_calendar_profile_normalizes_reminder_defaults() -> None:
    profile = CalendarProfileInput(
        default_reminder_lead_seconds=[600, 300],
        default_reminder_delivery_channels=["docket_queue", "google_popup"],
    )

    assert profile.default_reminder_lead_seconds == [300, 600]
    assert profile.default_reminder_delivery_channels == [
        "google_popup",
        "docket_queue",
    ]
