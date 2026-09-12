"""Explicit current-schema recompilation, never an old-protocol decoder.

This path proves equality with an already pinned semantic projection, or one
specific source-verified title coalescence with every other effect unchanged.
It is not a general verifier of source interpretation or permission to widen scope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import (
    AssemblyOperation,
    AuditEvent,
    ChangeSet,
    ChangeSetRevision,
    IntentSession,
    OperatorUtterance,
    SemanticRequest,
    SemanticRequestAttempt,
)
from docket.schemas.assembly import AssemblyAuthorityScopeInput, NormalizedEntryInput
from docket.schemas.authority import ChangeSetContent, mutation_input_json
from docket.services.changeset_compiler import (
    COMPILER_IDENTIFIER,
    COMPILER_VERSION,
    compile_normalized_entry,
    normalized_entry_record,
)
from docket.services.changeset_diff import bounded_sample, compiled_diff
from docket.services.changeset_pins import effect_hash
from docket.services.request_adoption import verify_adopted_content
from docket.services.request_specifications import read_request_proposal
from docket.services.semantic_scope import pinned_semantic_projection
from docket.services.source_title_repair import coalesce_source_titles

if TYPE_CHECKING:
    from docket.services.changeset_assembly import ChangeSetAssemblyService

_ENTRY_METADATA = {
    "statement_ref", "compiler_identifier", "compiler_version", "input_schema_version",
    "normalized_input_hash", "compilation_errors",
}
_ENTRY_ADAPTER: TypeAdapter[NormalizedEntryInput] = TypeAdapter(NormalizedEntryInput)


def _blocked(code: str, constraint: str, next_action: str) -> DocketError:
    return DocketError(
        code=code, message="The preserved draft cannot be mechanically recompiled as requested.",
        details={
            "category": "implementation_validation", "constraint": constraint,
            "authority_preserved": True, "next_action": next_action,
        },
    )


def _entry(record: dict[str, Any]) -> NormalizedEntryInput:
    payload = {key: value for key, value in record.items() if key not in _ENTRY_METADATA}
    try:
        parsed = _ENTRY_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        raise _blocked(
            "draft_input_migration_required", "current_normalized_schema",
            "migrate_preserved_request",
        ) from exc
    if (
        sha256_json(payload) != record.get("normalized_input_hash")
        or sha256_json(parsed.model_dump(mode="json", exclude_none=True)) != sha256_json(payload)
    ):
        raise _blocked(
            "draft_execution_pin_mismatch", "unchanged_normalized_input",
            "reconcile_draft_revision",
        )
    return parsed


def _semantic_projection(
    content: ChangeSetContent, exclusions: list[str], *, fixed_change_ids: bool = False,
) -> dict[str, Any]:
    projection = pinned_semantic_projection(content, exclusions, fixed_change_ids=fixed_change_ids)
    # Provider account, target, kind and opaque parameters are NOT bookkeeping.
    # Only the compiler's operation identity/provenance slots may differ here.
    projection["provider_effects"] = sorted([
        intent.model_dump(mode="json", exclude={"intent_id", "idempotency_key", "basis_refs"})
        for intent in content.provider_intents
    ], key=sha256_json)
    return projection


def recompile_draft(
    service: ChangeSetAssemblyService, *, changeset: ChangeSet, operation: AssemblyOperation,
    attempt: SemanticRequestAttempt, utterance: OperatorUtterance, intent_session: IntentSession,
    scope: AssemblyAuthorityScopeInput,
) -> dict[str, Any]:
    """The caller holds the request/draft locks and verified causal observation."""
    prior = service.changesets.verify_execution_revision(changeset, for_migration=True)
    if prior is None:
        raise _blocked(
            "draft_recompile_semantics_unavailable", "pinned_semantic_projection",
            "resolve_source_semantics",
        )
    before = service.session.scalar(select(ChangeSetRevision).where(
        ChangeSetRevision.change_set_id == changeset.id,
        ChangeSetRevision.revision == changeset.current_revision,
    ))
    assert before is not None  # verified above, under the same draft lock
    owned = {
        change_id for owner in changeset.compiled_action_ownership_json
        for change_id in owner["change_ids"]
    }
    actions = {
        key: value for key, value in service._draft_actions(changeset).items() if key not in owned
    }
    entries: list[dict[str, Any]] = []
    ownership: list[dict[str, Any]] = []
    for record in changeset.normalized_entries_json:
        entry = _entry(record)
        compiled = compile_normalized_entry(
            entry, utterance_ref=utterance.ref_id, statement_ref=record["statement_ref"],
            calendar_lane=service._entry_lane(entry, actions),
        )
        for action in compiled.actions:
            identifier = str(action["change_id"])
            if identifier in actions:
                raise _blocked(
                    "compiled_action_collision", "exclusive_entry_ownership",
                    "repair_staged_dependencies",
                )
            actions[identifier] = dict(action)
        entries.append(normalized_entry_record(entry, statement_ref=record["statement_ref"]))
        ownership.append({
            **compiled.ownership,
            "coverage": compiled.coverage.model_dump(mode="json", exclude_none=True),
        })
    content = service._content(
        changeset=changeset, utterance=utterance, actions=actions, entries=entries,
        ownership=ownership, expected_versions=dict(changeset.expected_versions),
    )
    if content is None:
        raise DocketError(
            code="draft_recompile_semantic_conflict",
            message="Recompilation removed the pinned effects; the existing draft is preserved.",
            details={
                "category": "semantic_conflict", "constraint": "unchanged_authorized_effects",
                "authority_preserved": True, "next_action": "resolve_source_semantics",
                "difference_count": 1, "differences": [{
                    "subject_kind": "compiled_effect", "field_path": [],
                    "change": "removed", "constraint": "nonempty_pinned_effects",
                }],
            },
        )
    content = service.changesets._compile_required_provider_intents(
        content, changeset_idempotency_key=changeset.idempotency_key,
    )
    fixed_change_ids = False
    try:
        old_scope = _semantic_projection(prior, scope.explicit_exclusions)
        new_scope = _semantic_projection(content, scope.explicit_exclusions)
    except DocketError as exc:
        if exc.code != "semantic_projection_unresolved" or (
            (exc.details or {}).get("constraint") != "unambiguous_planned_target_identity"
        ):
            raise
        fixed_change_ids = True
        old_scope = _semantic_projection(prior, scope.explicit_exclusions, fixed_change_ids=True)
        new_scope = _semantic_projection(content, scope.explicit_exclusions, fixed_change_ids=True)
    semantic_request = service.session.scalar(select(SemanticRequest).where(
        SemanticRequest.ref_id == attempt.semantic_request_ref
    ))
    assert semantic_request is not None  # locked/bound by the calling service
    repair_proofs: list[dict[str, Any]] = []
    if old_scope != new_scope:
        permitted, repair_proofs = coalesce_source_titles(
            service.session, prior=prior,
            proposal=read_request_proposal(
                service.session, semantic_request_ref=semantic_request.ref_id,
                version=before.revision,
            ),
        )
        if repair_proofs:
            old_scope = _semantic_projection(
                permitted, scope.explicit_exclusions, fixed_change_ids=fixed_change_ids,
            )
    if old_scope != new_scope:
        differences = compiled_diff(prior, content)
        raise DocketError(
            code="draft_recompile_semantic_conflict",
            message="Recompilation changes the bound effects; the existing draft is preserved.",
            details={
                "category": "semantic_conflict", "constraint": "unchanged_authorized_effects",
                "authority_preserved": True, "next_action": "resolve_source_semantics",
                "difference_count": len(differences), "differences": bounded_sample(differences),
            },
        )
    verify_adopted_content(service.session, changeset, content)
    errors = service.changesets._validate(intent_session, content, require_handlers=False)
    # No mutation of the draft occurred before equivalence was proved.
    changeset.normalized_entries_json = entries
    changeset.staged_actions_json = sorted(
        actions.values(), key=lambda item: str(item["change_id"])
    )
    changeset.compiled_action_ownership_json = ownership
    migration = {
        "from_revision": before.revision,
        "to_revision": before.revision + 1,
        "comparison_rule": "typed_semantic_projection_v2_and_provider_effect_equality",
        "authority_scope_hash": changeset.authority_scope_hash,
        "semantic_projection_hash": sha256_json(new_scope),
        "semantic_scope_changed": False,
        "dependency_comparison": (
            "exact_existing_change_ids" if fixed_change_ids else "semantic_slots"
        ),
        "old_compiled_effect_hash": effect_hash(mutation_input_json(prior)),
        "new_compiled_effect_hash": effect_hash(mutation_input_json(content)),
    }
    if repair_proofs:
        migration.update({
            "comparison_rule": "source_title_coalescence_v1_and_remaining_effect_equality_v2",
            "source_title_repair_count": len(repair_proofs),
            "source_title_proof_hash": sha256_json(repair_proofs),
        })
    changeset.compiler_manifest_json = {
        "identifier": COMPILER_IDENTIFIER, "version": COMPILER_VERSION,
        "entry_count": len(entries), "migration": migration,
        **({"source_title_repair_proofs": repair_proofs} if repair_proofs else {}),
    }
    changeset.validation_errors = errors
    changeset.state = "draft" if errors else "validated"
    if not errors:
        semantic_request.commit_state = "pending"
        intent_session.commit_state = "pending"
        attempt.state = "pending"
    revision = service._write_revision(changeset=changeset, content=content, operation=operation)
    # Keep this attempt's previous observation. Even the migration requester
    # must observe the new effects/diagnostics before staging or committing.
    operation.change_set_ref = changeset.ref_id
    service.session.add(AuditEvent(
        event_type="changeset.recompiled", entity_type="changeset", entity_id=changeset.id,
        actor_type="docket_compiler", actor_id=None, request_id=None,
        primary_ref=changeset.ref_id,
        affected_refs=[changeset.ref_id, attempt.semantic_request_ref],
        basis_refs=[utterance.ref_id], data=migration,
    ))
    from docket.services.changeset_assembly import (
        _cursor_encode,
        _diagnostic_projection,
        _request_authority_receipt,
    )

    return service._terminal(operation, {
        "ok": True, "disposition": "saved_with_errors" if errors else "ready_to_commit",
        **_request_authority_receipt(semantic_request),
        "draft_ref": changeset.ref_id, "current_revision": revision.revision,
        "readiness": "saved_with_errors" if errors else "ready_to_commit",
        "assembly_ready": not errors, "observation_required": True,
        "compiler_migration": migration,
        **_diagnostic_projection(
            changeset_ref=changeset.ref_id, revision=revision.revision, errors=errors,
        ),
        "diff_review": {
            "view": "diff", "cursor": _cursor_encode({
                "format_version": 1, "changeset_ref": changeset.ref_id,
                "revision": revision.revision, "view": "diff", "position": 0,
                "mutation_types": [], "entry_types": [],
            }),
        },
        "next": {"action": "review_changeset", "view": "summary"},
    })
