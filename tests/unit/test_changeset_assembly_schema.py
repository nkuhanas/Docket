from __future__ import annotations

import pytest
from pydantic import ValidationError

from docket.schemas.assembly import (
    StagePatchInput,
)
from docket.services.changeset_assembly import (
    MAX_CANONICAL_ACTIONS,
    MAX_DRAFT_ENTRIES,
    MAX_DRAFT_REVISIONS,
    MAX_PROVIDER_OPERATIONS,
)


def _item_operation(index: int) -> dict[str, object]:
    return {
        "operation": "action_upsert",
        "action": {
            "mutation_type": "item_create",
            "change_id": f"item-{index}",
            "action": "create",
            "object_type": "item",
            "affected_fields": ["title"],
            "basis_refs": ["utt_01ARZ3NDEKTSV4RRFFQ69G5FAV"],
            "create_spec": {"title": f"Item {index}"},
        },
    }


def test_stage_patch_is_bounded_and_discriminated() -> None:
    accepted = StagePatchInput.model_validate(
        {
            "operations": [
                _item_operation(1),
                {"operation": "action_remove", "change_id": "item-0"},
            ]
        }
    )
    assert [operation.operation for operation in accepted.operations] == [
        "action_upsert",
        "action_remove",
    ]

    with pytest.raises(ValidationError):
        StagePatchInput.model_validate(
            {"operations": [_item_operation(index) for index in range(51)]}
        )

    with pytest.raises(ValidationError):
        StagePatchInput.model_validate(
            {"operations": [{"operation": "invented_patch", "change_id": "x"}]}
        )


def test_commit_submission_variants_are_removed_not_aliased() -> None:
    import docket.schemas.assembly as assembly

    assert not hasattr(assembly, "DirectChangeSetSubmission")
    assert not hasattr(assembly, "AssembledChangeSetSubmission")
    assert not hasattr(assembly, "ChangeSetSubmission")


def test_workload_limits_are_independent_of_output_budget() -> None:
    assert MAX_DRAFT_ENTRIES == 250
    assert MAX_DRAFT_REVISIONS == 1_000
    assert MAX_CANONICAL_ACTIONS == 1_000
    assert MAX_PROVIDER_OPERATIONS == 500
    assert MAX_DRAFT_ENTRIES > 30
