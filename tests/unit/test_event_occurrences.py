from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.schemas.calendar import StandaloneCalendarEventInput
from docket.schemas.event_occurrences import OccurrenceIdentity, ResolvedCalendarDate
from docket.services.event_occurrences import (
    identity_for_timing,
    occurrence_timing,
    resolve_calendar_date,
)


def series_spec(**updates) -> StandaloneCalendarEventInput:
    return StandaloneCalendarEventInput.model_validate(
        {
            "title": "MATH 1263",
            "calendar_lane": "math-1263",
            "timing": {
                "kind": "timed",
                "start_local": "2026-08-24T15:00:00",
                "end_local": "2026-08-24T15:50:00",
                "timezone": "America/Los_Angeles",
            },
            "recurrence": {
                "frequency": "weekly",
                "weekdays": ["MO", "TU", "TH", "FR"],
                "until_date": "2026-10-14",
                "excluded_dates": ["2026-09-07"],
            },
            **updates,
        }
    )


def test_relative_date_is_bound_to_original_instant_and_timezone() -> None:
    binding = resolve_calendar_date(
        utterance_ref=new_public_ref("utt"),
        message_instant=datetime(2026, 9, 12, 1, tzinfo=UTC),
        timezone="America/Los_Angeles",
        relative_day="tomorrow",
    )
    assert binding.date == date(2026, 9, 12)
    stored = binding.model_dump(mode="json")
    assert ResolvedCalendarDate.model_validate(stored).timezone == "America/Los_Angeles"
    # Reload uses the persisted binding, not a new clock or a new setting.
    assert ResolvedCalendarDate.model_validate(stored) == binding
    with pytest.raises(ValueError, match="absolute instant"):
        resolve_calendar_date(
            utterance_ref=new_public_ref("utt"),
            message_instant=datetime(2026, 9, 11),
            timezone="UTC",
            relative_day="tomorrow",
        )


def test_original_coordinate_survives_provider_independent_serialization() -> None:
    timing = occurrence_timing(series_spec(), date(2026, 9, 8))
    identity = identity_for_timing(new_public_ref("evt"), timing)
    assert identity.coordinate_key == "timed:2026-09-08T15:00:00-07:00"
    assert OccurrenceIdentity.model_validate_json(identity.model_dump_json()) == identity
    assert timing.end_local == datetime(2026, 9, 8, 15, 50)


@pytest.mark.parametrize("selected", ["2026-08-23", "2026-09-09", "2026-10-15"])
def test_non_occurrence_dates_reject(selected) -> None:
    with pytest.raises(DocketError) as exc:
        occurrence_timing(series_spec(), date.fromisoformat(selected))
    assert exc.value.code == "occurrence_not_found"


def test_excluded_date_remains_an_addressable_original_occurrence() -> None:
    assert occurrence_timing(series_spec(), date(2026, 9, 7)).start_local.date() == date(2026, 9, 7)


def test_count_is_evaluated_before_exclusions_and_rdates_are_independent() -> None:
    spec = series_spec(
        recurrence={
            "frequency": "daily",
            "count": 3,
            "excluded_dates": ["2026-08-25"],
            "additional_dates": ["2026-09-01"],
        }
    )
    assert occurrence_timing(spec, date(2026, 8, 26))
    assert occurrence_timing(spec, date(2026, 9, 1))
    with pytest.raises(DocketError):
        occurrence_timing(spec, date(2026, 8, 27))


def test_monthly_invalid_dates_do_not_consume_count() -> None:
    spec = series_spec(
        timing={
            "kind": "all_day",
            "start_date": "2026-01-31",
            "end_date": "2026-02-01",
            "timezone": "UTC",
        },
        recurrence={"frequency": "monthly", "month_days": [31], "count": 3},
    )
    assert occurrence_timing(spec, date(2026, 5, 31)).end_date == date(2026, 6, 1)
    with pytest.raises(DocketError):
        occurrence_timing(spec, date(2026, 7, 31))


def test_weekly_interval_is_anchored_to_monday() -> None:
    spec = series_spec(
        recurrence={
            "frequency": "weekly",
            "weekdays": ["MO", "FR"],
            "interval": 2,
            "count": 4,
        }
    )
    assert occurrence_timing(spec, date(2026, 8, 28))
    assert occurrence_timing(spec, date(2026, 9, 11))
    with pytest.raises(DocketError):
        occurrence_timing(spec, date(2026, 9, 4))


def test_dst_identity_uses_original_zoned_start_and_requires_ambiguous_fold() -> None:
    spec = series_spec(
        timing={
            "kind": "timed",
            "start_local": "2026-10-25T01:10:00",
            "end_local": "2026-10-25T01:50:00",
            "timezone": "America/Los_Angeles",
        },
        recurrence={"frequency": "weekly", "weekdays": ["SU"], "count": 3},
    )
    with pytest.raises(ValidationError, match="requires fold"):
        occurrence_timing(spec, date(2026, 11, 1))
    ref = new_public_ref("evt")
    early = identity_for_timing(ref, occurrence_timing(spec, date(2026, 11, 1), fold=0))
    late = identity_for_timing(ref, occurrence_timing(spec, date(2026, 11, 1), fold=1))
    assert early.coordinate_key.endswith("-07:00")
    assert late.coordinate_key.endswith("-08:00")
    assert early.coordinate_key != late.coordinate_key
    with pytest.raises(ValidationError, match="does not exist"):
        OccurrenceIdentity(
            series_ref=ref,
            original_date=date(2026, 3, 8),
            original_start_local=datetime(2026, 3, 8, 2, 30),
            timezone="America/Los_Angeles",
        )
