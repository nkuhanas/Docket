"""Planning uses elapsed UTC unions, independent from accounting quantities."""

from datetime import UTC, date, datetime, time

import pytest

from docket.domain.errors import DocketError
from docket.domain.workload_time import clipped, clock_window, quantity, subtract

HOUR = 3_600_000_000


def test_overlapping_commitments_and_protected_windows_consume_time_once():
    def point(hour):
        return datetime(2027, 1, 4, hour, tzinfo=UTC)

    available = [(point(9), point(17))]
    busy = [(point(10), point(12)), (point(11), point(13))]
    protected = [(point(12), point(14))]
    assert quantity(busy) == 3 * HOUR
    assert quantity(subtract(available, busy + protected)) == 4 * HOUR
    assert clipped([(point(8), point(11))], (point(9), point(10))) == [(point(9), point(10))]


def test_overnight_protection_preserves_dst_elapsed_time_and_rejects_undefined_clock():
    assert (
        quantity([clock_window(date(2026, 10, 31), time(23), time(7), "America/Los_Angeles")])
        == 9 * HOUR
    )
    assert (
        quantity([clock_window(date(2027, 3, 13), time(23), time(7), "America/Los_Angeles")])
        == 7 * HOUR
    )
    with pytest.raises(DocketError):
        clock_window(date(2027, 3, 14), time(2, 30), time(5), "America/Los_Angeles")


def test_capacity_uses_the_policy_day_across_the_international_date_line():
    from types import SimpleNamespace

    from docket.config import get_settings
    from docket.schemas.workload import WorkloadPreferenceSpec
    from docket.services.compensation_policies import date_boundary
    from docket.services.workload_context import WorkloadContextService

    bounds = (
        date_boundary(date(2027, 1, 4), "Pacific/Kiritimati", "date_from"),
        date_boundary(date(2027, 1, 5), "Pacific/Kiritimati", "date_to"),
    )
    rules = WorkloadPreferenceSpec(
        timezone="Pacific/Pago_Pago", weekdays=[5], available_start=time(22), available_end=time(2)
    )
    service = WorkloadContextService(None, get_settings())
    result = service.capacity(
        bounds, rules, SimpleNamespace(rows=[]), datetime(2027, 1, 1, tzinfo=UTC)
    )
    assert result["potential_available_us"] == 3 * HOUR
