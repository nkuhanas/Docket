from __future__ import annotations

import pytest
from pydantic import ValidationError

from docket.schemas.assembly import (
    AssembledChangeSetSubmission,
    DirectChangeSetSubmission,
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


def test_commit_forms_are_exact_and_exclusive() -> None:
    assembled = AssembledChangeSetSubmission.model_validate(
        {"commit_mode": "assembled"}
    )
    assert assembled.commit_mode == "assembled"
    with pytest.raises(ValidationError):
        AssembledChangeSetSubmission.model_validate(
            {"commit_mode": "assembled", "content": None}
        )

    direct = DirectChangeSetSubmission.model_validate(
        {
            "commit_mode": "direct",
            "resolved_intent": {"intent": "small direct request"},
            "content": None,
        }
    )
    assert direct.commit_mode == "direct"
    with pytest.raises(ValidationError):
        DirectChangeSetSubmission.model_validate({"commit_mode": "direct"})


def test_workload_limits_are_independent_of_output_budget() -> None:
    assert MAX_DRAFT_ENTRIES == 250
    assert MAX_DRAFT_REVISIONS == 1_000
    assert MAX_CANONICAL_ACTIONS == 1_000
    assert MAX_PROVIDER_OPERATIONS == 500
    assert MAX_DRAFT_ENTRIES > 30
