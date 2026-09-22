"""Trusted, bounded continuation context; never a model-selected source allowlist."""

from __future__ import annotations

import base64
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.errors import DocketError
from docket.models import (
    AttachmentEvidence,
    ClarificationReply,
    IntentSession,
    OperatorProjection,
    OperatorUtterance,
    PersistedSemanticOption,
    ProjectionDelivery,
)
from docket.schemas.authority import ChangeSetContent
from docket.services.clarification_labels import calendar_choice, duration_minutes
from docket.services.semantic_scope import pinned_semantic_projection


def retained_attachments(session: Session, utterance: OperatorUtterance) -> dict[str, Any]:
    from docket.services.field_evidence import _reader

    reader = _reader(session)
    prior = [row for row in evidence_origins(session, utterance) if row.ref_id != utterance.ref_id]
    summaries = [summary for row in prior for summary in reader.summaries(row)]
    if len(summaries) + len(utterance.attachment_source_refs) > 10:
        raise DocketError(
            code="clarification_attachment_limit",
            message="The request has too many attachments; narrow the source scope.",
        )
    attachments = []
    total = sum(
        session.scalars(
            select(AttachmentEvidence.byte_size).where(
                AttachmentEvidence.ref_id.in_(utterance.attachment_source_refs),
            )
        )
    )
    for summary in summaries:
        value = dict(summary)
        if summary["ingest_state"] == "available":
            data = reader.plaintext(str(summary["ref"]))
            total += len(data)
            if total > reader.max_total_bytes:
                raise DocketError(
                    code="clarification_attachment_limit",
                    message="Retained request evidence exceeds its byte budget.",
                )
            value["plaintext_base64"] = base64.b64encode(data).decode("ascii")
        attachments.append(value)
    return {"retained_attachments": attachments, "retained_attachment_summaries": summaries}


def _options(session: Session, projection_ref: str) -> list[PersistedSemanticOption]:
    options = list(
        session.scalars(
            select(PersistedSemanticOption)
            .where(
                PersistedSemanticOption.projection_ref == projection_ref,
            )
            .order_by(PersistedSemanticOption.created_at, PersistedSemanticOption.ref_id)
        )
    )
    projection = session.scalar(
        select(OperatorProjection).where(
            OperatorProjection.ref_id == projection_ref,
        )
    )
    assert projection is not None
    by_ref = {option.ref_id: option for option in options}
    return [by_ref[row["option_ref"]] for row in projection.semantic_content["render"]["options"]]


def _match(text: str, options: list[PersistedSemanticOption]) -> PersistedSemanticOption | None:
    """Only exact choices and unambiguous numeric durations; not general intent NLP."""
    answer = text.strip().casefold().rstrip(".")
    duration = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)(?: each)?", answer)
    minutes = None
    if duration:
        minutes = float(duration[1]) * (60 if duration[2].startswith("h") else 1)
    matches = []
    for index, option in enumerate(options, 1):
        content = option.compilation_template_json
        calendar = calendar_choice(content)
        if answer in {
            str(index),
            f"option {index}",
            option.option_id.casefold(),
            option.visible_text.casefold().rstrip("."),
        } or (
            minutes is not None
            and calendar is not None
            and minutes
            == duration_minutes(content["event_changes"][0]["create_spec"]["event_spec"])
        ):
            matches.append(option)
    return matches[0] if len(matches) == 1 else None


def evidence_origins(session: Session, utterance: OperatorUtterance) -> list[OperatorUtterance]:
    """Only immutable, authenticated same-conversation linkage can extend evidence."""
    reply = session.get(ClarificationReply, utterance.ref_id)
    refs = reply.evidence_utterance_refs if reply is not None else []
    if utterance.selected_option_ref and utterance.projection_ref:
        projection = session.scalar(
            select(OperatorProjection).where(
                OperatorProjection.ref_id == utterance.projection_ref,
            )
        )
        if projection is not None and projection.operator_ref == utterance.actor_ref:
            refs = [ref for ref in projection.basis_refs if ref.startswith("utt_")]
            refs = sorted(
                set(refs).union(
                    *(
                        row.evidence_utterance_refs
                        for row in session.scalars(
                            select(ClarificationReply).where(
                                ClarificationReply.utterance_ref.in_(refs),
                            )
                        )
                    )
                )
            )
    rows = list(
        session.scalars(
            select(OperatorUtterance).where(
                OperatorUtterance.ref_id.in_([utterance.ref_id, *refs]),
                OperatorUtterance.actor_ref == utterance.actor_ref,
                OperatorUtterance.conversation_ref == utterance.conversation_ref,
            )
        )
    )
    if len(rows) != len(set([utterance.ref_id, *refs])):
        raise DocketError(
            code="clarification_evidence_mismatch",
            message="The retained clarification evidence needs reconciliation.",
        )
    return rows


