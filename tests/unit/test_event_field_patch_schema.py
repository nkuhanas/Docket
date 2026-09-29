import pytest
from pydantic import TypeAdapter, ValidationError

from docket.domain.public_refs import new_public_ref
from docket.mcp.instrumented import _validation_issues
from docket.schemas.authority import CanonicalChangeInput, CanonicalEventModify, mutation_input_json
from docket.schemas.events import EventFieldPatch


@pytest.mark.parametrize("value", [None, "", "Async", "東京 🏫", "https://example.test"])
def test_field_presence_and_literal_location(value):
    patch = EventFieldPatch(location=value)
    assert patch.model_fields_set == {"location"}
    assert mutation_input_json(patch, preserve_explicit_nulls=True) == {"location": value}


@pytest.mark.parametrize(
    ("payload", "path"),
    [
        ({}, ()),
        ({"title": None}, ("title",)),
        ({"timing": None}, ("timing",)),
        ({"location": "x" * 1001}, ("location",)),
        ({"notes": "x" * 4001}, ("notes",)),
        ({"calendar_lane": "unsorted"}, ("calendar_lane",)),
        ({"recurrence": None}, ("recurrence",)),
        ({"priority": "high"}, ("priority",)),
        ({"operator_tags": []}, ("operator_tags",)),
    ],
)
def test_field_patch_rejects_invalid_or_noncontent_fields(payload, path):
    with pytest.raises(ValidationError) as caught:
        EventFieldPatch.model_validate(payload)
    assert caught.value.errors(include_input=False)[0]["loc"] == path


def test_only_public_sparse_mutation_is_exposed_and_destination_error_is_actionable():
    action = {
        "mutation_type": "canonical_event_modify",
        "change_id": "room",
        "action": "update",
        "object_type": "canonical_event",
        "object_ref": new_public_ref("evt"),
        "basis_refs": [new_public_ref("utt")],
        "affected_fields": ["event_spec.location"],
        "payload": {"event_spec": {"location": "Async", "calendar_lane": "unsorted"}},
    }
    adapter = TypeAdapter(CanonicalChangeInput)
    with pytest.raises(ValidationError) as caught:
        adapter.validate_python(action)
    issue = _validation_issues(caught.value)[0]
    assert issue["path"][-2:] == ["event_spec", "calendar_lane"]
    assert issue["next_action"] == "omit_calendar_lane_to_preserve_destination"
    action["payload"] = {"title": "Old duplicate spelling"}
    with pytest.raises(ValidationError):
        adapter.validate_python(action)
    action["payload"] = {}
    with pytest.raises(ValidationError):
        adapter.validate_python(action)
    action["mutation_type"] = "canonical_event_apply"
    with pytest.raises(ValidationError):
        adapter.validate_python(action)
    schema = adapter.json_schema()
    assert "MaterializedEventModify" not in schema["$defs"]


def test_recurrence_remains_a_separate_explicit_typed_capability():
    action = CanonicalEventModify(
        change_id="exceptions",
        action="update",
        object_type="canonical_event",
        object_ref=new_public_ref("evt"),
        scope={"kind": "entire_series"},
        payload={"recurrence": None},
        affected_fields=["event_spec.recurrence"],
        basis_refs=[new_public_ref("utt")],
    )
    assert mutation_input_json(action)["payload"] == {"recurrence": None}


@pytest.mark.parametrize("timing", [
    {"kind": "timed", "start_local": "2026-10-08T14:00:00", "end_local": "2026-10-08T14:50:00"},
    {"kind": "all_day", "start_date": "2026-10-08", "end_date": "2026-10-09"},
])
def test_timing_patch_requires_explicit_timezone(timing):
    with pytest.raises(ValidationError) as caught:
        EventFieldPatch.model_validate({"timing": timing})
    assert any(error["loc"][-1] == "timezone" for error in caught.value.errors())
