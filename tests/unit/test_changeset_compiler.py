from __future__ import annotations

import pytest
from pydantic import ValidationError

from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.schemas.assembly import (
    ScheduledOccurrenceEntry,
    ScheduleExceptionEntry,
)
from docket.services.changeset_compiler import compile_normalized_entry


def _evidence(source_ref: str) -> dict[str, object]:
    return {
        "source_ref": source_ref,
        "source_fragment_locator": {"page": 1, "cell": "week-2-tu"},
        "source_fragment_hash": "a" * 64,
        "extractor_identifier": "docket.attachment-text",
        "extractor_version": "1",
    }


def test_scheduled_occurrence_compiles_exact_owned_action_set() -> None:
    source_ref = new_public_ref("src")
    entry = ScheduledOccurrenceEntry.model_validate(
        {
            "import_entry_id": "math-1263-week-2-tu",
            "evidence": _evidence(source_ref),
            "title": "MATH 1263 — Lecture 11.3: p-series and applications",
            "kind": "academic.lecture_topic",
            "location": "Math & Science 038-0148",
            "lane_ref": new_public_ref("lane"),
            "timing": {
                "kind": "timed",
                "start_local": "2026-09-01T15:00:00",
                "end_local": "2026-09-01T15:50:00",
                "timezone": "America/Los_Angeles",
            },
        }
    )

    compiled = compile_normalized_entry(
        entry,
        utterance_ref=new_public_ref("utt"),
        statement_ref=new_public_ref("stm"),
        calendar_lane="math-1263",
    )

    assert [action["change_id"] for action in compiled.actions] == [
        "math-1263-week-2-tu.item",
        "math-1263-week-2-tu.time",
        "math-1263-week-2-tu.event",
        "math-1263-week-2-tu.route",
    ]
    assert compiled.ownership["change_ids"] == [
        "math-1263-week-2-tu.item",
        "math-1263-week-2-tu.time",
        "math-1263-week-2-tu.event",
        "math-1263-week-2-tu.route",
    ]
    assert compiled.predicted_provider_operation_types == ("calendar_create_event",)
    event = compiled.actions[-2]
    item = compiled.actions[0]["create_spec"]
    temporal = compiled.actions[1]["create_spec"]["temporal_value"]
    event_spec = event["create_spec"]["event_spec"]
    assert item["title"] == event["create_spec"]["title"] == event_spec["title"] == entry.title
    assert event_spec["calendar_lane"] == "math-1263"
    assert event_spec["location"] == entry.location
    assert event_spec["timing"]["start_local"] == temporal["start_local"]
    assert event_spec["timing"]["end_local"] == temporal["end_local"]
    assert event_spec["timing"]["timezone"] == temporal["timezone"]
    assert compiled.stored_entry["input_schema_version"] == 2
    assert compiled.stored_entry["compiler_version"] == 2
    route = compiled.actions[-1]
    assert route["mutation_type"] == "lane_routing_decision_create"
    assert route["create_spec"]["event_change_id"] == event["change_id"]


def test_scheduled_occurrence_requires_resolved_lane_and_single_timing() -> None:
    source_ref = new_public_ref("src")
    entry = ScheduledOccurrenceEntry.model_validate(
        {
            "import_entry_id": "mismatch",
            "evidence": _evidence(source_ref),
            "title": "Quiz",
            "lane_ref": new_public_ref("lane"),
            "timing": {
                "kind": "all_day",
                "start_date": "2026-09-01",
                "end_date": "2026-09-02",
                "timezone": "America/Los_Angeles",
            },
        }
    )

    with pytest.raises(DocketError) as exc_info:
        compile_normalized_entry(
            entry,
            utterance_ref=new_public_ref("utt"),
            statement_ref=new_public_ref("stm"),
        )
    assert exc_info.value.code == "normalized_entry_lane_unresolved"
    compiled = compile_normalized_entry(
        entry,
        utterance_ref=new_public_ref("utt"),
        statement_ref=new_public_ref("stm"),
        calendar_lane="math-1263",
    )
    temporal = compiled.actions[1]["create_spec"]["temporal_value"]
    event_timing = compiled.actions[2]["create_spec"]["event_spec"]["timing"]
    assert temporal["end_inclusive"] is False
    assert temporal["start_date"] == event_timing["start_date"] == "2026-09-01"
    assert temporal["end_date"] == event_timing["end_date"] == "2026-09-02"
    # There is no second title, second time, recurrence, or provider lane input
    # that could disagree with the resolved semantic entry.
    for field in ("item", "temporal", "calendar", "recurrence", "calendar_lane"):
        with pytest.raises(ValidationError):
            ScheduledOccurrenceEntry.model_validate({**entry.model_dump(), field: {}})


def test_schedule_exception_never_compiles_an_event() -> None:
    entry = ScheduleExceptionEntry.model_validate(
        {
            "import_entry_id": "labor-day",
            "evidence": _evidence(new_public_ref("src")),
            "item": {"title": "Labor Day — No Class", "kind": "schedule.exception"},
            "temporal": {
                "role": "scheduled_on",
                "temporal_value": {
                    "kind": "date",
                    "date": "2026-09-07",
                    "timezone": "America/Los_Angeles",
                },
            },
            "exception_disposition": "no_occurrence",
            "calendar": {"kind": "none"},
        }
    )
    compiled = compile_normalized_entry(
        entry,
        utterance_ref=new_public_ref("utt"),
        statement_ref=new_public_ref("stm"),
    )
    assert [action["mutation_type"] for action in compiled.actions] == [
        "item_create",
        "temporal_binding_create",
    ]
    assert compiled.predicted_provider_operation_types == ()
