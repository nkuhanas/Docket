"""Deterministic work evaluation; no inference, canonical writes or provider effects.

All quantities are integer microseconds. Money remains rational until explicit
currency rounding. Reported classification never becomes verified eligibility.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from fractions import Fraction
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

from docket.domain.work_time import elapsed_us
from docket.schemas.compensation import PolicyTermSpec

HOUR = 3_600_000_000
DAY = timedelta(days=1)


@dataclass(frozen=True)
class PolicyWindow:
    ref: str
    revision_ref: str
    terms: PolicyTermSpec


@dataclass(frozen=True)
class WorkQuantity:
    """One immutable active segment or report, with offsets in its root work basis."""

    work_ref: str
    revision_ref: str
    source_revision_ref: str
    engagement_ref: str | None
    segment_key: str
    offset_us: int
    quantity_us: int | None
    timezone: str = "UTC"
    start: datetime | None = None
    end: datetime | None = None
    day: date | None = None
    period_start: date | None = None
    period_end: date | None = None
    reported_class: str | None = None
    open_timer: bool = False
    quantity_needs_review: bool = False
    engagement_from: date | None = None
    engagement_to: date | None = None


@dataclass
class Contribution:
    source: WorkQuantity
    start: datetime | None
    end: datetime | None
    offset_us: int
    raw_us: int | None
    day: date | None
    pay: PolicyWindow | None
    rules: PolicyWindow | None
    included: bool
    reportable_us: int | None = None
    overtime_us: int | None = None
    gross: Fraction | None = None
    flags: set[str] = field(default_factory=set)

    @property
    def eligible(self) -> bool | None:
        if "outside_engagement_dates" in self.flags:
            return None
        if not self.pay:
            return None
        p = self.pay.terms
        if p.basis == "unpaid" or (p.compensable is False and p.billable is False):
            return False
        if p.compensable is True or p.billable is True:
            return True
        return None


def boundary(day: date, zone: str) -> datetime:
    return datetime.combine(day, time(), ZoneInfo(zone)).astimezone(UTC)


def applicable(windows: list[PolicyWindow], instant: datetime) -> PolicyWindow | None:
    return next(
        (
            w
            for w in windows
            if w.terms.start_at <= instant and (w.terms.end_at is None or instant < w.terms.end_at)
        ),
        None,
    )


def stable_window(
    windows: list[PolicyWindow], start: datetime, end: datetime
) -> PolicyWindow | None:
    window = applicable(windows, start)
    if window and (window.terms.end_at is None or window.terms.end_at >= end):
        return window
    return None


def week(day: date, starts: int = 0) -> date:
    return day - timedelta(days=(day.weekday() - starts) % 7)


def period(day: date, rules: PolicyTermSpec | None, first: date, last: date) -> tuple[date, date]:
    if rules and rules.period_days and rules.anchor_date:
        offset = (day - rules.anchor_date).days // rules.period_days
        start = rules.anchor_date + timedelta(days=offset * rules.period_days)
        return start, start + timedelta(days=rules.period_days)
    if first <= day < last:
        return first, last
    start = week(day, rules.week_starts_on or 0 if rules else 0)
    return start, start + timedelta(days=7)


def rounded_integer(value: Fraction, mode: str = "nearest") -> int:
    floor = value.numerator // value.denominator
    if mode == "down":
        return floor
    if mode == "up":
        return floor + (value.denominator != 1)
    return floor + (value - floor >= Fraction(1, 2))


def money(value: Fraction, decimals: int) -> str:
    """Round half up once at the declared monetary boundary, without a float."""
    units = rounded_integer(abs(value) * 10**decimals)
    sign = "-" if value < 0 and units else ""
    digits = str(units).zfill(decimals + 1)
    return sign + (digits[:-decimals] + "." + digits[-decimals:] if decimals else digits)


def allocate(total: int, weights: list[int]) -> list[int]:
    """Exact largest-remainder allocation; ties retain immutable source order."""
    denominator = sum(weights)
    if not denominator:
        return [0] * len(weights)
    shares = [Fraction(total * weight, denominator) for weight in weights]
    result = [share.numerator // share.denominator for share in shares]
    order = sorted(range(len(weights)), key=lambda i: (-(shares[i] - result[i]), i))
    for index in order[: total - sum(result)]:
        result[index] += 1
    return result


class CompensationCalculator:
    def __init__(
        self,
        pay: dict[str, list[PolicyWindow]],
        reporting: dict[str, list[PolicyWindow]],
        first: date,
        last: date,
        timezone: str,
    ) -> None:
        self.pay, self.reporting = pay, reporting
        self.first, self.last, self.timezone = first, last, timezone
        self.start, self.end = boundary(first, timezone), boundary(last, timezone)

    def pieces(self, source: WorkQuantity) -> list[Contribution]:
        pay = self.pay.get(source.engagement_ref or "", [])
        rules = self.reporting.get(source.engagement_ref or "", [])
        if source.open_timer:
            return [
                Contribution(
                    source,
                    source.start,
                    source.end,
                    source.offset_us,
                    None,
                    source.day,
                    None,
                    None,
                    True,
                    flags={"open_timer"},
                )
            ]
        if source.start is not None and source.end is not None:
            boundaries = {source.start, source.end, self.start, self.end}
            for window in (*pay, *rules):
                boundaries.add(window.terms.start_at)
                if window.terms.end_at:
                    boundaries.add(window.terms.end_at)
            # Each policy's local day is independent of physical work location.
            zones = {self.timezone, *(w.terms.timezone for w in rules)}
            for zone in zones:
                local = source.start.astimezone(ZoneInfo(zone)).date()
                local_last = source.end.astimezone(ZoneInfo(zone)).date()
                while local <= local_last:
                    boundaries.add(boundary(local, zone))
                    local += DAY
            ordered = sorted(b for b in boundaries if source.start <= b <= source.end)
            result = []
            for start, end in pairwise(ordered):
                rule = applicable(rules, start)
                zone = rule.terms.timezone if rule else self.timezone
                result.append(
                    Contribution(
                        source,
                        start,
                        end,
                        source.offset_us + elapsed_us(start - source.start),
                        elapsed_us(end - start),
                        start.astimezone(ZoneInfo(zone)).date(),
                        applicable(pay, start),
                        rule,
                        self.start <= start < self.end,
                    )
                )
            return result
        first = source.day or source.period_start
        last = source.day + DAY if source.day else source.period_end
        if first is None or last is None:
            return [
                Contribution(
                    source,
                    None,
                    None,
                    source.offset_us,
                    source.quantity_us,
                    None,
                    None,
                    None,
                    True,
                    flags={"unplaced_time"},
                )
            ]
        start, end = boundary(first, source.timezone), boundary(last, source.timezone)
        p, r = stable_window(pay, start, end), stable_window(rules, start, end)
        included = start < self.end and end > self.start
        contribution = Contribution(
            source, None, None, source.offset_us, source.quantity_us, source.day, p, r, included
        )
        if start < self.start or end > self.end:
            contribution.flags.add("cross_period_quantity_unallocated")
        if (not p and any(start < w.terms.start_at < end for w in pay)) or (
            not r and any(start < w.terms.start_at < end for w in rules)
        ):
            contribution.flags.add("within_report_policy_change")
        if r and r.terms.timezone != source.timezone:
            # A reported civil day cannot silently acquire a different timezone.
            contribution.flags.add("reporting_day_timezone_unresolved")
        return [contribution]

    @staticmethod
    def grouping(piece: Contribution) -> tuple[str, bool]:
        rules = piece.rules.terms if piece.rules else None
        if rules and rules.group_with_payer:
            payer = piece.pay.terms.payer_ref if piece.pay else None
            if payer:
                return payer, True
            piece.flags.add("group_payer_unknown")
        return piece.source.engagement_ref or "unknown", False

    def round_quantities(self, pieces: list[Contribution]) -> None:
        groups: dict[tuple[Any, ...], list[Contribution]] = defaultdict(list)
        for piece in pieces:
            piece.reportable_us = piece.raw_us
            if piece.raw_us is None:
                piece.flags.add("quantity_unknown")
                continue
            r = piece.rules.terms if piece.rules else None
            if not r or r.rounding_minutes is None:
                continue
            group = self.grouping(piece)
            scope = r.rounding_scope
            if r.group_with_payer and not group[1] and scope in {"day", "period"}:
                piece.flags.add("rounding_group_payer_unknown")
                piece.reportable_us = None
                continue
            if scope in {"day", "period"} and piece.day is None:
                if scope == "period" and piece.source.period_start and piece.source.period_end:
                    lo, hi = period(piece.source.period_start, r, self.first, self.last)
                    if piece.source.period_start >= lo and piece.source.period_end <= hi:
                        key: Any = (lo, hi)
                    else:
                        piece.flags.add("rounding_placement_unknown")
                        piece.reportable_us = None
                        continue
                else:
                    piece.flags.add("rounding_placement_unknown")
                    piece.reportable_us = None
                    continue
            else:
                key = (
                    piece.source.segment_key
                    if scope == "segment"
                    else piece.source.revision_ref
                    if scope == "session"
                    else piece.day
                    if scope == "day"
                    else period(piece.day, r, self.first, self.last)
                    if piece.day
                    else None
                )
            if scope == "segment" and piece.start is None:
                piece.flags.add("rounding_segments_unknown")
                piece.reportable_us = None
                continue
            # A pay-rate boundary does not create another rounding group.
            signature = (
                str(r.rounding_minutes),
                r.rounding_mode,
                r.rounding_scope,
                r.timezone,
                r.anchor_date,
                r.period_days,
            )
            groups[(group, signature, key)].append(piece)
        for (_group, signature, _key), members in groups.items():
            quantum = Fraction(signature[0]) * 60_000_000
            total = sum(p.raw_us or 0 for p in members)
            rounded = rounded_integer(Fraction(total) / quantum, str(signature[1])) * quantum
            if rounded.denominator != 1:
                for piece in members:
                    piece.flags.add("rounding_precision_unrepresentable")
                    piece.reportable_us = None
                continue
            allocated = allocate(int(rounded), [p.raw_us or 0 for p in members])
            for piece, quantity in zip(members, allocated, strict=True):
                piece.reportable_us = quantity

    def overtime(self, pieces: list[Contribution]) -> None:
        groups: dict[tuple[Any, ...], list[Contribution]] = defaultdict(list)
        for piece in pieces:
            piece.overtime_us = 0 if piece.reportable_us is not None else None
            r = piece.rules.terms if piece.rules else None
            if not r or (r.overtime_after_hours is None and r.hour_limit_week is None):
                continue
            if piece.day is None:
                piece.flags.add("hour_rule_placement_unknown")
                piece.overtime_us = None
                continue
            group = self.grouping(piece)
            # Shared employer policies must agree; disagreement is not a new budget.
            key = (group, r.timezone, week(piece.day, r.week_starts_on or 0))
            groups[key].append(piece)
        for members in groups.values():
            limits = {p.rules.terms.hour_limit_week for p in members if p.rules}
            total = sum(p.raw_us or 0 for p in members)
            if len(limits) > 1:
                for p in members:
                    p.flags.add("hour_limit_policy_conflict")
            elif next(iter(limits)) is not None:
                limit = next(iter(limits))
                assert limit is not None
                if total > Fraction(limit) * HOUR:
                    for p in members:
                        p.flags.add("configured_weekly_limit_exceeded")
            ot_groups: dict[date, list[Contribution]] = defaultdict(list)
            signatures = {
                (
                    p.rules.terms.overtime_after_hours,
                    p.rules.terms.overtime_multiplier,
                    p.rules.terms.overtime_scope,
                )
                for p in members
                if p.rules
            }
            if len(signatures) != 1:
                for p in members:
                    p.flags.add("overtime_policy_conflict")
                    p.overtime_us = None
                continue
            threshold, _multiplier, scope = next(iter(signatures))
            if threshold is None:
                continue
            for p in members:
                if p.eligible is False:
                    continue
                assert p.day is not None
                bucket = (
                    p.day
                    if scope == "day"
                    else week(p.day, p.rules.terms.week_starts_on or 0 if p.rules else 0)
                )
                ot_groups[bucket].append(p)
            for eligible in ot_groups.values():
                self._overtime_bucket(eligible, Fraction(threshold) * HOUR)

        for piece in pieces:
            rules = piece.rules.terms if piece.rules else None
            if (
                rules
                and rules.group_with_payer
                and rules.overtime_after_hours is not None
                and (piece.pay is None or piece.pay.terms.payer_ref is None)
            ):
                piece.overtime_us = None
                piece.flags.add("overtime_group_payer_unknown")

    @staticmethod
    def _overtime_bucket(members: list[Contribution], threshold: Fraction) -> None:
        if any(p.reportable_us is None or p.eligible is None for p in members):
            for p in members:
                p.overtime_us = None
                p.flags.add("overtime_quantity_unknown")
            return
        total = sum(p.reportable_us or 0 for p in members)
        if total <= threshold:
            return
        # Dates establish an order without inventing within-day clock bounds.
        days: dict[date, list[Contribution]] = defaultdict(list)
        for p in members:
            assert p.day is not None
            days[p.day].append(p)
        consumed = 0
        for _day, daily in sorted(days.items()):
            daily_total = sum(p.reportable_us or 0 for p in daily)
            remaining = max(Fraction(0), threshold - consumed)
            if daily_total <= remaining:
                consumed += daily_total
                continue
            if remaining == 0:
                for p in daily:
                    p.overtime_us = p.reportable_us
            elif any(p.start is None for p in daily):
                rates = {(p.pay.terms.rate, p.pay.terms.currency) for p in daily if p.pay}
                if len(rates) > 1:
                    for p in daily:
                        p.overtime_us = None
                        p.flags.add("overtime_allocation_unknown")
                else:
                    extra = Fraction(daily_total) - remaining
                    assert extra.denominator == 1
                    allocation = allocate(int(extra), [p.reportable_us or 0 for p in daily])
                    for p, amount in zip(daily, allocation, strict=True):
                        p.overtime_us = amount
            else:
                offset = 0
                for p in sorted(daily, key=lambda p: (p.start, p.source.revision_ref, p.offset_us)):
                    quantity = p.reportable_us or 0
                    extra = max(Fraction(0), min(Fraction(quantity), offset + quantity - remaining))
                    assert extra.denominator == 1
                    p.overtime_us = int(extra)
                    offset += quantity
            consumed += daily_total

    @staticmethod
    def overlaps(pieces: list[Contribution]) -> None:
        active: list[Contribution] = []
        for piece in sorted(
            (p for p in pieces if p.start and p.end),
            key=lambda p: p.start or datetime.min.replace(tzinfo=UTC),
        ):
            assert piece.start is not None
            active = [p for p in active if p.end and p.end > piece.start]
            for other in active:
                if other.source.revision_ref != piece.source.revision_ref:
                    other.flags.add("overlapping_work")
                    piece.flags.add("overlapping_work")
            active.append(piece)

    def evaluate(self, sources: list[WorkQuantity]) -> list[Contribution]:
        pieces = [piece for source in sources for piece in self.pieces(source)]
        for piece in pieces:
            if piece.day and (
                (piece.source.engagement_from and piece.day < piece.source.engagement_from)
                or (piece.source.engagement_to and piece.day > piece.source.engagement_to)
            ):
                piece.flags.add("outside_engagement_dates")
        self.round_quantities(pieces)
        self.overtime(pieces)
        self.overlaps(pieces)
        for p in pieces:
            if p.source.quantity_needs_review:
                p.flags.add("quantity_lineage_needs_review")
            if p.raw_us is None:
                p.flags.add("quantity_unknown")
            if p.source.engagement_ref is None:
                p.flags.add("engagement_unknown")
            if p.pay is None:
                p.flags.add("pay_policy_unknown")
                continue
            policy = p.pay.terms
            if p.eligible is False:
                p.gross = Fraction(0)
                continue
            if p.eligible is None:
                p.flags.add("eligibility_unknown")
                continue
            if policy.basis in {"salary", "fixed_fee"}:
                p.flags.add("nonhourly_entitlement_not_established")
                continue
            if policy.basis != "hourly" or policy.rate is None:
                p.flags.add("rate_unknown")
                continue
            if policy.currency is None:
                p.flags.add("currency_unknown")
                continue
            blocking = {
                "unplaced_time",
                "cross_period_quantity_unallocated",
                "within_report_policy_change",
                "reporting_day_timezone_unresolved",
                "open_timer",
            }
            if p.flags & blocking or p.reportable_us is None or p.overtime_us is None:
                continue
            multiplier = (
                Fraction(p.rules.terms.overtime_multiplier)
                if p.rules and p.rules.terms.overtime_multiplier is not None
                else Fraction(1)
            )
            payable = p.reportable_us + p.overtime_us * (multiplier - 1)
            p.gross = payable * Fraction(policy.rate) / HOUR
        return pieces


def summarize(pieces: list[Contribution]) -> dict[str, Any]:
    included = [p for p in pieces if p.included]
    totals: dict[tuple[str, int], Fraction] = defaultdict(Fraction)
    for piece in included:
        if piece.gross is not None and piece.pay and piece.pay.terms.currency:
            key = (piece.pay.terms.currency, piece.pay.terms.money_decimals or 0)
            totals[key] += piece.gross
    return {
        "worked_us": sum(p.raw_us or 0 for p in included),
        "reportable_us": sum(p.reportable_us or 0 for p in included),
        "compensable_us": sum(
            p.reportable_us or 0
            for p in included
            if p.pay and p.pay.terms.compensable is True and p.eligible is True
        ),
        "billable_us": sum(
            p.reportable_us or 0
            for p in included
            if p.pay and p.pay.terms.billable is True and p.eligible is True
        ),
        "overtime_us": sum(p.overtime_us or 0 for p in included),
        "unknown_quantity_count": sum(p.raw_us is None for p in included),
        "unresolved_gross_count": sum(p.gross is None for p in included),
        "gross_estimates": [
            {"currency": currency, "amount": money(amount, precision), "money_decimals": precision}
            for (currency, precision), amount in sorted(totals.items())
        ],
        "flags": sorted({flag for p in included for flag in p.flags}),
        "coverage": "reported work only; missing work is not evidence of inactivity",
        "financial_state": "estimate; no claim, submission, invoice or payment",
    }
