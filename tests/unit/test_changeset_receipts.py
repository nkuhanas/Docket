from __future__ import annotations

import json

from docket.domain.public_refs import new_public_ref
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
