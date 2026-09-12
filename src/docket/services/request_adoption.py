"""Explicit, one-time migration of a bound direct request into assembly.

Only current typed, hash-matching stored inputs are eligible. This is not a
legacy decoder or an interpretation of the Operator's words. The original
semantic binding remains intact; later changes must still match its effects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import (
    AssemblyOperation,
    AttachmentEvidence,
    AuditEvent,
    ChangeSet,
    ChangeSetRevision,
    IntentSession,
    OperatorUtterance,
    RequestAssemblyAdoption,
    SemanticRequest,
    SemanticRequestAttempt,
    Source,
)
from docket.schemas.assembly import AssemblyAuthorityScopeInput
from docket.schemas.authority import ChangeSetContent, mutation_input_json
from docket.schemas.request_adoption import RequestAdoptionProof
from docket.schemas.request_specifications import RequestSourceBinding, RequestUtteranceBinding
from docket.services.changeset_diff import bounded_sample, compiled_diff
from docket.services.semantic_scope import freeform_authority_scope, require_current_semantic_scope

if TYPE_CHECKING:
    from docket.services.changeset_assembly import ChangeSetAssemblyService

GROUPS = (
    "registry_changes", "preference_changes", "lane_changes", "event_changes",
    "tracked_context_changes", "resolution_changes",
)


def _unproven(constraint: str, path: list[str]) -> DocketError:
    return DocketError(
        code="request_adoption_unproven",
        message="The preserved request cannot be proven equivalent for assembly adoption.",
        details={
            "category": "implementation_validation", "constraint": constraint,
            "field_path": path, "authority_preserved": True,
            "next_action": "reconcile_preserved_request", "new_authorization_required": False,
        },
    )


def _provider_hash(content: ChangeSetContent) -> str:
    return sha256_json(sorted([
        intent.model_dump(mode="json", exclude={"intent_id", "idempotency_key", "basis_refs"})
        for intent in content.provider_intents
    ], key=sha256_json))


def _revision_content(revision: ChangeSetRevision) -> ChangeSetContent:
    try:
        content = ChangeSetContent.model_validate({
            "basis_refs": revision.basis_refs, "import_scope": revision.import_scope_json,
            "expected_versions": revision.expected_versions,
            **{group: getattr(revision, group) for group in GROUPS},
            "provider_intents": revision.provider_intents,
            "occurrence_plans": revision.compiler_manifest_json.get("occurrence_plans", []),
        })
    except ValidationError as exc:
        raise _unproven("current_canonical_input_schema", ["original_revision"]) from exc
    payload = mutation_input_json(content)
    payload.setdefault("import_scope", None)
    if sha256_json(payload) != revision.parameter_hash:
        raise _unproven("original_parameter_hash", ["original_revision", "parameter_hash"])
    return content


def _source_bindings(session: Session, refs: set[str]) -> list[RequestSourceBinding]:
    sources = {row.ref_id: row for row in session.scalars(select(Source).where(
        Source.ref_id.in_(refs),
    ))}
    attachments = {row.ref_id: row for row in session.scalars(select(AttachmentEvidence).where(
        AttachmentEvidence.ref_id.in_(refs),
    ))}
    if refs - sources.keys():
        raise _unproven("retained_source_identity", ["source_bindings"])
    return [RequestSourceBinding(
        source_ref=ref, source_manifest_hash=sources[ref].content_hash,
        attachment_content_hash=attachments[ref].content_hash if ref in attachments else None,
        evidence_state="attachment_recorded" if ref in attachments else "source_recorded",
    ) for ref in sorted(refs)]


def _utterance_bindings(session: Session, refs: list[str]) -> list[RequestUtteranceBinding]:
    rows = list(session.scalars(select(OperatorUtterance).where(
        OperatorUtterance.ref_id.in_(refs),
    )))
    if set(refs) != {row.ref_id for row in rows}:
        raise _unproven("original_utterance_evidence", ["originating_utterances"])
    return [RequestUtteranceBinding(utterance_ref=row.ref_id, content_hash=row.content_hash)
            for row in sorted(rows, key=lambda row: row.ref_id)]


def read_adoption(
    session: Session, request: SemanticRequest,
) -> tuple[RequestAssemblyAdoption, RequestAdoptionProof] | None:
    row = session.get(RequestAssemblyAdoption, request.ref_id)
    if row is None:
        return None
    try:
        proof = RequestAdoptionProof.model_validate(row.proof_json)
    except ValidationError as exc:
        raise _unproven("adoption_proof_schema", ["adoption"]) from exc
    if (
        sha256_json(proof.model_dump(mode="json")) != row.proof_hash
        or proof.original_authority_scope_hash != request.authority_scope_hash
        or proof.original_binding_hash != sha256_json(request.selected_option_binding)
        or proof.origin_utterances != _utterance_bindings(session, request.origin_utterance_refs)
        or proof.source_bindings != _source_bindings(
            session, {source.source_ref for source in proof.source_bindings},
        )
    ):
        raise _unproven("immutable_adoption_evidence", ["adoption", "proof_hash"])
    return row, proof


def verify_adopted_content(
    session: Session, changeset: ChangeSet, content: ChangeSetContent | None,
    *, compiled: bool = True,
) -> None:
    request = session.scalar(select(SemanticRequest).where(
        SemanticRequest.ref_id == changeset.semantic_request_ref,
    ))
    if request is None:
        return
    adopted = read_adoption(session, request)
    if adopted is None:
        return
    row, proof = adopted
    original = session.get(ChangeSetRevision, row.original_revision_id)
    target = session.get(ChangeSetRevision, row.adopted_revision_id)
    if original is None or target is None or (
        row.change_set_id != changeset.id
        or original.change_set_id != changeset.id or target.change_set_id != changeset.id
        or original.semantic_request_ref != request.ref_id
        or target.semantic_request_ref != request.ref_id
        or original.parameter_hash != proof.original_parameter_hash
        or original.authority_scope_hash != proof.original_authority_scope_hash
        or target.revision != original.revision + 1
        or changeset.current_revision < target.revision
    ):
        raise _unproven("original_and_adopted_revision_binding", ["adoption", "revision"])
    prior = _revision_content(original)
    scope = (request.selected_option_binding or {}).get("scope", {})
    if (
        sha256_json(freeform_authority_scope(prior, proof.assembly_boundary.resolved_intent))
        != request.authority_scope_hash
    ):
        raise _unproven("original_semantic_authority_scope", ["adoption", "authority_scope_hash"])
    if content is None or (
        freeform_authority_scope(content, proof.assembly_boundary.resolved_intent) != scope
        or (compiled and _provider_hash(content) != proof.provider_effect_hash)
    ):
        differences = compiled_diff(prior, content) if content is not None else [{
            "field_path": [], "change": "removed", "constraint": "original_effects_required",
        }]
        raise DocketError(
            code="adopted_request_scope_conflict",
            message="This patch changes the preserved request's effects; the draft is unchanged.",
            details={
                "category": "semantic_conflict", "constraint": "original_authorized_effects",
                "authority_preserved": True, "next_action": "reconcile_semantic_scope",
                "difference_count": len(differences), "differences": bounded_sample(differences),
            },
        )


def adopt_request(
    service: ChangeSetAssemblyService, *, changeset: ChangeSet,
    operation: AssemblyOperation, request: SemanticRequest, attempt: SemanticRequestAttempt,
    utterance: OperatorUtterance, intent_session: IntentSession,
) -> dict[str, Any]:
    """Called with request/draft locks and the exact causal observation checked."""
    existing = read_adoption(service.session, request)
    if existing is not None:
        return service._terminal(operation, {
            "ok": True, "disposition": "no_op", "draft_ref": changeset.ref_id,
            "semantic_request_ref": request.ref_id, "adoption_proof_hash": existing[0].proof_hash,
            "current_revision": changeset.current_revision,
            "next": {"action": "review_changeset", "view": "summary"},
        })
    binding = request.selected_option_binding or {}
    if binding.get("kind") != "freeform_turn":
        raise _unproven("original_direct_request", ["selected_option_binding", "kind"])
    if request.authority_availability != "available":
        raise _unproven("available_original_authority", ["authority_availability"])
    scope = binding.get("scope")
    if not isinstance(scope, dict):
        raise _unproven("current_typed_semantic_scope", ["selected_option_binding", "scope"])
    try:
        require_current_semantic_scope(scope)
    except DocketError as exc:
        raise _unproven(
            "current_typed_semantic_scope", ["selected_option_binding", "scope"],
        ) from exc
    before = service.session.scalar(select(ChangeSetRevision).where(
        ChangeSetRevision.change_set_id == changeset.id,
        ChangeSetRevision.revision == changeset.current_revision,
    ))
    if before is None or before.semantic_request_ref != request.ref_id:
        raise _unproven("original_request_revision", ["original_revision"])
    prior = _revision_content(before)
    # Verify the mutable working copy against immutable evidence before adopting.
    if any(getattr(changeset, name) != getattr(before, name) for name in (
        *GROUPS, "provider_intents", "basis_refs", "expected_versions", "import_scope_json",
        "execution_binding_json", "authority_scope_hash", "precondition_hash",
        "normalized_entries_json", "compiled_action_ownership_json", "staged_actions_json",
        "compiler_manifest_json",
    )):
        raise _unproven("unchanged_original_snapshot", ["original_revision"])
    resolved_intent = scope.get("resolved_intent")
    if not isinstance(resolved_intent, dict) or (
        sha256_json(scope) != request.authority_scope_hash
        or freeform_authority_scope(prior, resolved_intent) != scope
        or before.authority_scope_hash != request.authority_scope_hash
    ):
        raise _unproven("original_semantic_authority_scope", ["selected_option_binding", "scope"])
    if changeset.normalized_entries_json or changeset.compiled_action_ownership_json:
        raise _unproven("direct_request_has_no_unverified_compiler_ownership", ["ownership"])
    actions = [action for group in GROUPS for action in getattr(prior, group)]
    source_refs = set(prior.import_scope.source_refs if prior.import_scope else [])
    source_refs.update(ref for ref in prior.basis_refs if ref.startswith("src_"))
    for action in actions:
        source_refs.update(ref for ref in action.basis_refs if ref.startswith("src_"))
        source_refs.update(getattr(getattr(action, "create_spec", None), "source_refs", []))
    try:
        boundary = AssemblyAuthorityScopeInput(
            resolved_intent=resolved_intent,
            allowed_mutation_types=sorted({action.mutation_type for action in actions}),
            planned_create_types=sorted({action.object_type for action in actions
                                         if action.action == "create"}),
            source_refs=sorted(source_refs),
            target_refs=sorted({ref for action in actions for ref in service._semantic_target_refs(
                mutation_input_json(action),
            )}),
            event_scopes={
                action.object_ref: action.scope for action in actions
                if action.object_ref is not None and hasattr(action, "scope")
            },
        )
    except ValidationError as exc:
        raise _unproven("bounded_typed_assembly_scope", ["assembly_boundary"]) from exc
    proof = RequestAdoptionProof(
        original_authority_scope_hash=request.authority_scope_hash,
        original_parameter_hash=before.parameter_hash,
        original_binding_hash=sha256_json(binding), provider_effect_hash=_provider_hash(prior),
        origin_utterances=_utterance_bindings(service.session, request.origin_utterance_refs),
        source_bindings=_source_bindings(service.session, source_refs), assembly_boundary=boundary,
    )
    errors = service.changesets._validate(intent_session, prior, require_handlers=False)
    changeset.staged_actions_json = [mutation_input_json(action) for action in actions]
    changeset.execution_binding_json = {
        "kind": "incremental_assembly", "adopted_from_revision": before.revision,
        "original_binding_hash": proof.original_binding_hash,
    }
    changeset.validation_errors = errors
    changeset.state = "draft" if errors else "validated"
    if not errors:
        request.commit_state = "pending"
        intent_session.commit_state = "pending"
        attempt.state = "pending"
    proof_json = proof.model_dump(mode="json")
    proof_hash = sha256_json(proof_json)
    changeset.compiler_manifest_json = {
        **changeset.compiler_manifest_json,
        "request_adoption": {"from_revision": before.revision, "proof_hash": proof_hash},
    }
    revision = service._write_revision(
        changeset=changeset, content=prior, operation=operation, request_boundary=boundary,
    )
    service.session.flush()
    service.session.add(RequestAssemblyAdoption(
        semantic_request_ref=request.ref_id, change_set_id=changeset.id,
        original_revision_id=before.id, adopted_revision_id=revision.id,
        proof_json=proof_json, proof_hash=proof_hash,
    ))
    operation.change_set_ref = changeset.ref_id
    attempt.change_set_ref = changeset.ref_id
    service.session.add(AuditEvent(
        event_type="changeset.request_adopted", entity_type="changeset", entity_id=changeset.id,
        actor_type="docket_compiler", actor_id=None, request_id=None,
        primary_ref=changeset.ref_id, affected_refs=[changeset.ref_id, request.ref_id],
        basis_refs=[utterance.ref_id], data={
            "from_revision": before.revision, "to_revision": revision.revision,
            "adoption_proof_hash": proof_hash, "semantic_scope_changed": False,
            "authority_scope_hash": request.authority_scope_hash,
        },
    ))
    # Migration does not mark the new effects observed; a bounded summary suffices.
    from docket.services.changeset_assembly import (
        _diagnostic_projection,
        _request_authority_receipt,
    )

    return service._terminal(operation, {
        "ok": True, "disposition": "saved_with_errors" if errors else "ready_to_commit",
        **_request_authority_receipt(request), "draft_ref": changeset.ref_id,
        "current_revision": revision.revision, "observation_required": True,
        "readiness": "saved_with_errors" if errors else "ready_to_commit",
        "assembly_ready": not errors, "adoption_proof_hash": proof_hash,
        **_diagnostic_projection(changeset_ref=changeset.ref_id,
                                 revision=revision.revision, errors=errors),
        "next": {"action": "review_changeset", "view": "summary"},
    })
