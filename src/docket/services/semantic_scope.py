"""Typed semantic projection, distinct from execution and evidence validation.

This projection can compare already-bound intent; it cannot establish that an
interpretation is authorized. Callers must independently validate its evidence.
Opaque domain JSON is kept verbatim, even when its keys resemble bookkeeping.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from pydantic import BaseModel

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.schemas.authority import (
    AttentionCaseResolutionInput,
    ChangeSetContent,
    ImportScope,
    MutationBase,
    OperatorChangeSetContent,
)
from docket.schemas.registry import IdentityResolutionBasis

_GROUPS = (
    "registry_changes", "preference_changes", "lane_changes", "event_changes",
    "tracked_context_changes", "resolution_changes",
)


def require_current_semantic_scope(scope: dict[str, Any]) -> None:
    if type(scope.get("format_version")) is not int or scope["format_version"] != 2:
        raise DocketError(
            code="semantic_request_migration_required",
            message="The preserved request needs an explicit semantic-binding migration.",
            details={
                "category": "implementation_validation",
                "constraint": "current_typed_semantic_scope",
                "authority_preserved": True, "next_action": "migrate_preserved_request",
            },
        )


def _invalid(constraint: str) -> DocketError:
    return DocketError(
        code="semantic_projection_unresolved",
        message="The planned-effect graph cannot yet be compared deterministically.",
        details={
            "category": "implementation_validation", "constraint": constraint,
            "authority_preserved": True, "next_action": "repair_staged_dependencies",
        },
    )


def semantic_authority_scope(
    content: dict[str, Any], exclusions: list[str],
) -> dict[str, Any]:
    """Preserve the existing authority representation, independent of comparison."""
    return _project_scope(
        content, exclusions, fixed_change_ids=False, normalize_create_defaults=False,
    )


def pinned_semantic_projection(
    content: dict[str, Any] | OperatorChangeSetContent | ChangeSetContent,
    exclusions: list[str], *, fixed_change_ids: bool = False,
) -> dict[str, Any]:
    """Compare already-bound effects; never derive a request's authority hash."""
    return _project_scope(
        content, exclusions, fixed_change_ids=fixed_change_ids, normalize_create_defaults=True,
    )


