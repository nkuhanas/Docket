"""Deterministic performed-time normalization, independent of Calendar wall clocks."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from docket.domain.errors import DocketError


@dataclass(frozen=True)
class WorkInstant:
    instant: datetime
    offset_seconds: int


@dataclass(frozen=True)
class WorkInterval:
    start: WorkInstant
    end: WorkInstant
    kind: Literal["active", "break"] = "active"


@dataclass(frozen=True)
class WorkTime:
    start: WorkInstant | None
    end: WorkInstant | None
    precision: Literal["interval", "duration", "incomplete"]
    duration_us: int | None
    intervals: tuple[WorkInterval, ...]


def elapsed_us(value: timedelta) -> int:
    return (value.days * 86400 + value.seconds) * 1_000_000 + value.microseconds


def work_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DocketError(
            code="invalid_timezone",
            message="Use an IANA reporting timezone.",
            details={"field": "timezone"},
        ) from exc


def work_instant(value: str, timezone: str, field: str) -> WorkInstant:
    zone = work_zone(timezone)
    try:
        if "T" not in value and " " not in value:
            raise ValueError("A date alone does not establish a clock bound")
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DocketError(
            code="invalid_work_timestamp",
            message="Use an ISO timestamp, or report work_date only.",
            details={"field": field},
        ) from exc
    if parsed.tzinfo is None:
        candidates = [parsed.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]
        valid = [
            candidate
            for candidate in candidates
            if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == parsed
        ]
        if not valid or len({candidate.utcoffset() for candidate in valid}) > 1:
            raise DocketError(
                code="ambiguous_or_nonexistent_time",
                message="Supply a real local timestamp with its UTC offset.",
                details={"field": field},
            )
        parsed = valid[0]
    offset = parsed.utcoffset()
    assert offset is not None
    if offset.microseconds:
        raise DocketError(
            code="invalid_work_offset",
            message="Use a whole-second UTC offset.",
            details={"field": field},
        )
    # Explicit offsets may describe travel between zones; reporting zone is separate.
    return WorkInstant(parsed.astimezone(UTC), elapsed_us(offset) // 1_000_000)


def normalize_work_time(
    *,
    start: str | None,
    end: str | None,
    timezone: str,
    duration_minutes: Decimal | None = None,
    breaks: list[tuple[str, str]] | None = None,
) -> WorkTime:
    work_zone(timezone)
    first = work_instant(start, timezone, "start") if start is not None else None
    last = work_instant(end, timezone, "end") if end is not None else None
    if first and last and last.instant <= first.instant:
        raise DocketError(
            code="invalid_work_end",
            message="end must be after start in elapsed time.",
            details={"field": "end"},
        )
    if duration_minutes is not None:
        if first or last or breaks:
            raise DocketError(
                code="conflicting_work_time",
                message="Use intervals or a reported duration.",
                details={"field": "duration_minutes"},
            )
        if not duration_minutes.is_finite():
            raise DocketError(
                code="invalid_work_duration",
                message="Use finite positive minutes.",
                details={"field": "duration_minutes"},
            )
        quantity = duration_minutes * 60_000_000
        if not quantity.is_finite() or quantity <= 0 or quantity != quantity.to_integral_value():
            raise DocketError(
                code="invalid_work_duration",
                message="Use positive minutes at microsecond precision.",
                details={"field": "duration_minutes"},
            )
        if quantity > 2**63 - 1:
            raise DocketError(
                code="invalid_work_duration",
                message="Reported duration exceeds supported precision.",
                details={"field": "duration_minutes"},
            )
        return WorkTime(None, None, "duration", int(quantity), ())

    reported_breaks: list[WorkInterval] = []
    for index, (pause_start, pause_end) in enumerate(breaks or []):
        begin = work_instant(pause_start, timezone, f"breaks.{index}.start")
        finish = work_instant(pause_end, timezone, f"breaks.{index}.end")
        if (
            finish.instant <= begin.instant
            or (first and begin.instant < first.instant)
            or (last and finish.instant > last.instant)
        ):
            raise DocketError(
                code="invalid_work_break",
                message="Breaks must be positive and inside known bounds.",
                details={"field": f"breaks.{index}"},
            )
        reported_breaks.append(WorkInterval(begin, finish, "break"))
    merged: list[WorkInterval] = []
    for pause in sorted(reported_breaks, key=lambda interval: interval.start.instant):
        if merged and pause.start.instant <= merged[-1].end.instant:
            previous = merged[-1]
            if pause.end.instant > previous.end.instant:
                merged[-1] = WorkInterval(previous.start, pause.end, "break")
        else:
            merged.append(pause)
    if first is None or last is None:
        return WorkTime(first, last, "incomplete", None, tuple(merged))
    active: list[WorkInterval] = []
    cursor = first
    for pause in merged:
        if cursor.instant < pause.start.instant:
            active.append(WorkInterval(cursor, pause.start))
        cursor = pause.end
    if cursor.instant < last.instant:
        active.append(WorkInterval(cursor, last))
    quantity_us = sum(
        elapsed_us(interval.end.instant - interval.start.instant) for interval in active
    )
    intervals = tuple(sorted([*active, *merged], key=lambda interval: interval.start.instant))
    return WorkTime(first, last, "interval", quantity_us, intervals)
