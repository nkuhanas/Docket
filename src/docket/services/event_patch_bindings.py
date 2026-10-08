"""Keep a field patch's recorded interpretation distinct from mechanical repair.

These bindings do not prove a model's interpretation of the Operator. They stop
a retry from silently replacing that interpretation with different effects.
"""

from __future__ import annotations

from typing import Any

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import ChangeSet


def bind_event_patches(
    draft: ChangeSet,
    actions: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    bindings = dict(draft.compiler_manifest_json.get("event_patch_bindings", {}))
    previous = draft.staged_actions_json or []
    if any(row.get("mutation_type") == "canonical_event_modify" for row in previous) and (
        draft.compiler_manifest_json.get("execution_pin", {}).get("compiler_version", 0) < 3
    ):
        raise DocketError(
            code="event_patch_migration_required",
            message="The retained event edit needs effect-proved migration, not reinterpretation.",
            details={
                "authority_preserved": True,
                "next_action": "reconcile_preserved_event_effects",
                "field_path": ["staged_actions"],
                "constraint": "original_field_intent_proven",
            },
        )
    for action in actions.values():
        if action.get("object_type") != "canonical_event" or not action.get("object_ref"):
            continue
        target = action["object_ref"]
        scope = action.get("scope", {"kind": "one_time"})
        # Key by target: changing the selector/ID must not escape its original scope.
        key = sha256_json({"target_ref": target})
        effect = {
            "target_ref": target,
            "scope": scope,
            "mutation_type": action["mutation_type"],
            "payload": action.get("payload", {}),
        }
        prior = bindings.get(key)
        if (
            prior is not None
            and prior != effect
            and (draft.execution_binding_json.get("authority_kind") != "agent_request")
        ):
            raise DocketError(
                code="event_patch_effect_conflict",
                message="Changing the recorded field intent requires semantic reconciliation.",
                details={
                    "change_id": action["change_id"],
                    "field_path": ["payload"],
                    "constraint": "unchanged_recorded_event_field_intent",
                    "authority_preserved": True,
                    "next_action": "resolve_semantic_conflict",
                },
            )
        if action["mutation_type"] == "canonical_event_modify":
            bindings[key] = effect
    return bindings
