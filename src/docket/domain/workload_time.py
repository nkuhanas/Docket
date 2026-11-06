"""Exact interval unions for potential capacity, distinct from performed work."""

from datetime import UTC, date, datetime, time, timedelta

from docket.domain.work_time import elapsed_us, work_instant

Interval = tuple[datetime, datetime]


def union(intervals: list[Interval]) -> list[Interval]:
    result: list[Interval] = []
    for first, last in sorted((a.astimezone(UTC), b.astimezone(UTC)) for a, b in intervals):
        if last <= first:
            continue
        if result and first <= result[-1][1]:
            result[-1] = (result[-1][0], max(last, result[-1][1]))
        else:
            result.append((first, last))
    return result


def clipped(intervals: list[Interval], bounds: Interval) -> list[Interval]:
    return union([(max(a, bounds[0]), min(b, bounds[1])) for a, b in intervals])


def subtract(available: list[Interval], unavailable: list[Interval]) -> list[Interval]:
    result = []
    occupied = union(unavailable)
    for first, last in union(available):
        point = first
        for lo, hi in occupied:
            if hi <= point:
                continue
            if lo >= last:
                break
            if lo > point:
                result.append((point, min(lo, last)))
            point = max(point, hi)
            if point >= last:
                break
        if point < last:
            result.append((point, last))
    return result


def quantity(intervals: list[Interval]) -> int:
    return sum(elapsed_us(last - first) for first, last in union(intervals))


def clock_window(day: date, first: time, last: time, timezone: str) -> Interval:
    end_day = day + timedelta(days=1) if last < first else day
    return (
        work_instant(datetime.combine(day, first).isoformat(), timezone, "available_start").instant,
        work_instant(
            datetime.combine(end_day, last).isoformat(), timezone, "available_end"
        ).instant,
    )
