"""Exact quantities across policy boundaries; unknown reports remain unknown."""

from datetime import UTC, date, datetime
from decimal import Decimal
from fractions import Fraction

from docket.domain.compensation import (
    HOUR,
    CompensationCalculator,
    PolicyWindow,
    WorkQuantity,
    money,
    summarize,
)
from docket.schemas.compensation import PolicyTermSpec


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


def pay(ref="p1", start="2026-01-01T00:00:00+00:00", end=None, **values):
    return PolicyWindow(
        ref,
        "rev-" + ref,
        PolicyTermSpec(
            kind="pay",
            start_at=instant(start),
            end_at=instant(end) if end else None,
            timezone="UTC",
            **{
                "basis": "hourly",
                "rate": "80",
                "currency": "USD",
                "money_decimals": 2,
                "compensable": True,
                **values,
            },
        ),
    )


def rules(**values):
    return PolicyWindow(
        "rules",
        "rules-revision",
        PolicyTermSpec(
            kind="reporting",
            start_at=datetime(2026, 1, 1, tzinfo=UTC),
            **{
                "timezone": "UTC",
                "week_starts_on": 0,
                "rounding_scope": "period",
                "rounding_mode": "nearest",
                "overtime_scope": "week",
                "group_with_payer": False,
                **values,
            },
        ),
    )


def work(ref="w1", eng="eng1", start=None, end=None, minutes=60, day=None, **values):
    return WorkQuantity(
        work_ref=ref,
        revision_ref="rev-" + ref,
        source_revision_ref="rev-" + ref,
        engagement_ref=eng,
        segment_key="segment-" + ref,
        offset_us=0,
        quantity_us=minutes * 60_000_000 if minutes is not None else None,
        start=instant(start) if start else None,
        end=instant(end) if end else None,
        day=date.fromisoformat(day) if day else None,
        **values,
    )


def calculate(
    sources, pays=None, reporting=None, first="2026-11-02", last="2026-11-09", timezone="UTC"
):
    return CompensationCalculator(
        pays if pays is not None else {"eng1": [pay()]},
        reporting or {},
        date.fromisoformat(first),
        date.fromisoformat(last),
        timezone,
    ).evaluate(sources)


def test_round_once_across_effective_rate_boundary_and_preserve_quantity_coordinates():
    source = work(start="2026-11-03T23:50:00+00:00", end="2026-11-04T00:10:00+00:00", minutes=20)
    policies = {
        "eng1": [
            pay(end="2026-11-04T00:00:00+00:00"),
            pay("p2", start="2026-11-04T00:00:00+00:00", rate="100"),
        ]
    }
    pieces = calculate(
        [source], policies, {"eng1": [rules(rounding_minutes=15, rounding_scope="session")]}
    )
    assert [p.raw_us for p in pieces] == [600_000_000, 600_000_000]
    assert [p.offset_us for p in pieces] == [0, 600_000_000]
    assert [p.reportable_us for p in pieces] == [450_000_000, 450_000_000]
    assert summarize(pieces)["gross_estimates"] == [
        {"currency": "USD", "amount": "22.50", "money_decimals": 2}
    ]
    assert source.quantity_us == 1_200_000_000


def test_day_rounding_sums_reports_before_rounding_and_period_does_not_include_other_days():
    sources = [
        work("a", minutes=8, day="2026-11-03"),
        work("b", minutes=8, day="2026-11-03"),
        work("before", minutes=8, day="2026-10-20"),
    ]
    pieces = calculate(
        sources, reporting={"eng1": [rules(rounding_minutes=15, rounding_scope="day")]}
    )
    assert summarize(pieces)["reportable_us"] == 15 * 60_000_000
    pieces = calculate(sources, reporting={"eng1": [rules(rounding_minutes=15)]})
    assert summarize(pieces)["reportable_us"] == 15 * 60_000_000


def test_dst_and_cross_midnight_use_elapsed_instants_and_keep_reported_timezone():
    source = work(
        start="2026-11-01T01:30:00-07:00",
        end="2026-11-01T01:30:00-08:00",
        minutes=60,
        timezone="America/Los_Angeles",
    )
    pieces = calculate(
        [source], first="2026-11-01", last="2026-11-02", timezone="America/Los_Angeles"
    )
    assert sum(p.raw_us for p in pieces) == HOUR
    assert summarize(pieces)["gross_estimates"][0]["amount"] == "80.00"
    assert all(p.start < p.end for p in pieces)


def test_unknown_terms_duration_and_reported_classification_do_not_become_zero_or_pay():
    source = work(minutes=180, day="2026-11-03", reported_class="billable")
    pieces = calculate([source], pays={})
    assert source.reported_class == "billable"
    assert pieces[0].eligible is None and pieces[0].gross is None
    assert summarize(pieces)["gross_estimates"] == []
    unplaced = calculate([work("unknown", minutes=60)])
    assert "unplaced_time" in unplaced[0].flags and unplaced[0].gross is None
    incomplete = calculate([work("incomplete", minutes=None, day="2026-11-03")])
    assert summarize(incomplete)["unknown_quantity_count"] == 1
    unpaid = calculate([source], {"eng1": [pay(basis="unpaid", rate=None)]})
    assert unpaid[0].gross == 0 and unpaid[0].eligible is False


