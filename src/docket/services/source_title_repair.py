"""One source-grounded repair rule, not a general source interpretation oracle.

The selected Item title and all other original semantics stay fixed. Only a
duplicated Event title may be coalesced to that value, after checking the exact
retained source fragment and its original normalized statement. Callers must
compare *all* remaining effects, including provider effects, before accepting it.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import InterpretedStatement, OperatorUtterance
from docket.schemas.assembly import ScheduledOccurrenceEntry
from docket.schemas.authority import CanonicalEventCreate, ChangeSetContent, ItemCreate
from docket.schemas.request_specifications import RequestSpecificationProposal
from docket.services.attachment_evidence import AttachmentEvidenceService, AttachmentTextService


def _unproved(entry_id: str, constraint: str) -> DocketError:
    return DocketError(
        code="source_title_repair_unproved",
        message="The original selected title is not grounded in its retained source binding.",
        details={
            "category": "evidence_validation", "entry_id": entry_id,
            "field_path": ["title"], "constraint": constraint,
            "authority_preserved": True, "next_action": "read_attachment_text",
        },
    )


def coalesce_source_titles(
    session: Session, *, prior: ChangeSetContent, proposal: RequestSpecificationProposal,
) -> tuple[ChangeSetContent, list[dict[str, Any]]]:
    """Derive the only permitted title corrections without editing prior evidence."""
    scope = prior.import_scope
    if scope is None:
        return prior, []
    origin_hashes = {row.utterance_ref: row.content_hash for row in proposal.originating_utterances}
    utterances = list(session.scalars(select(OperatorUtterance).where(
        OperatorUtterance.ref_id.in_(origin_hashes),
    )))
    if {row.ref_id: row.content_hash for row in utterances} != origin_hashes:
        raise _unproved("request", "unchanged_original_utterance_evidence")
    origin_ids = {row.id for row in utterances}
    source_refs = {ref for row in utterances for ref in row.attachment_source_refs}
    selected_entries = {entry.import_entry_id: entry for entry in proposal.normalized_entries}
    source_bindings = {binding.source_ref: binding for binding in proposal.source_bindings}
    items = {change.change_id: change for change in prior.tracked_context_changes
             if isinstance(change, ItemCreate)}
    repaired = prior.model_copy(deep=True)
    events = {change.change_id: change for change in repaired.event_changes
              if isinstance(change, CanonicalEventCreate)}
    proof: list[dict[str, Any]] = []
    reader: AttachmentTextService | None = None
    for entry in scope.entry_coverage:
        if entry.calendar_representation != "canonical_event":
            continue
        item = items.get(entry.item_change_id)
        event = events.get(entry.calendar_change_id or "")
        if item is None or event is None:
            raise _unproved(entry.entry_id, "exact_original_item_event_pair")
        title = item.create_spec.title
        if event.create_spec.event_spec.title == title and event.create_spec.title == title:
            continue
        if event.create_spec.item_change_ids != [item.change_id]:
            raise _unproved(entry.entry_id, "event_represents_exact_selected_item")
        selected = selected_entries.get(entry.entry_id)
        if not isinstance(selected, ScheduledOccurrenceEntry) or selected.title != title:
            raise _unproved(entry.entry_id, "exact_recorded_request_entry")
        selected_value = selected.model_dump(mode="json", exclude={"evidence"}, exclude_none=True)
        basis = set(item.basis_refs).intersection(event.basis_refs)
        statements = [
            row for row in session.scalars(select(InterpretedStatement).where(
                InterpretedStatement.ref_id.in_(basis),
                InterpretedStatement.utterance_id.in_(origin_ids),
            ))
            if row.interpretation_json.get("import_entry_id") == entry.entry_id
            and row.statement_kind == "normalized_source_entry"
            and row.predicate == "normalized_temporal_entry"
            and isinstance(row.value_json, dict)
            and row.value_json == selected_value
            and row.source_ref == selected.evidence.source_ref
            and row.source_fragment_hash == selected.evidence.source_fragment_hash
            and row.source_fragment_locator == selected.evidence.source_fragment_locator
            and row.extractor_identifier == selected.evidence.extractor_identifier
            and row.extractor_version == selected.evidence.extractor_version
            and row.source_ref in source_refs
            and row.source_ref in scope.source_refs
            and row.source_ref in item.create_spec.source_refs
        ]
        if len(statements) != 1:
            raise _unproved(entry.entry_id, "exact_original_source_statement")
        statement = statements[0]
        if reader is None:
            settings = get_settings()
            reader = AttachmentTextService(AttachmentEvidenceService(
                session, encryption_key=settings.attachment_encryption_key(),
                encryption_key_ref=settings.attachment_encryption_key_ref,
                max_attachment_bytes=settings.attachment_max_bytes,
                max_total_bytes=settings.attachment_total_max_bytes,
            ))
        assert statement.source_ref is not None
        fragment = reader.verify_fragment(
            source_ref=statement.source_ref, locator=statement.source_fragment_locator or {},
            fragment_hash=statement.source_fragment_hash or "",
            extractor_identifier=statement.extractor_identifier or "",
            extractor_version=statement.extractor_version or "",
        )
        source_binding = source_bindings.get(statement.source_ref)
        if source_binding is None or (
            source_binding.attachment_content_hash != fragment.attachment_content_hash
        ):
            raise _unproved(entry.entry_id, "unchanged_request_attachment_content")
        # Exact text only. No fuzzy match, paraphrase, added course prefix or
        # model-supplied assertion of equivalence can satisfy this rule.
        match = re.search(r"(?<!\w)" + re.escape(title) + r"(?!\w)", fragment.text)
        if not title.strip() or match is None:
            raise _unproved(entry.entry_id, "selected_title_is_literal_source_text")
        offset = match.start()
        # Deep-copy typed models, not a null-stripped JSON round trip. Explicit
        # null patches elsewhere must keep their original field-presence semantics.
        event.create_spec.title = title
        event.create_spec.event_spec.title = title
        proof.append({
            "rule": "coalesce_selected_source_title_v1", "entry_id": entry.entry_id,
            "item_change_id": item.change_id, "event_change_id": event.change_id,
            "statement_ref": statement.ref_id, "evidence": fragment.binding(),
            "request_entry_hash": sha256_json(selected.model_dump(mode="json", exclude_none=True)),
            "title_character_start": fragment.locator.text_character_start + offset,
            "title_character_end": fragment.locator.text_character_start + offset + len(title),
            "title_sha256": hashlib.sha256(title.encode("utf-8")).hexdigest(),
        })
    return repaired, proof
