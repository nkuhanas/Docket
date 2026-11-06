"""Exact pricing over retained raw work coordinates, before document rounding."""

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any

from docket.domain.compensation import HOUR

NANO = 1_000_000_000


def expense_units(amount: str) -> int:
    value = Fraction(Decimal(amount)) * NANO
    if value < 0 or value.denominator != 1:
        raise ValueError("Expense money must be nonnegative with at most nine decimal places")
    return int(value)


def rational(value: Fraction | None) -> dict[str, str] | None:
    if value is None:
        return None
    return {"numerator": str(value.numerator), "denominator": str(value.denominator)}


@dataclass(frozen=True)
class WorkPriceBasis:
    raw_us: int
    reportable_us: int | None
    overtime_us: int | None
    rate: str | None
    overtime_multiplier: str | None

    def __post_init__(self) -> None:
        if self.raw_us <= 0 or (self.reportable_us is not None and self.reportable_us < 0):
            raise ValueError("Work pricing requires positive raw and nonnegative reportable time")
        if self.overtime_us is not None and (
            self.overtime_us < 0
            or (self.reportable_us is not None and self.overtime_us > self.reportable_us)
        ):
            raise ValueError("Overtime must be within reportable time")
        if any(
            value is not None and Fraction(Decimal(value)) < 0
            for value in (self.rate, self.overtime_multiplier)
        ):
            raise ValueError("Work pricing cannot use negative rates or multipliers")

    def price(self, start: int, end: int) -> Fraction | None:
        if not 0 <= start <= end <= self.raw_us:
            raise ValueError("Price slice must lie inside its retained work contribution")
        if self.reportable_us is None or self.overtime_us is None or self.rate is None:
            return None
        if self.overtime_us and self.overtime_multiplier is None:
            return None
        rate = Fraction(Decimal(self.rate))
        reportable = self.reportable_us
        multiplier = Fraction(Decimal(self.overtime_multiplier or "1"))
        regular_end = Fraction(reportable - self.overtime_us)

        def cumulative(raw: int) -> Fraction:
            reported = Fraction(reportable * raw, self.raw_us)
            regular = min(reported, regular_end)
            overtime = max(Fraction(0), reported - regular_end)
            return rate * (regular + overtime * multiplier) / HOUR

        return cumulative(end) - cumulative(start)


def price_quote(quote: dict[str, Any], start: int, end: int) -> Fraction | None:
    """Allocation coordinates are stable source units; quotes pin their price curve."""
    if start < 0 or end <= start:
        raise ValueError("Allocation requires a positive source range")
    if quote["kind"] == "expense":
        return Fraction(end - start, NANO)
    basis = WorkPriceBasis(**quote["basis"])
    offset = int(quote["line_offset_us"]) - int(quote["source_start"])
    return basis.price(start + offset, end + offset)


def remaining_ranges(
    start: int, end: int, occupied: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Union occupied source ranges once, without moving existing claimed quantities."""
    if start < 0 or end < start:
        raise ValueError("Invalid source bounds")
    result = []
    point = start
    for lo, hi in sorted(occupied):
        lo, hi = max(lo, start), min(hi, end)
        if hi <= point or hi <= lo:
            continue
        if lo > point:
            result.append((point, lo))
        point = max(point, hi)
    if point < end:
        result.append((point, end))
    return result
