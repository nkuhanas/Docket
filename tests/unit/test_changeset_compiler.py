from __future__ import annotations

import pytest

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
            "item": {
                "title": "Lecture 11.3 — p-series and applications",
                "kind": "academic.lecture_topic",
            },
            "temporal": {
                "role": "window",
                "temporal_value": {
                    "kind": "datetime_interval",
                    "start_local": "2026-09-01T15:00:00",
                    "end_local": "2026-09-01T15:50:00",
                    "timezone": "America/Los_Angeles",
                },
            },
            "calendar": {
                "kind": "canonical_event",
                "lane_ref": new_public_ref("lane"),
                "event_spec": {
                    "title": "MATH 1263 — Lecture 11.3: p-series and applications",
                    "calendar_lane": "math-1263",
                    "timing": {
                        "kind": "timed",
                        "start_local": "2026-09-01T15:00:00",
                        "end_local": "2026-09-01T15:50:00",
                        "timezone": "America/Los_Angeles",
                    },
                },
            },
        }
    )

    compiled = compile_normalized_entry(
        entry,
        utterance_ref=new_public_ref("utt"),
        statement_ref=new_public_ref("stm"),
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
    assert event["create_spec"]["title"] == (
        "Lecture 11.3 — p-series and applications"
    )
    assert event["create_spec"]["event_spec"]["title"].startswith("MATH 1263")
    route = compiled.actions[-1]
    assert route["mutation_type"] == "lane_routing_decision_create"
    assert route["create_spec"]["event_change_id"] == event["change_id"]


def test_scheduled_occurrence_refuses_time_mismatch() -> None:
    source_ref = new_public_ref("src")
    entry = ScheduledOccurrenceEntry.model_validate(
        {
            "import_entry_id": "mismatch",
            "evidence": _evidence(source_ref),
            "item": {"title": "Quiz"},
            "temporal": {
                "role": "window",
                "temporal_value": {
                    "kind": "datetime_interval",
                    "start_local": "2026-09-01T15:00:00",
                    "end_local": "2026-09-01T15:50:00",
                    "timezone": "America/Los_Angeles",
                },
            },
            "calendar": {
                "kind": "canonical_event",
                "lane_ref": new_public_ref("lane"),
                "event_spec": {
                    "title": "Quiz",
                    "timing": {
                        "kind": "timed",
                        "start_local": "2026-09-01T16:00:00",
                        "end_local": "2026-09-01T16:50:00",
                        "timezone": "America/Los_Angeles",
                    },
                },
            },
        }
    )

    with pytest.raises(DocketError) as exc_info:
        compile_normalized_entry(
            entry,
            utterance_ref=new_public_ref("utt"),
            statement_ref=new_public_ref("stm"),
        )
    assert exc_info.value.code == "normalized_occurrence_time_mismatch"


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