def _project_scope(
    content: dict[str, Any] | OperatorChangeSetContent | ChangeSetContent,
    exclusions: list[str], *, fixed_change_ids: bool, normalize_create_defaults: bool,
) -> dict[str, Any]:
    """Project current typed inputs without recursively erasing arbitrary keys.

    Dependency names and action order are mechanical. References resolve to the
    referenced effect's local semantic slot. Multiplicity is retained. If multiple
    indistinguishable creates are referenced, refuse to assert equivalence:
    otherwise a shared target could silently become two different targets.
    """
    if isinstance(content, OperatorChangeSetContent | ChangeSetContent):
        # Keep field-presence information when the caller already has exact
        # typed inputs. JSON exclude_none would erase explicit clear patches.
        parsed: OperatorChangeSetContent | ChangeSetContent = content
    else:
        scope = content.get("import_scope") or {}
        parsed = (
            ChangeSetContent.model_validate(content)
            if "authority_statement_refs" in scope or "provider_intents" in content
            else OperatorChangeSetContent.model_validate(content)
        )
    actions = [action for group in _GROUPS for action in getattr(parsed, group)]
    by_id = {action.change_id: action for action in actions}
    if len(by_id) != len(actions):
        raise _invalid("unique_change_ids")
    slots: dict[str, str] = {}
    referenced: set[str] = set()

    def dependency(change_id: str) -> dict[str, str]:
        if change_id not in by_id:
            raise _invalid("dependency_target_exists")
        referenced.add(change_id)
        return {"planned_effect_slot": slots[change_id]}

    def model_projection(
        model: BaseModel, *, resolve: bool, patch_fields: bool = False,
    ) -> dict[str, Any]:
        excluded: set[str] = set()
        if isinstance(model, OperatorChangeSetContent | ChangeSetContent):
            excluded = {"basis_refs", "expected_versions", "provider_intents", "occurrence_plans"}
        elif isinstance(model, MutationBase | AttentionCaseResolutionInput):
            excluded = {"change_id", "basis_refs"}
            if isinstance(model, AttentionCaseResolutionInput):
                excluded.add("case_revision_ref")
        elif isinstance(model, IdentityResolutionBasis) and model.kind == "operator_selection":
            excluded = {"utterance_ref"}
        elif isinstance(model, ImportScope):
            excluded = {"authority_statement_refs"}
        serialized = model.model_dump(mode="json")
        result: dict[str, Any] = {}
        for name in type(model).model_fields:
            if name in excluded:
                continue
            value = getattr(model, name)
            # Only mutation payloads carry partial-update field semantics.
            # Create defaults and optional envelope fields use null == absent.
            if value is None and (
                name not in model.model_fields_set
                or (normalize_create_defaults and not patch_fields)
            ):
                continue
            if name.endswith("_change_id") and isinstance(value, str):
                result[name] = dependency(value) if resolve else {"planned_target": True}
            elif name.endswith("_change_ids") and isinstance(value, list):
                result[name] = [
                    dependency(identifier) if resolve else {"planned_target": True}
                    for identifier in value
                ]
            elif isinstance(value, BaseModel):
                result[name] = model_projection(
                    value, resolve=resolve,
                    patch_fields=(
                        patch_fields or (isinstance(model, MutationBase) and name == "payload")
                    ),
                )
            elif isinstance(value, list) and any(isinstance(item, BaseModel) for item in value):
                result[name] = [
                    model_projection(item, resolve=resolve)
                    if isinstance(item, BaseModel) else serialized[name][i]
                    for i, item in enumerate(value)
                ]
            else:
                # Dict[str, Any] is domain data, not a recursive instruction to
                # drop basis_refs/change_id/etc. Nor are its strings references.
                result[name] = serialized[name]
        return result

    if fixed_change_ids:
        # Comparison-only fallback for an already bound graph with repeated
        # indistinguishable creates. Exact IDs prove graph identity; they must
        # NEVER be used when deriving a semantic authority hash. Renaming or
        # rewiring any dependency is conservatively non-equivalent in this mode.
        slots = {change_id: change_id for change_id in by_id}
        effects = model_projection(parsed, resolve=True)
        for group in _GROUPS:
            effects[group] = sorted([
                {"bound_change_id": action.change_id, **model_projection(action, resolve=True)}
                for action in getattr(parsed, group)
            ], key=sha256_json)
        return {
            "format_version": 2, "comparison_binding": "exact_existing_change_ids",
            "effects": effects, "explicit_exclusions": sorted(exclusions),
        }

    # Two passes also describe invalid cyclic drafts without recursing forever
    # or turning a later compilation error into a lost authority/request. The
    # transaction's whole-graph validator remains responsible for validity.
    slots = {
        action.change_id: sha256_json(model_projection(action, resolve=False))
        for action in actions
    }
    # Equal local fields can still denote distinct effects through distinct
    # dependencies (e.g. two Time bindings on differently titled Items). Refine
    # those partitions before deciding identity is ambiguous. At most N splits
    # are possible; no recursion or graph-isomorphism guess is needed.
    partition_count = len(set(slots.values()))
    while True:
        projected = {
            action.change_id: model_projection(action, resolve=True) for action in actions
        }
        refined = {change_id: sha256_json(value) for change_id, value in projected.items()}
        refined_count = len(set(refined.values()))
        slots = refined
        if refined_count == partition_count:
            break
        partition_count = refined_count
    projected = {
        action.change_id: model_projection(action, resolve=True) for action in actions
    }
    effects = model_projection(parsed, resolve=True)
    for group in _GROUPS:
        effects[group] = sorted(
            [projected[action.change_id] for action in getattr(parsed, group)], key=sha256_json,
        )
    counts = Counter(slots.values())
    if any(counts[slots[change_id]] > 1 for change_id in referenced):
        raise _invalid("unambiguous_planned_target_identity")
    return {
        "format_version": 2,
        "effects": effects,
        "explicit_exclusions": sorted(exclusions),
    }
