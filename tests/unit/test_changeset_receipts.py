from __future__ import annotations

import json

import pytest

from docket.domain.public_refs import new_public_ref
from docket.services.calendar_delivery_notice import calendar_delivery_notice
from docket.services.change_sets import (
    ChangeSetApplicationReceipt,
    ChangeSetEffectReceipt,
    ProviderOperationReceipt,
)


def test_large_changeset_receipt_returns_exact_counts_and_bounded_samples() -> None:
    receipt = ChangeSetApplicationReceipt()
    for index in range(100):
        item_ref = new_public_ref("item")
        operation_ref = new_public_ref("op")
        receipt.add_refs([item_ref, operation_ref])
        receipt.effects.append(
            ChangeSetEffectReceipt(
                change_id=f"item-{index}",
                mutation_type="item_create",
                action="create",
                object_type="item",
                refs=(item_ref,),
            )
        )
        receipt.provider_operations.append(
            ProviderOperationReceipt(
                intent_id=f"project-{index}",
                operation_type="calendar_create_event",
                refs=(operation_ref,),
                target_refs=(item_ref,),
            )
        )

    projection = {
        "ok": True,
        "disposition": "committed",
        **receipt.affected_projection(),
        **receipt.projection(),
    }

    assert projection["affected_ref_count"] == 200
    assert projection["affected_refs_truncated"] is True
    assert len(projection["affected_refs"]) == 25
    assert projection["effect_count"] == 100
    assert projection["effect_counts"] == {"item_create": 100}
    assert projection["effects_truncated"] is True
    assert len(projection["effects"]) == 10
    assert projection["provider_operation_count"] == 100
    assert projection["provider_operation_counts"] == {"calendar_create_event": 100}
    assert projection["provider_operations_truncated"] is True
    assert len(projection["provider_operations"]) == 10
    assert len(json.dumps(projection, separators=(",", ":")).encode("utf-8")) <= 16_384


@pytest.mark.parametrize("operation_count", [0, 1, 100])
def test_durable_calendar_notice_is_bounded_and_describes_commit_not_delivery(operation_count):
    receipt = ChangeSetApplicationReceipt(provider_operations=[
        ProviderOperationReceipt(
            intent_id=f"project-{index}", operation_type="calendar_create_event",
            refs=(new_public_ref("op"),), target_refs=(new_public_ref("evt"),),
        )
        for index in range(operation_count)
    ])
    result = receipt.durable_projection(
        changeset_ref=new_public_ref("chg"), semantic_request_ref=new_public_ref("sreq"),
    )
    assert result["provider_operation_count"] == operation_count
    notice = result["calendar_delivery_notice"]
    if operation_count:
        assert "queued at commit, not confirmed" in notice
    else:
        assert "Docket only" in notice
        assert "queued no Google Calendar delivery" in notice
    assert len(notice.encode()) < 256
    assert len(json.dumps(result).encode()) < 2048


@pytest.mark.parametrize("operation_count", [0, 1])
def test_invalid_draft_notice_never_claims_commit_or_queued_delivery(operation_count):
    notice = calendar_delivery_notice(operation_count, phase="draft", has_errors=True)
    assert notice == (
        "Draft has errors; nothing has committed or been queued for Google Calendar."
    )
