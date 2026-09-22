"""Supporting attachment fields are not structured schedule entries.

Initial readings remain fallible. The immutable proof fixes the assembled
effect inventory, original utterances and source identities; later compilation
may add provenance, never reinterpret a date, duration, destination or scope.
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import (
    AttachmentEvidence,
    InterpretedStatement,
    OperatorUtterance,
    RequestFieldEvidence,
    SemanticRequest,
    Source,
)
from docket.schemas.assembly import AssemblyAuthorityScopeInput, FieldEvidenceInput
from docket.schemas.authority import (
    ChangeSetContent,
    ImportEffect,
    ImportScope,
    StatementInput,
    mutation_input_json,
)
from docket.services.attachment_evidence import (
    PDF_TEXT_EXTRACTOR,
    AttachmentEvidenceService,
    AttachmentTextService,
)
from docket.services.statements import StatementService

GROUPS = (
    "registry_changes", "preference_changes", "lane_changes", "event_changes",
    "tracked_context_changes", "resolution_changes",
)
_BINDINGS = TypeAdapter(list[FieldEvidenceInput])


def _source_refs(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value} if value.startswith("src_") else set()
    if isinstance(value, dict):
        return set().union(*(_source_refs(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_source_refs(item) for item in value))
    return set()


def _error(constraint: str, *, target: dict[str, Any] | None = None) -> DocketError:
    return DocketError(
        code="field_evidence_invalid", message="Supporting evidence does not bind this field.",
        details={
            "category": "evidence_validation", "constraint": constraint,
            "authority_preserved": True, "next_action": "repair_field_evidence_binding",
            **(target or {}),
        },
    )


def effect_projection(content: ChangeSetContent) -> dict[str, Any]:
    """Exact effect IDs/values, excluding ONLY declared provenance bookkeeping.

    Unlike an authority hash, this comparison is deliberately conservative: no
    dependency renaming, opaque JSON stripping, scope re-selection or defaults.
    """
    value = mutation_input_json(content)
    return {
        **{group: sorted([
            {key: val for key, val in action.items() if key != "basis_refs"}
            for action in value[group]
        ], key=lambda action: str(action["change_id"])) for group in GROUPS},
        "provider_intents": sorted([
            {key: val for key, val in intent.items()
             if key not in {"intent_id", "idempotency_key", "basis_refs"}}
            for intent in value["provider_intents"]
        ], key=sha256_json),
        "occurrence_plans": value["occurrence_plans"],
    }


def require_same_effects(before: ChangeSetContent, after: ChangeSetContent) -> None:
    if effect_projection(before) != effect_projection(after):
        raise DocketError(
            code="field_evidence_effect_conflict",
            message="Evidence repair cannot change the preserved semantic effects.",
            details={
                "category": "semantic_conflict", "constraint": "exact_original_effect_inventory",
                "authority_preserved": True, "next_action": "reconcile_source_interpretation",
            },
        )


def _source(session: Session, source_ref: str) -> tuple[Source, AttachmentEvidence]:
    source = session.scalar(select(Source).where(Source.ref_id == source_ref))
    attachment = session.scalar(select(AttachmentEvidence).where(
        AttachmentEvidence.ref_id == source_ref,
    ))
    if source is None or attachment is None or attachment.ingest_state != "available":
        raise _error("available_retained_attachment", target={"source_ref": source_ref})
    return source, attachment


def _identity(source: Source, attachment: AttachmentEvidence) -> dict[str, Any]:
    return {
        "source_ref": source.ref_id, "source_manifest_hash": source.content_hash,
        "attachment_content_hash": attachment.content_hash,
        "operator_utterance_ref": attachment.operator_utterance_ref,
    }


def _reader(session: Session) -> AttachmentEvidenceService:
    settings = get_settings()
    return AttachmentEvidenceService(
        session, encryption_key=settings.attachment_encryption_key(),
        encryption_key_ref=settings.attachment_encryption_key_ref,
        max_attachment_bytes=settings.attachment_max_bytes,
        max_total_bytes=settings.attachment_total_max_bytes,
    )


def read_proof(session: Session, request_ref: str | None) -> dict[str, Any] | None:
    row = session.get(RequestFieldEvidence, request_ref) if request_ref else None
    if row is None:
        return None
    proof = row.proof_json
    if proof.get("schema_version") != 1 or sha256_json(proof) != row.proof_hash:
        raise _error("immutable_field_evidence_digest")
    request = session.scalar(select(SemanticRequest).where(SemanticRequest.ref_id == request_ref))
    origins = {row.ref_id: row.content_hash for row in session.scalars(select(
        OperatorUtterance,
    ).where(OperatorUtterance.ref_id.in_(proof["originating_utterances"])))}
    if request is None or request.authority_scope_hash != proof["authority_scope_hash"] or (
        origins != proof["originating_utterances"]
        or set(request.origin_utterance_refs) != set(origins)
    ):
        raise _error("unchanged_original_authority")
    for identity in proof["sources"]:
        if _identity(*_source(session, identity["source_ref"])) != identity:
            raise _error("unchanged_original_source")
    return proof


def bind_field_evidence(
    session: Session, *, request: SemanticRequest, scope: AssemblyAuthorityScopeInput,
    content: ChangeSetContent, bindings: list[FieldEvidenceInput], direct_action_ids: set[str],
    from_revision: int,
) -> dict[str, Any]:
    """Called under the existing request/draft lock, before canonical validation."""
    supplied = [binding.model_dump(mode="json") for binding in bindings]
    existing = read_proof(session, request.ref_id)
    if existing is not None:
        if existing["bindings"] != supplied:
            raise _error("unchanged_recorded_field_interpretation")
        return existing
    actions = {action.change_id: mutation_input_json(action)
               for group in GROUPS for action in getattr(content, group)}
    origins = {row.ref_id: row for row in session.scalars(select(OperatorUtterance).where(
        OperatorUtterance.ref_id.in_(request.origin_utterance_refs),
    ))}
    if set(origins) != set(request.origin_utterance_refs):
        raise _error("original_utterance_present")
    sources: dict[str, dict[str, Any]] = {}
    statements: list[tuple[str, StatementInput]] = []
    seen: set[tuple[str, str, str]] = set()
    reader = _reader(session)
    verified_images: set[str] = set()
    for binding in bindings:
        if binding.source_ref not in scope.source_refs:
            raise _error("source_in_original_scope")
        source, attachment = _source(session, binding.source_ref)
        if attachment.operator_utterance_ref not in origins:
            raise _error("source_bound_to_original_utterance")
        sources[source.ref_id] = _identity(source, attachment)
        for target in binding.targets:
            detail = {"change_id": target.change_id, "field_path": target.field_path.split(".")}
            if target.change_id not in direct_action_ids:
                raise _error("supporting_field_on_direct_action_not_import_entry", target=detail)
            key = (source.ref_id, target.change_id, target.field_path)
            if key in seen:
                raise _error("one_source_binding_per_field", target=detail)
            seen.add(key)
            value: Any = actions.get(target.change_id)
            for segment in target.field_path.split("."):
                value = value.get(segment) if isinstance(value, dict) else None
            if not isinstance(value, str) or not (
                value == binding.value or (
                    target.match == "prefix" and any(
                        value.startswith(binding.value + separator)
                        for separator in (", ", " — ", "\n")
                    )
                )
            ):
                raise _error(
                    "interpreted_text_matches_exact_field_or_delimited_prefix", target=detail,
                )
        fragment_hash = binding.source_fragment_hash
        if attachment.media_type in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            if binding.extractor_identifier != "hermes.native-vision":
                raise _error("native_image_interpretation_not_ocr_claim")
            if source.ref_id not in verified_images:
                reader.plaintext(source.ref_id)  # verifies retained encrypted bytes, not meaning
                verified_images.add(source.ref_id)
            if fragment_hash is not None and fragment_hash != attachment.content_hash:
                raise _error("native_image_hash_matches_retained_bytes")
            fragment_hash = attachment.content_hash
        elif attachment.media_type == "application/pdf":
            if binding.extractor_identifier != PDF_TEXT_EXTRACTOR or fragment_hash is None:
                raise _error("read_pdf_fragment_before_binding")
            AttachmentTextService(reader).verify_fragment(
                source_ref=source.ref_id, locator=binding.source_fragment_locator,
                fragment_hash=fragment_hash, extractor_identifier=binding.extractor_identifier,
                extractor_version=binding.extractor_version,
            )
        else:
            raise _error("supported_field_evidence_media_type")
        statements.append((attachment.operator_utterance_ref, StatementInput(
            statement_kind="supporting_field_interpretation", subject_refs=[source.ref_id],
            predicate="supports_staged_fields", value_json={"value": binding.value},
            affected_fields=sorted({target.field_path for target in binding.targets}),
            interpretation_json={
                "targets": [target.model_dump(mode="json") for target in binding.targets],
                "interpretation_state": "recorded_interpretation",
                "independent_semantic_verification": False,
            },
            interpreter_version="docket.field-evidence.v1", source_ref=source.ref_id,
            source_fragment_locator=binding.source_fragment_locator,
            source_fragment_hash=fragment_hash, extractor_identifier=binding.extractor_identifier,
            extractor_version=binding.extractor_version,
        )))
    # Validate every binding before writing its statements. Dates, room numbers,
    # routing and other uncovered values retain their original utterance basis.
    supported: dict[str, set[str]] = {}
    for binding in bindings:
        for target in binding.targets:
            supported.setdefault(target.change_id, set()).add(binding.source_ref)
    for identifier in direct_action_ids:
        cited = _source_refs(actions[identifier])
        cited.update(source_ref for source_ref in session.scalars(select(
            InterpretedStatement.source_ref,
        ).where(InterpretedStatement.ref_id.in_(actions[identifier]["basis_refs"]))) if source_ref)
        if cited - supported.get(identifier, set()):
            raise _error("every_cited_source_has_field_binding", target={
                "change_id": identifier, "field_path": ["basis_refs"],
            })
    statement_refs = [StatementService(session).derive(origin, [statement])[0].ref_id
                      for origin, statement in statements]
    proof = {
        "schema_version": 1, "interpretation_state": "recorded_interpretation",
        "independent_semantic_verification": False,
        "authority_scope_hash": request.authority_scope_hash,
        "originating_utterances": {ref: row.content_hash for ref, row in sorted(origins.items())},
        "sources": sorted(sources.values(), key=lambda row: str(row["source_ref"])),
        "bindings": supplied, "statement_refs": statement_refs,
        "direct_action_ids": sorted(direct_action_ids),
        "effects": effect_projection(content), "from_revision": from_revision,
        "historical_derivation_backfilled": False,
    }
    session.add(RequestFieldEvidence(
        semantic_request_ref=request.ref_id, proof_json=proof, proof_hash=sha256_json(proof),
    ))
    session.flush()
    return proof


def compile_field_evidence(
    session: Session, *, request_ref: str | None, content: ChangeSetContent,
) -> ChangeSetContent:
    proof = read_proof(session, request_ref)
    if proof is None:
        return content
    if proof["effects"] != effect_projection(content):
        raise DocketError(
            code="field_evidence_effect_conflict",
            message="This patch changes the preserved request, not its evidence bookkeeping.",
            details={"authority_preserved": True, "category": "semantic_conflict",
                     "constraint": "exact_original_effect_inventory",
                     "next_action": "reconcile_source_interpretation"},
        )
    raw = mutation_input_json(content)
    actions = {action["change_id"]: action for group in GROUPS for action in raw[group]}
    for binding, statement_ref in zip(proof["bindings"], proof["statement_refs"], strict=True):
        for target in binding["targets"]:
            action = actions[target["change_id"]]
            action["basis_refs"] = list(dict.fromkeys([*action["basis_refs"], statement_ref]))
    sources = sorted({row["source_ref"] for row in proof["sources"]}.union(
        content.import_scope.source_refs if content.import_scope else [],
    ))
    effects = sorted({action["object_type"] for action in actions.values()})
    origin = next(iter(proof["originating_utterances"]))
    authority = StatementService(session).derive(origin, [StatementInput(
        statement_kind="operator_intent", subject_refs=sources, predicate="import_effect_authority",
        value_json={"authorized_effects": effects}, affected_fields=["import_scope"],
        interpretation_json={"compiler": "field_evidence", "proof_hash": sha256_json(proof)},
        interpreter_version="docket.field-evidence.v1",
    )])[0]
    raw["basis_refs"] = list(dict.fromkeys([
        *raw["basis_refs"], *proof["statement_refs"], authority.ref_id,
    ]))
    raw["import_scope"] = ImportScope(
        mode="operator_explicit", source_refs=sources,
        authorized_effects=cast(list[ImportEffect], effects),
        authority_statement_refs=[authority.ref_id],
        entry_coverage=content.import_scope.entry_coverage if content.import_scope else [],
        partition_key="assembled",
    ).model_dump(mode="json")
    return ChangeSetContent.model_validate(raw)


def verified_direct_actions(
    session: Session, *, request_ref: str | None, content: ChangeSetContent,
) -> set[str]:
    """Shared mutation-service guard. No client boolean can bypass import checks."""
    proof = read_proof(session, request_ref)
    if proof is None:
        return set()
    compiled = compile_field_evidence(session, request_ref=request_ref, content=content)
    if mutation_input_json(compiled) != mutation_input_json(content):
        raise _error("compiled_field_statements_in_exact_action_basis")
    # Ensure no source is laundered through a statement belonging to another
    # action/source. Direct source refs and statement refs use the same test.
    statements = {row.ref_id: row.source_ref for row in session.scalars(select(
        InterpretedStatement,
    ).where(InterpretedStatement.ref_id.in_(
        {ref for group in GROUPS for action in getattr(content, group)
         for ref in action.basis_refs},
    )))}
    supported: dict[str, set[str]] = {}
    for binding in _BINDINGS.validate_python(proof["bindings"]):
        for target in binding.targets:
            supported.setdefault(target.change_id, set()).add(binding.source_ref)
    for group in GROUPS:
        for action in getattr(content, group):
            if action.change_id not in proof["direct_action_ids"]:
                continue
            cited = _source_refs(mutation_input_json(action))
            cited.update(source for ref in action.basis_refs if (source := statements.get(ref)))
            if cited - supported.get(action.change_id, set()):
                raise _error("every_cited_source_has_field_binding", target={
                    "change_id": action.change_id, "field_path": ["basis_refs"],
                })
    return set(proof["direct_action_ids"])
