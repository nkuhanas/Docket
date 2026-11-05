from decimal import Decimal

import pytest

from docket.domain.errors import DocketError
from docket.domain.work_time import normalize_work_time

ZONE = "America/Los_Angeles"


def test_work_fall_fold_preserves_independent_offsets_and_elapsed_break():
    value = normalize_work_time(
        start="2026-11-01T01:30:00-07:00",
        end="2026-11-01T01:15:00-08:00",
        timezone=ZONE,
        breaks=[("2026-11-01T01:40:00-07:00", "2026-11-01T01:50:00-07:00")],
    )
    assert value.duration_us == 35 * 60_000_000
    assert value.start.offset_seconds == -7 * 3600
    assert value.end.offset_seconds == -8 * 3600
    assert [interval.kind for interval in value.intervals] == ["active", "break", "active"]


def test_work_break_union_is_subtracted_once_and_gaps_remain_explicit():
    value = normalize_work_time(
        start="2026-11-05T10:00:00-08:00",
        end="2026-11-05T13:00:00-08:00",
        timezone=ZONE,
        breaks=[
            ("2026-11-05T11:00:00-08:00", "2026-11-05T11:30:00-08:00"),
            ("2026-11-05T11:15:00-08:00", "2026-11-05T11:45:00-08:00"),
        ],
    )
    assert value.duration_us == 135 * 60_000_000
    assert len([part for part in value.intervals if part.kind == "break"]) == 1
    assert value.intervals[1].start.instant.hour == 19
    assert value.intervals[1].end.instant.minute == 45


def test_work_travel_offsets_do_not_infer_a_different_reporting_zone():
    value = normalize_work_time(
        start="2026-11-05T23:00:00-08:00",
        end="2026-11-06T05:00:00-05:00",
        timezone=ZONE,
    )
    assert value.duration_us == 3 * 3600_000_000
    assert value.start.instant.day == value.end.instant.day == 6
    assert value.start.offset_seconds == -8 * 3600 and value.end.offset_seconds == -5 * 3600


def test_duration_only_and_incomplete_reports_never_gain_fabricated_clock_bounds():
    duration = normalize_work_time(
        start=None,
        end=None,
        timezone=ZONE,
        duration_minutes=Decimal("150.000001"),
    )
    assert duration.precision == "duration" and duration.duration_us == 9_000_000_060
    assert duration.start is None and duration.end is None and not duration.intervals
    unknown = normalize_work_time(start=None, end=None, timezone=ZONE)
    assert unknown.precision == "incomplete" and unknown.duration_us is None
    started = normalize_work_time(start="2026-11-05T10:00:00-08:00", end=None, timezone=ZONE)
    assert started.precision == "incomplete" and started.duration_us is None
    assert started.start is not None and started.end is None


@pytest.mark.parametrize(
    "start,code",
    [
        ("2026-11-01T01:30:00", "ambiguous_or_nonexistent_time"),
        ("2026-03-08T02:30:00", "ambiguous_or_nonexistent_time"),
        ("2026-11-05", "invalid_work_timestamp"),
    ],
)
def test_work_local_clock_errors_name_the_public_field(start, code):
    with pytest.raises(DocketError) as raised:
        normalize_work_time(start=start, end=None, timezone=ZONE)
    assert raised.value.code == code and raised.value.details["field"] == "start"


def test_work_invalid_duration_and_breaks_reject_only_the_affected_report():
    for minutes in (Decimal("0"), Decimal("-1"), Decimal("sNaN"), Decimal("0.000000001")):
        with pytest.raises(DocketError) as raised:
            normalize_work_time(start=None, end=None, timezone=ZONE, duration_minutes=minutes)
        assert raised.value.details["field"] == "duration_minutes"
    with pytest.raises(DocketError) as raised:
        normalize_work_time(
            start="2026-11-05T10:00:00-08:00",
            end="2026-11-05T12:00:00-08:00",
            timezone=ZONE,
            breaks=[("2026-11-05T09:00:00-08:00", "2026-11-05T09:15:00-08:00")],
        )
    assert raised.value.code == "invalid_work_break"
    assert raised.value.details["field"] == "breaks.0"
