"""Bounded, deterministic presentation of calendar choices; never model labels."""

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo


def duration_minutes(spec: dict[str, Any]) -> int | None:
    timing = spec.get("timing") or {}
    if timing.get("kind") != "timed":
        return None
    zone = ZoneInfo(timing["timezone"])
    fold = timing.get("fold") or 0
    start = datetime.fromisoformat(timing["start_local"]).replace(tzinfo=zone, fold=fold)
    end = datetime.fromisoformat(timing["end_local"]).replace(tzinfo=zone, fold=fold)
    seconds = (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds()
    return int(seconds // 60) if seconds > 0 and seconds % 60 == 0 else None


def calendar_choice(content: dict[str, Any]) -> tuple[str, str] | None:
    """Group only equivalent simple occurrences and their exact routing support.

    Unsupported mixed effects use the ordinary renderer. Dates, actual start/end
    bounds, timezone, destination and locations remain visible in every choice.
    """
    if any(content.get(group) for group in (
        "registry_changes", "preference_changes", "tracked_context_changes", "resolution_changes",
    )):
        return None
    events = content.get("event_changes") or []
    routes = content.get("lane_changes") or []
    if not events or any(e.get("mutation_type") != "canonical_event_create" for e in events):
        return None
    creates = {e["change_id"]: e["create_spec"] for e in events}
    for route in routes:
        if route.get("mutation_type") != "lane_routing_decision_create":
            return None
        spec = route["create_spec"]
        event = creates.get(spec.get("event_change_id"))
        if event is None or not event.get("lane_ref") or spec.get("lane_ref") != event["lane_ref"]:
            return None
    specs = [e["event_spec"] for e in creates.values()]
    first = specs[0]
    minutes = duration_minutes(first)
    if minutes is None or any(
        e["title"] != first["title"] or not e.get("lane_ref") or e.get("lane_change_id")
        or e.get("entity_refs") or e.get("entity_change_ids") or e.get("item_refs")
        or e.get("item_change_ids") or e.get("realizes_temporal_binding_refs")
        or e.get("realizes_temporal_binding_change_ids") or e.get("operator_policy_text")
        for e in creates.values()
    ):
        return None
    if len({e["lane_ref"] for e in creates.values()}) != 1 or any(
        duration_minutes(s) != minutes or s["title"] != first["title"]
        or s["calendar_lane"] != first["calendar_lane"] or s.get("recurrence")
        or s.get("notes") or s.get("operator_tags") or s.get("priority", "normal") != "normal"
        or s["timing"]["timezone"] != first["timing"]["timezone"]
        for s in specs
    ):
        return None
    label = f"{minutes} minutes each" if len(events) > 1 else f"{minutes} minutes"
    lines = [
        f"{label} — create {len(events)} “{first['title']}” "
        f"{'events' if len(events) > 1 else 'event'} in {first['calendar_lane']}. "
        f"Timezone: {first['timing']['timezone']}.",
    ]
    for spec in specs:
        timing = spec["timing"]
        start = datetime.fromisoformat(timing["start_local"])
        end = datetime.fromisoformat(timing["end_local"])
        end_text = end.strftime("%H:%M") if start.date() == end.date() else end.isoformat(" ")
        fold = f" (DST fold {timing['fold']})" if timing.get("fold") is not None else ""
        location = f" · {spec['location']}" if spec.get("location") else ""
        lines.append(f"{start:%Y-%m-%d %H:%M}\u2013{end_text}{fold}{location}")
    return "\n".join(lines), label