def bind_reply(session: Session, utterance: OperatorUtterance) -> ClarificationReply | None:
    """Run once at trusted capture, after storing the verbatim message.

    Explicit replies address a delivered prompt. An unthreaded exact choice may
    answer one pending prompt only when its source is the immediately preceding
    Operator message. General channel history is not request identity.
    """
    existing = session.get(ClarificationReply, utterance.ref_id)
    if existing is not None or utterance.utterance_kind != "typed_message":
        return existing
    query = (
        select(OperatorProjection)
        .join(
            ProjectionDelivery,
            ProjectionDelivery.projection_id == OperatorProjection.id,
        )
        .join(IntentSession, IntentSession.ref_id == OperatorProjection.intent_session_ref)
        .where(
            OperatorProjection.projection_kind == "clarification",
            OperatorProjection.operator_ref == utterance.actor_ref,
            ProjectionDelivery.destination_ref == utterance.conversation_ref,
            ProjectionDelivery.status == "delivered",
            ProjectionDelivery.transport == "discord",
            ProjectionDelivery.last_error_code.is_(None),
            OperatorProjection.created_at <= utterance.recorded_at,
        )
    )
    explicit = utterance.reply_to_source_ref is not None
    if explicit:
        query = query.where(
            ProjectionDelivery.external_message_ref == utterance.reply_to_source_ref,
        )
    else:
        query = query.where(IntentSession.semantic_state == "needs_clarification")
    projections = list(
        session.scalars(
            query.order_by(OperatorProjection.created_at.desc()).limit(5),
        )
    )
    # Superseded projections are never guessed from an unthreaded answer.
    projections = [
        p
        for p in projections
        if not session.scalar(
            select(OperatorProjection.id)
            .where(
                OperatorProjection.supersedes_projection_ref == p.ref_id,
            )
            .limit(1)
        )
    ]
    if len(projections) != 1:
        return None
    projection = projections[0]
    intent = session.scalar(
        select(IntentSession)
        .where(
            IntentSession.ref_id == projection.intent_session_ref,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if intent is None:
        return None
    options = _options(session, projection.ref_id)
    from docket.services.semantic_options import require_distinct_choices

    try:
        require_distinct_choices(projection)
    except DocketError:
        # Old indistinguishable numbered choices cannot establish reply authority.
        return None
    selected = _match(utterance.verbatim_text, options)
    if not explicit:
        previous = session.scalar(
            select(OperatorUtterance)
            .where(
                OperatorUtterance.conversation_ref == utterance.conversation_ref,
                OperatorUtterance.actor_ref == utterance.actor_ref,
                OperatorUtterance.ref_id != utterance.ref_id,
                (OperatorUtterance.said_at < utterance.said_at)
                | (
                    (OperatorUtterance.said_at == utterance.said_at)
                    & (OperatorUtterance.recorded_at < utterance.recorded_at)
                ),
            )
            .order_by(OperatorUtterance.said_at.desc(), OperatorUtterance.recorded_at.desc())
            .limit(1)
        )
        if selected is None or previous is None or previous.ref_id not in projection.basis_refs:
            return None
    refs: set[str] = set()
    for origin in session.scalars(
        select(OperatorUtterance).where(
            OperatorUtterance.ref_id.in_(projection.basis_refs),
            OperatorUtterance.actor_ref == utterance.actor_ref,
            OperatorUtterance.conversation_ref == utterance.conversation_ref,
        )
    ):
        refs.update(row.ref_id for row in evidence_origins(session, origin))
    if not refs or len(refs) > 25:
        raise DocketError(
            code="clarification_evidence_limit",
            message="Clarification context needs an explicit bounded reconciliation.",
        )
    reply = ClarificationReply(
        utterance_ref=utterance.ref_id,
        projection_ref=projection.ref_id,
        intent_session_ref=intent.ref_id,
        selected_option_ref=selected.ref_id if selected is not None else None,
        evidence_utterance_refs=sorted(refs),
    )
    session.add(reply)
    session.flush()
    return reply


def reply_context(session: Session, utterance: OperatorUtterance) -> dict[str, Any] | None:
    reply = session.get(ClarificationReply, utterance.ref_id)
    if reply is None:
        return None
    return {
        "kind": "clarification",
        "primary_ref": reply.intent_session_ref,
        "intent_session_ref": reply.intent_session_ref,
        "projection_ref": reply.projection_ref,
        "selected_option_ref": reply.selected_option_ref,
        "evidence_utterance_refs": reply.evidence_utterance_refs,
        "case_refs": [],
        "case_revision_refs": [],
        "trusted_context_refs": [],
    }


def validate_reply_effects(
    session: Session, intent: IntentSession, content: ChangeSetContent
) -> list[dict[str, Any]]:
    if not intent.semantic_request_ref:
        return []
    from docket.models import SemanticRequest
    from docket.services.semantic_options import complete_selection_provenance

    request = session.scalar(
        select(SemanticRequest).where(
            SemanticRequest.ref_id == intent.semantic_request_ref,
        )
    )
    if request is None:
        return []
    for reply in session.scalars(
        select(ClarificationReply).where(
            ClarificationReply.utterance_ref.in_(request.origin_utterance_refs),
            ClarificationReply.selected_option_ref.is_not(None),
        )
    ):
        option = session.scalar(
            select(PersistedSemanticOption).where(
                PersistedSemanticOption.ref_id == reply.selected_option_ref,
            )
        )
        assert option is not None
        expected = complete_selection_provenance(
            option.compilation_template_json,
            reply.utterance_ref,
        )
        # Supporting-field compilation adds evidence bookkeeping, not effects.
        actual = content
        from docket.services.field_evidence import read_proof

        if read_proof(session, request.ref_id) is not None:
            actual = content.model_copy(update={"import_scope": None})
        if pinned_semantic_projection(actual, []) != pinned_semantic_projection(expected, []):
            return [
                {
                    "code": "clarification_effect_mismatch",
                    "details": {
                        "category": "semantic_conflict",
                        "authority_preserved": True,
                        "next_action": "restore_selected_effects_or_request_clarification",
                    },
                }
            ]
    return []
