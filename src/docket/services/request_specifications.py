"""Record exact interpretation evidence separately from the mutable draft.

These proposals are a prerequisite to source-grounded repair, not its verifier.
No proposal changes or consumes authority, and no historical row is backfilled.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import (
    AttachmentEvidence,
    ChangeSet,
    ChangeSetRevision,
    OperatorUtterance,
    SemanticRequest,
    SemanticRequestSpecification,
    Source,
)
from docket.schemas.assembly import AssemblyAuthorityScopeInput
from docket.schemas.authority import mutation_input_json
from docket.schemas.request_specifications import RequestSpecificationProposal
from docket.services.request_adoption import read_adoption

_ENTRY_METADATA = {
    "statement_ref", "compiler_identifier", "compiler_version", "input_schema_version",
    "normalized_input_hash", "compilation_errors",
}


def read_request_proposal(
    session: Session, *, semantic_request_ref: str, version: int,
) -> RequestSpecificationProposal:
    """Read one exact immutable version; never silently substitute the latest."""
    row = session.get(SemanticRequestSpecification, (semantic_request_ref, version))
    if row is None:
        raise DocketError(
            code="request_specification_not_found",
            message="This request version was not recorded.",
        )
    try:
        proposal = RequestSpecificationProposal.model_validate(row.specification_json)
    except ValidationError as exc:
        raise DocketError(
            code="request_specification_invalid", message="The request specification is invalid.",
        ) from exc
    computed = sha256_json(mutation_input_json(proposal))
    if (
        row.schema_version != proposal.schema_version
        or row.interpretation_state != proposal.interpretation_state
        or computed != row.specification_hash
    ):
        raise DocketError(
            code="request_specification_integrity_mismatch",
            message="The stored request specification does not match its immutable digest.",
        )
    return proposal


def record_request_proposal(
    session: Session, *, changeset: ChangeSet, revision: ChangeSetRevision,
    request_boundary: AssemblyAuthorityScopeInput | None = None,
) -> SemanticRequestSpecification:
    request = session.scalar(select(SemanticRequest).where(
        SemanticRequest.ref_id == changeset.semantic_request_ref,
    ))
    if request is None:
        raise DocketError(code="semantic_request_not_found", message="The draft lost its request.")
    utterances = list(session.scalars(select(OperatorUtterance).where(
        OperatorUtterance.ref_id.in_(request.origin_utterance_refs),
    )))
    if set(request.origin_utterance_refs) != {row.ref_id for row in utterances}:
        raise DocketError(
            code="request_origin_evidence_missing", message="Original request evidence is missing.",
        )
    adopted = read_adoption(session, request)
    request_boundary = request_boundary or (adopted[1].assembly_boundary if adopted else None)
    boundary = (
        request_boundary.model_dump(mode="json", exclude_none=True)
        if request_boundary else (request.selected_option_binding or {}).get("scope")
    )
    entries = [
        {key: value for key, value in row.items() if key not in _ENTRY_METADATA}
        for row in changeset.normalized_entries_json
    ]
    source_refs = set((boundary or {}).get("source_refs", [])) | {
        row["evidence"]["source_ref"] for row in entries
    }
    sources = {row.ref_id: row for row in session.scalars(select(Source).where(
        Source.ref_id.in_(source_refs),
    ))}
    attachments = {row.ref_id: row for row in session.scalars(select(AttachmentEvidence).where(
        AttachmentEvidence.ref_id.in_(source_refs),
    ))}
    owned_ids = {
        change_id for owner in changeset.compiled_action_ownership_json
        for change_id in owner["change_ids"]
    }
    payload: dict[str, Any] = {
        "originating_utterances": [
            {"utterance_ref": row.ref_id, "content_hash": row.content_hash}
            for row in sorted(utterances, key=lambda row: row.ref_id)
        ],
        "source_bindings": [
            {
                "source_ref": ref,
                "source_manifest_hash": sources[ref].content_hash if ref in sources else None,
                "attachment_content_hash": (
                    attachments[ref].content_hash if ref in attachments else None
                ),
                "evidence_state": (
                    "attachment_recorded" if ref in attachments else
                    "source_recorded" if ref in sources else "missing"
                ),
            } for ref in sorted(source_refs)
        ],
        "assembly_boundary": boundary,
        "normalized_entries": entries,
        "direct_actions": [
            row for row in changeset.staged_actions_json or [] if row["change_id"] not in owned_ids
        ],
    }
    proposal = RequestSpecificationProposal.model_validate(payload)
    specification = mutation_input_json(proposal)
    row = SemanticRequestSpecification(
        semantic_request_ref=request.ref_id, version=revision.revision,
        change_set_revision=revision, schema_version=proposal.schema_version,
        interpretation_state=proposal.interpretation_state,
        specification_json=specification, specification_hash=sha256_json(specification),
    )
    session.add(row)
    return row