def test_salary_and_future_rate_do_not_manufacture_hourly_earnings():
    sources = [work(minutes=120, day="2026-11-03")]
    pieces = calculate(sources, {"eng1": [pay(basis="salary", rate=None, amount="10000")]})
    assert pieces[0].gross is None
    assert "nonhourly_entitlement_not_established" in pieces[0].flags
    expired = calculate(sources, {"eng1": [pay(end="2026-11-02T00:00:00+00:00")]})
    assert expired[0].pay is None and expired[0].gross is None


def test_duration_only_crossing_period_or_rate_boundary_requires_allocation():
    source = work(minutes=120, period_start=date(2026, 11, 1), period_end=date(2026, 11, 5))
    pieces = calculate([source])
    assert "cross_period_quantity_unallocated" in pieces[0].flags
    assert pieces[0].gross is None and pieces[0].raw_us == 2 * HOUR
    changing = calculate(
        [work(day="2026-11-03")],
        {
            "eng1": [
                pay(end="2026-11-03T12:00:00+00:00"),
                pay("second", start="2026-11-03T12:00:00+00:00", rate="90"),
            ]
        },
    )
    assert "within_report_policy_change" in changing[0].flags
    assert changing[0].gross is None


def test_overtime_limits_and_shared_payer_use_explicit_policy_only():
    sources = [
        work("a", "eng1", minutes=1800, day="2026-11-03"),
        work("b", "eng2", minutes=900, day="2026-11-04"),
    ]
    payer = "ent_00000000000000000000000000"
    pays = {"eng1": [pay(payer_ref=payer)], "eng2": [pay(payer_ref=payer)]}
    rule = rules(
        overtime_after_hours=40,
        overtime_multiplier="1.5",
        hour_limit_week=40,
        group_with_payer=True,
    )
    pieces = calculate(sources, pays, {"eng1": [rule], "eng2": [rule]})
    summary = summarize(pieces)
    assert summary["overtime_us"] == 5 * HOUR
    assert summary["gross_estimates"][0]["amount"] == "3800.00"
    assert "configured_weekly_limit_exceeded" in summary["flags"]
    separate = calculate(sources, pays, {"eng1": [rules()], "eng2": [rules()]})
    assert summarize(separate)["overtime_us"] == 0
    assert summarize(separate)["gross_estimates"][0]["amount"] == "3600.00"


def test_reported_dates_order_overtime_without_fabricating_clock_times():
    sources = [work("a", minutes=1800, day="2026-11-03"), work("b", minutes=900, day="2026-11-04")]
    pays = {
        "eng1": [
            pay(end="2026-11-04T00:00:00+00:00"),
            pay("later", start="2026-11-04T00:00:00+00:00", rate="100"),
        ]
    }
    pieces = calculate(
        sources, pays, {"eng1": [rules(overtime_after_hours=40, overtime_multiplier="1.5")]}
    )
    assert summarize(pieces)["gross_estimates"][0]["amount"] == "4150.00"
    assert summarize(pieces)["overtime_us"] == 5 * HOUR


def test_explicit_employer_grouping_with_unknown_payer_remains_unresolved():
    source = work(minutes=60, day="2026-11-03")
    pieces = calculate(
        [source],
        reporting={
            "eng1": [
                rules(group_with_payer=True, overtime_after_hours=40, overtime_multiplier="1.5")
            ]
        },
    )
    assert pieces[0].gross is None and pieces[0].overtime_us is None
    assert summarize(pieces)["unresolved_gross_count"] == 1
    rounded = calculate(
        [source],
        reporting={
            "eng1": [rules(group_with_payer=True, rounding_minutes=15, rounding_scope="day")]
        },
    )
    assert rounded[0].gross is None and rounded[0].reportable_us is None


def test_unknown_within_day_overtime_order_and_separate_currencies():
    payer = "ent_00000000000000000000000000"
    sources = [
        work("a", minutes=1800, day="2026-11-03"),
        work("b", "eng2", minutes=900, day="2026-11-03"),
    ]
    rule = rules(overtime_after_hours=40, overtime_multiplier="1.5", group_with_payer=True)
    pieces = calculate(
        sources,
        {"eng1": [pay(payer_ref=payer)], "eng2": [pay(rate=100, payer_ref=payer)]},
        {"eng1": [rule], "eng2": [rule]},
    )
    assert all(p.gross is None for p in pieces)
    assert "overtime_allocation_unknown" in summarize(pieces)["flags"]
    foreign = calculate(
        [work("a", day="2026-11-03"), work("b", "eng2", day="2026-11-03")],
        {"eng1": [pay(rate="0.1")], "eng2": [pay(currency="CAD")]},
    )
    assert [r["currency"] for r in summarize(foreign)["gross_estimates"]] == ["CAD", "USD"]


def test_exact_large_money_and_unknown_timer_overlap_do_not_change_actual_time():
    assert money(Fraction(Decimal("123456789012345.123456789")), 9) == "123456789012345.123456789"
    assert money(Fraction(1, 200), 2) == "0.01"
    assert money(Fraction(-1, 200), 2) == "-0.01"
    sources = [
        work("a", start="2026-11-03T10:00:00+00:00", end="2026-11-03T11:00:00+00:00"),
        work("b", start="2026-11-03T10:30:00+00:00", end="2026-11-03T11:30:00+00:00"),
        work("timer", minutes=None, open_timer=True),
    ]
    pieces = calculate(sources)
    assert [p.raw_us for p in pieces] == [HOUR, HOUR, None]
    assert "overlapping_work" in pieces[0].flags and "overlapping_work" in pieces[1].flags
    assert pieces[2].gross is None and "open_timer" in pieces[2].flags
