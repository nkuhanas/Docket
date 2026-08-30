from datetime import date, datetime

import pytest
from pydantic import ValidationError

from docket.config import get_settings
from docket.schemas.calendar import (
    AllDayEventTiming,
    CalendarRecurrenceInput,
    StandaloneCalendarEventInput,
    TimedEventTiming,
)
from docket.schemas.tracked_context import ReminderPlanInput


def test_reminder_plan_is_a_separate_canonical_primitive() -> None:
    plan = ReminderPlanInput(
        subject_ref="evt_01M1A8P3BN3QBTVNS5JNXJARH3",
        delivery_channels=["docket_queue", "google_popup"],
        lead_seconds=[600, 0, 300],
    )

    assert plan.delivery_channels == ["docket_queue", "google_popup"]
    assert plan.lead_seconds == [600, 300, 0]
    assert "reminder_plan" not in StandaloneCalendarEventInput.model_fields


@pytest.mark.parametrize("lead_seconds", [[30], [2_419_260], [60, 60]])
def test_reminder_plan_rejects_non_provider_leads(lead_seconds: list[int]) -> None:
    with pytest.raises(ValidationError, match=r"lead|Google popup"):
        ReminderPlanInput(
            subject_ref="evt_01M1A8P3BN3QBTVNS5JNXJARH3",
            delivery_channels=["google_popup"],
            lead_seconds=lead_seconds,
        )


def test_docket_queue_reminder_does_not_require_provider_compatible_leads() -> None:
    assert ReminderPlanInput(
        subject_ref="evt_01M1A8P3BN3QBTVNS5JNXJARH3",
        delivery_channels=["docket_queue"],
        lead_seconds=[30],
    ).lead_seconds == [30]


def test_timed_event_rejects_dst_gap() -> None:
    with pytest.raises(ValidationError, match="nonexistent daylight-saving"):
        TimedEventTiming(
            kind="timed",
            start_local=datetime(2026, 3, 8, 2, 15),
            end_local=datetime(2026, 3, 8, 2, 45),
            timezone="America/Los_Angeles",
        )


def test_standalone_timing_defaults_to_configured_timezone(monkeypatch) -> None:
    monkeypatch.setenv("DOCKET_TIMEZONE", "America/New_York")
    get_settings.cache_clear()
    try:
        timed = TimedEventTiming(
            kind="timed",
            start_local=datetime(2026, 7, 30, 12, 0),
            end_local=datetime(2026, 7, 30, 12, 15),
        )
        all_day = AllDayEventTiming(
            kind="all_day",
            start_date=date(2026, 7, 30),
            end_date=date(2026, 7, 31),
        )
    finally:
        get_settings.cache_clear()

    assert timed.timezone == "America/New_York"
    assert all_day.timezone == "America/New_York"


def test_explicit_standalone_timezone_overrides_configured_default(monkeypatch) -> None:
    monkeypatch.setenv("DOCKET_TIMEZONE", "America/New_York")
    get_settings.cache_clear()
    try:
        timing = TimedEventTiming(
            kind="timed",
            start_local=datetime(2026, 7, 30, 12, 0),
            end_local=datetime(2026, 7, 30, 12, 15),
            timezone="Europe/London",
        )
    finally:
        get_settings.cache_clear()

    assert timing.timezone == "Europe/London"


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
    payload = {
        "title": "Check my email",
        "timing": {
            "kind": "all_day",
            "start_date": date(2026, 7, 30),
            "end_date": date(2026, 7, 31),
        },
        "priority": "urgent",
    }
    with pytest.raises(ValidationError, match="authenticated Priority control"):
        StandaloneCalendarEventInput.model_validate(payload)

    explicitly_selected = StandaloneCalendarEventInput.model_validate(
        payload,
        context={"allow_explicit_priority": True},
    )
    assert explicitly_selected.priority == "urgent"
