from __future__ import annotations

from copy import deepcopy

import pytest

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.services.semantic_options import (
    CURRENT_SELECTION_UTTERANCE,
    _replace_authority_slot,
    complete_selection_provenance,
)
from docket.services.semantic_scope import semantic_authority_scope


def _entity(utterance, change_id="organization", name="Cal Poly"):
    return {
        "mutation_type": "entity_create", "change_id": change_id, "action": "create",
        "object_type": "entity", "create_spec": {
            "entity_kind": "organization", "display_name": name,
        }, "affected_fields": ["identity"], "basis_refs": [utterance],
    }


def _policy(utterance):
    return {
        "mutation_type": "preference_create", "change_id": "policy", "action": "create",
        "object_type": "preference", "create_spec": {
            "preference_key": "test-policy", "policy_kind": "behavior",
            "target_type": "global", "policy_text": "Keep the exact supplied policy data.",
            "policy_json": {},
        }, "affected_fields": ["policy"], "basis_refs": [utterance],
    }


def _scope(content):
    return semantic_authority_scope(content, [])


@pytest.mark.parametrize("key", [
    "basis_refs", "source_refs", "change_id", "intent_id", "idempotency_key",
    "expected_versions", "utterance_ref", "case_revision_ref", "entity_change_id",
])
def test_opaque_domain_keys_cannot_disappear_from_authority_hash(key):
    utterance = new_public_ref("utt")
    content = {"basis_refs": [utterance], "preference_changes": [_policy(utterance)]}
    content["preference_changes"][0]["create_spec"]["policy_json"] = {key: "first"}
    original = _scope(content)
    assert original["effects"]["preference_changes"][0]["create_spec"]["policy_json"] == {
        key: "first"
    }
    content["preference_changes"][0]["create_spec"]["policy_json"][key] = "second"
    assert sha256_json(original) != sha256_json(_scope(content))


def test_dependency_renaming_order_and_provenance_do_not_change_semantic_scope():
    utterance = new_public_ref("utt")
    parent = _entity(utterance)
    child = _entity(utterance, "department", "Engineering Student Services")
    child["create_spec"]["parent_entity_change_id"] = "organization"
    content = {"basis_refs": [utterance], "registry_changes": [child, parent]}
    before = deepcopy(content)
    scope = _scope(content)
    repaired = deepcopy(content)
    repaired["registry_changes"].reverse()
    repaired["registry_changes"][0]["change_id"] = "parent-recompiled"
    repaired["registry_changes"][1]["change_id"] = "child-recompiled"
    repaired["registry_changes"][1]["create_spec"]["parent_entity_change_id"] = "parent-recompiled"
    repaired["basis_refs"] = [new_public_ref("utt")]
    repaired["expected_versions"] = {new_public_ref("ent"): 42}
    for action in repaired["registry_changes"]:
        action["basis_refs"] = repaired["basis_refs"]
    assert _scope(repaired) == scope
    assert content == before
    assert "parent-recompiled" not in str(_scope(repaired))
    repaired["registry_changes"][0]["create_spec"]["display_name"] = "Another organization"
    assert _scope(repaired) != scope


def test_create_multiplicity_and_ambiguous_shared_target_are_not_erased():
    utterance = new_public_ref("utt")
    content = {"basis_refs": [utterance], "registry_changes": [_entity(utterance)]}
    one = _scope(content)
    content["registry_changes"].append(_entity(utterance, "second"))
    two = _scope(content)
    assert one != two
    assert len(two["effects"]["registry_changes"]) == 2
    child = _entity(utterance, "child", "Department")
    child["create_spec"]["parent_entity_change_id"] = "organization"
    content["registry_changes"].append(child)
    with pytest.raises(DocketError) as error:
        _scope(content)
    assert error.value.details["constraint"] == "unambiguous_planned_target_identity"
    assert error.value.details["authority_preserved"] is True


def test_missing_dependency_does_not_claim_semantic_equivalence():
    utterance = new_public_ref("utt")
    action = _entity(utterance)
    action["create_spec"]["parent_entity_change_id"] = "missing"
    with pytest.raises(DocketError) as error:
        _scope({"basis_refs": [utterance], "registry_changes": [action]})
    assert error.value.details["constraint"] == "dependency_target_exists"


def test_cyclic_draft_can_be_described_without_claiming_it_is_valid():
    utterance = new_public_ref("utt")
    action = _entity(utterance)
    action["create_spec"]["parent_entity_change_id"] = "organization"
    original = _scope({"basis_refs": [utterance], "registry_changes": [action]})
    action["change_id"] = "renamed"
    action["create_spec"]["parent_entity_change_id"] = "renamed"
    assert _scope({"basis_refs": [utterance], "registry_changes": [action]}) == original
    del action["create_spec"]["parent_entity_change_id"]
    assert _scope({"basis_refs": [utterance], "registry_changes": [action]}) != original


def test_opaque_symbolic_strings_are_not_rewritten_as_operator_authority():
    utterance, selected = new_public_ref("utt"), new_public_ref("utt")
    content = {"basis_refs": [utterance], "preference_changes": [_policy(utterance)]}
    opaque = {
        "basis_refs": [utterance],
        "resolution_basis": {"utterance_ref": utterance},
        "literal": CURRENT_SELECTION_UTTERANCE,
        "price": "$100",
    }
    content["preference_changes"][0]["create_spec"]["policy_json"] = opaque
    template, replacements = _replace_authority_slot(content, utterance)
    assert replacements == 2
    assert template["preference_changes"][0]["create_spec"]["policy_json"] == opaque
    completed = complete_selection_provenance(template, selected)
    assert completed["basis_refs"] == [selected]
    assert completed["preference_changes"][0]["basis_refs"] == [selected]
    assert completed["preference_changes"][0]["create_spec"]["policy_json"] == opaque


