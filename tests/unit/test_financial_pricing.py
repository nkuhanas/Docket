"""Partial claims retain marginal overtime pricing and exact conserved quantities."""

from fractions import Fraction

import pytest

from docket.domain.compensation import HOUR, money
from docket.domain.financial_pricing import (
    WorkPriceBasis,
    distribute_money,
    expense_units,
    price_quote,
    remaining_ranges,
)


def test_partial_overtime_is_not_whole_session_average_price():
    basis = WorkPriceBasis(2 * HOUR, 2 * HOUR, HOUR, "80", "1.5")
    assert basis.price(0, HOUR) == 80
    assert basis.price(HOUR, 2 * HOUR) == 120
    assert (
        sum(basis.price(lo, hi) for lo, hi in [(0, HOUR // 3), (HOUR // 3, HOUR), (HOUR, 2 * HOUR)])
        == 200
    )
    assert basis.price(0, HOUR) != basis.price(0, 2 * HOUR) / 2


def test_rounding_is_pinned_and_money_rounds_over_document_total():
    basis = WorkPriceBasis(50 * 60_000_000, HOUR, 0, "80", None)
    assert basis.price(0, 25 * 60_000_000) == 40
    assert basis.price(25 * 60_000_000, 50 * 60_000_000) == 40
    thirds = WorkPriceBasis(3 * HOUR, 3 * HOUR, 0, "0.333333333", None)
    pieces = [thirds.price(i * HOUR, (i + 1) * HOUR) for i in range(3)]
    assert money(sum(pieces), 2) == "1.00"
    assert sum(Fraction(money(piece, 2)) for piece in pieces) == Fraction(99, 100)


def test_unknown_prices_are_distinct_from_zero_and_quote_coordinates_are_exact():
    assert WorkPriceBasis(HOUR, None, None, "80", None).price(0, HOUR) is None
    assert WorkPriceBasis(HOUR, HOUR, HOUR, "80", None).price(0, HOUR) is None
    assert WorkPriceBasis(HOUR, 0, 0, "80", None).price(0, HOUR) == 0
    quote = {
        "kind": "work",
        "source_start": 100,
        "line_offset_us": HOUR,
        "basis": {
            "raw_us": 2 * HOUR,
            "reportable_us": 2 * HOUR,
            "overtime_us": HOUR,
            "rate": "80",
            "overtime_multiplier": "1.5",
        },
    }
    assert price_quote(quote, 100, 100 + HOUR) == 120
    with pytest.raises(ValueError):
        price_quote(quote, 100, 101 + HOUR)


def test_expense_quantity_and_duplicate_ranges_never_use_float_or_double_count():
    assert expense_units("100000000000000.000000001") == 100000000000000000000001
    assert price_quote({"kind": "expense"}, 0, 3) == Fraction(3, 1_000_000_000)
    assert remaining_ranges(0, 120, [(0, 90), (10, 30), (50, 95)]) == [(95, 120)]
    assert remaining_ranges(0, 120, [(20, 30), (70, 80)]) == [(0, 20), (30, 70), (80, 120)]
    assert remaining_ranges(90, 120, [(0, 90)]) == [(90, 120)]
    with pytest.raises(ValueError):
        expense_units("0.0000000001")


def test_exact_largest_remainder_preserves_large_and_partial_currency_quantities():
    total = 999999999999999999999999
    weights = [Fraction(1, 3), Fraction(2, 3), Fraction(1, 7)]
    values = distribute_money(total, weights)
    assert sum(values) == total
    for actual, weight in zip(values, weights, strict=True):
        assert abs(Fraction(actual) - total * weight / sum(weights)) < 1
    assert distribute_money(2, [Fraction(1), Fraction(1), Fraction(1)]) == [1, 1, 0]
