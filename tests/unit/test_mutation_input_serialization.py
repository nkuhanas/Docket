from copy import deepcopy

import pytest

from docket.domain.public_refs import new_public_ref
from docket.schemas.authority import (
    ChangeSetContent,
    OperatorChangeSetContent,
    mutation_input_json,
)
from docket.services.change_sets import _content_payload
from docket.services.changeset_diff import compiled_diff
from docket.services.changeset_pins import effect_hash
from docket.services.semantic_options import (
    _replace_authority_slot,
    complete_selection_provenance,
)
from docket.services.semantic_scope import semantic_authority_scope


def _content():
    utterance = new_public_ref("utt")
    return OperatorChangeSetContent.model_validate({
        "basis_refs": [utterance], "tracked_context_changes": [{
            "mutation_type": "item_modify", "action": "update", "object_type": "item",
            "object_ref": new_public_ref("item"), "change_id": "patch",
            "payload": {"title": "Retitled", "description": None,
                        "metadata_json": {"payload": {"description": None}, "flag": False}},
            "affected_fields": ["title", "description", "metadata_json"],
            "basis_refs": [utterance],
        }],
    })


@pytest.mark.parametrize("exclude_none", [True, False])
def test_input_roundtrip_keeps_patch_presence_and_opaque_json(exclude_none):
    content = _content()
    payload = content.tracked_context_changes[0].payload.model_dump(exclude_unset=True)
    encoded = mutation_input_json(content, exclude_none=exclude_none)
    assert encoded["tracked_context_changes"][0]["payload"] == payload
    assert encoded["tracked_context_changes"][0]["mutation_type"] == "item_modify"
    restored = OperatorChangeSetContent.model_validate(encoded)
    internal = restored.to_internal()
    restored_again = OperatorChangeSetContent.from_internal(internal)
    patch = restored_again.tracked_context_changes[0].payload
    assert patch.model_dump(exclude_unset=True) == payload
    assert restored_again.tracked_context_changes[0].payload.model_fields_set == set(payload)


def test_selection_provenance_completion_does_not_clear_unspecified_fields():
    original = _content()
    encoded = mutation_input_json(original, exclude_none=False)
    template, _ = _replace_authority_slot(encoded, original.basis_refs[0])
    completed = complete_selection_provenance(template, new_public_ref("utt"))
    assert completed["tracked_context_changes"][0]["payload"] == encoded[
        "tracked_context_changes"
    ][0]["payload"]
    assert semantic_authority_scope(completed, []) == semantic_authority_scope(encoded, [])


def test_compiled_diff_exposes_an_explicit_clear_not_omission():
    original = _content().to_internal()
    changed = deepcopy(mutation_input_json(original))
    del changed["tracked_context_changes"][0]["payload"]["description"]
    rows = compiled_diff(original, ChangeSetContent.model_validate(changed))
    description = next(row for row in rows if row["field_path"] == ["payload", "description"])
    assert description["before_present"] is True and description["before"] is None
    assert description["after_present"] is False and "after" not in description


def test_executable_hash_distinguishes_clear_without_reconstructing_omitted_input():
    original = _content().to_internal()
    original_payload = _content_payload(original)
    omitted_payload = deepcopy(original_payload)
    del omitted_payload["tracked_context_changes"][0]["payload"]["description"]
    restored = ChangeSetContent.model_validate(omitted_payload)
    assert _content_payload(restored) == omitted_payload
    assert effect_hash(original_payload) != effect_hash(_content_payload(restored))