def test_identity_selection_basis_is_a_declared_symbolic_slot():
    utterance, selected = new_public_ref("utt"), new_public_ref("utt")
    content = {"basis_refs": [utterance], "registry_changes": [
        _entity(utterance), {
            "mutation_type": "identity_binding_bind", "change_id": "bind",
            "action": "bind", "object_type": "identity_binding",
            "object_ref": new_public_ref("idn"), "payload": {
                "entity_change_id": "organization", "resolution_basis": {
                    "kind": "operator_selection", "utterance_ref": utterance,
                },
            }, "affected_fields": ["entity"], "basis_refs": [utterance],
        },
    ]}
    template, replacements = _replace_authority_slot(content, utterance)
    assert replacements == 4
    completed = complete_selection_provenance(template, selected)
    basis = completed["registry_changes"][1]["payload"]["resolution_basis"]
    assert basis["utterance_ref"] == selected
    # Compare the same serialization shape; completion cannot alter semantics.
    from docket.schemas.authority import OperatorChangeSetContent
    original = OperatorChangeSetContent.model_validate(content).model_dump(mode="json")
    assert _scope(completed) == _scope(original)


def test_null_patch_and_missing_patch_field_have_different_semantic_effects():
    utterance = new_public_ref("utt")
    content = {"basis_refs": [utterance], "tracked_context_changes": [{
        "mutation_type": "item_modify", "change_id": "edit", "action": "update",
        "object_type": "item", "object_ref": new_public_ref("item"),
        "payload": {"title": "New title"},
        "affected_fields": ["item"], "basis_refs": [utterance],
    }]}
    original = _scope(content)
    content["tracked_context_changes"][0]["payload"]["description"] = None
    assert _scope(content) != original


def test_occurrence_date_timezone_and_series_scope_are_semantic():
    utterance, series = new_public_ref("utt"), new_public_ref("evt")
    action = {
        "mutation_type": "canonical_event_cancel", "change_id": "cancel", "action": "retract",
        "object_type": "canonical_event", "object_ref": series,
        "scope": {"kind": "occurrence", "identity": {
            "series_ref": series, "original_date": "2026-09-14", "timezone": "America/Los_Angeles",
        }}, "affected_fields": ["status"], "basis_refs": [utterance],
    }
    content = {"basis_refs": [utterance], "event_changes": [action]}
    original = _scope(content)
    for field, value in [("original_date", "2026-09-15"), ("timezone", "America/New_York")]:
        changed = deepcopy(content)
        changed["event_changes"][0]["scope"]["identity"][field] = value
        assert _scope(changed) != original
    action["scope"] = {"kind": "entire_series"}
    assert _scope(content) != original


@pytest.mark.parametrize("change", ["fourth", "date", "time", "title", "lane", "location"])
def test_three_occurrence_projection_does_not_equate_expanded_or_changed_effects(change):
    utterance, lane = new_public_ref("utt"), new_public_ref("lane")
    actions = []
    for index in range(3):
        title = "2026 Fall Career Fair" if index < 2 else "2026 Business Career Fair"
        actions.append({
            "mutation_type": "canonical_event_create", "change_id": f"event-{index}",
            "action": "create", "object_type": "canonical_event", "create_spec": {
                "title": title, "lane_ref": lane, "event_spec": {
                    "title": title, "calendar_lane": "meetings",
                    "location": "Cal Poly Recreation Center, Building 43",
                    "timing": {
                        "kind": "timed", "start_local": f"2026-09-{16 + index}T10:00:00",
                        "end_local": f"2026-09-{16 + index}T{15 if index < 2 else 14}:00:00",
                        "timezone": "America/Los_Angeles",
                    },
                },
            }, "affected_fields": ["event"], "basis_refs": [utterance],
        })
    content = {"basis_refs": [utterance], "event_changes": actions}
    original = _scope(content)
    spec = actions[0]["create_spec"]
    if change == "fourth":
        extra = deepcopy(actions[0])
        extra["change_id"] = "fourth-event"
        actions.append(extra)
    elif change == "date":
        spec["event_spec"]["timing"]["start_local"] = "2026-09-15T10:00:00"
        spec["event_spec"]["timing"]["end_local"] = "2026-09-15T15:00:00"
    elif change == "time":
        spec["event_spec"]["timing"]["start_local"] = "2026-09-16T11:00:00"
    elif change == "title":
        spec["title"] = spec["event_spec"]["title"] = "Different event"
    elif change == "lane":
        spec["lane_ref"] = new_public_ref("lane")
    else:
        spec["event_spec"]["location"] = "Elsewhere"
    assert _scope(content) != original


def test_source_identity_is_preserved_even_when_evidence_refs_change():
    utterance, source = new_public_ref("utt"), new_public_ref("src")
    content = {"basis_refs": [utterance], "tracked_context_changes": [{
        "mutation_type": "item_create", "change_id": "item", "action": "create",
        "object_type": "item", "create_spec": {"title": "Tracked", "source_refs": [source]},
        "affected_fields": ["item"], "basis_refs": [utterance],
    }]}
    original = _scope(content)
    content["tracked_context_changes"][0]["basis_refs"] = [new_public_ref("stm")]
    assert _scope(content) == original
    content["tracked_context_changes"][0]["create_spec"]["source_refs"] = [new_public_ref("src")]
    assert _scope(content) != original
