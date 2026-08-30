from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from docket.domain.errors import DocketError
from docket.domain.public_refs import parse_public_ref
from docket.models import (
    Affiliation,
    AgentResponse,
    AttachmentEvidence,
    AttentionCase,
    AttentionCaseRevision,
    AuditEvent,
    BriefEntry,
    CalendarLane,
    CanonicalEvent,
    CaseItem,
    ChangeSet,
    Conflict,
    ContextPacket,
    ConversationalToolTrace,
    DailyBrief,
    Decision,
    DeferredIngress,
    DrainBarrier,
    Entity,
    Fact,
    GatewayLifetime,
    GmailSource,
    IdentityBinding,
    IdentityHandle,
    IntentSession,
    IntentTurn,
    Interaction,
    InterpretedStatement,
    Item,
    LaneRoutingDecision,
    Operation,
    OperatorProjection,
    OperatorUtterance,
    PersistedSemanticOption,
    Preference,
    ProviderAccount,
    Relationship,
    ReminderPlan,
    RuntimeLogEntry,
    SemanticRequest,
    SemanticRequestAttempt,
    Source,
    Task,
    TemporalBinding,
    TemporalCalendarProjection,
    ToolInvocation,
    TriageRun,
)
from docket.services.source_identities import (
    associated_sender_emails,
    gmail_sender_identity,
    sender_handles_for_email,
)

DEFAULT_PAGE_SIZE = 25
HARD_PAGE_SIZE = 100
DEFAULT_OUTPUT_BYTES = 16 * 1024
AUDIT_OUTPUT_BYTES = 64 * 1024
MAX_TEXT_CHUNK_BYTES = 32 * 1024

_PREFIX_MODELS: dict[str, tuple[str, type[Any], str]] = {
    "utt": ("operator_utterance", OperatorUtterance, "recorded_at"),
    "rsp": ("agent_response", AgentResponse, "generated_at"),
    "stm": ("interpreted_statement", InterpretedStatement, "created_at"),
    "dec": ("decision", Decision, "created_at"),
    "ses": ("intent_session", IntentSession, "created_at"),
    "turn": ("intent_turn", IntentTurn, "created_at"),
    "chg": ("changeset", ChangeSet, "created_at"),
    "conf": ("conflict", Conflict, "created_at"),
    "ent": ("entity", Entity, "created_at"),
    "idn": ("identity_handle", IdentityHandle, "created_at"),
    "aff": ("affiliation", Affiliation, "created_at"),
    "rel": ("relationship", Relationship, "created_at"),
    "fact": ("fact", Fact, "created_at"),
    "int": ("interaction", Interaction, "created_at"),
    "lane": ("calendar_lane", CalendarLane, "created_at"),
    "pref": ("preference", Preference, "created_at"),
    "route": ("lane_routing_decision", LaneRoutingDecision, "decided_at"),
    "evt": ("canonical_event", CanonicalEvent, "created_at"),
    "src": ("source", Source, "created_at"),
    "brief": ("daily_brief", DailyBrief, "created_at"),
    "tri": ("triage_run", TriageRun, "started_at"),
    "ctx": ("context_packet", ContextPacket, "created_at"),
    "case": ("attention_case", AttentionCase, "created_at"),
    "caserev": ("attention_case_revision", AttentionCaseRevision, "created_at"),
    "item": ("item", Item, "created_at"),
    "task": ("task", Task, "created_at"),
    "time": ("temporal_binding", TemporalBinding, "created_at"),
    "tproj": ("temporal_calendar_projection", TemporalCalendarProjection, "created_at"),
    "rem": ("reminder_plan", ReminderPlan, "created_at"),
    "citem": ("case_item", CaseItem, "created_at"),
    "bentry": ("brief_entry", BriefEntry, "created_at"),
    "acct": ("provider_account", ProviderAccount, "created_at"),
    "op": ("operation", Operation, "created_at"),
    "call": ("tool_invocation", ToolInvocation, "started_at"),
    "aud": ("audit_event", AuditEvent, "created_at"),
    "log": ("runtime_log_entry", RuntimeLogEntry, "occurred_at"),
    "proj": ("operator_projection", OperatorProjection, "created_at"),
    "opt": ("persisted_semantic_option", PersistedSemanticOption, "created_at"),
    "trace": ("conversational_tool_trace", ConversationalToolTrace, "created_at"),
    "sreq": ("semantic_request", SemanticRequest, "created_at"),
    "sattempt": ("semantic_request_attempt", SemanticRequestAttempt, "started_at"),
    "gwy": ("gateway_lifetime", GatewayLifetime, "started_at"),
    "drain": ("drain_barrier", DrainBarrier, "requested_at"),
    "ing": ("deferred_ingress", DeferredIngress, "created_at"),
}

_SEARCH_SPECS: list[tuple[str, type[Any], str]] = [
    *_PREFIX_MODELS.values(),
]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _text_chunk(value: str, offset: int, limit: int) -> dict[str, Any]:
    encoded = value.encode("utf-8")
    start = min(offset, len(encoded))
    while start < len(encoded):
        end = min(start + limit, len(encoded))
        while end > start:
            try:
                chunk = encoded[start:end].decode("utf-8")
                return {
                    "verbatim_text_chunk": chunk,
                    "text_offset": start,
                    "text_next_offset": end if end < len(encoded) else None,
                    "text_total_bytes": len(encoded),
                    "text_truncated": end < len(encoded),
                }
            except UnicodeDecodeError:
                end -= 1
        start += 1
    else:
        end = len(encoded)
    return {
        "verbatim_text_chunk": "",
        "text_offset": start,
        "text_next_offset": None,
        "text_total_bytes": len(encoded),
        "text_truncated": False,
    }


def _cursor_encode(occurred_at: datetime, ref_id: str) -> str:
    raw = json.dumps(
        {"occurred_at": _iso(occurred_at), "ref": ref_id},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_decode(value: str) -> tuple[datetime, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode())
        occurred_at = datetime.fromisoformat(str(decoded["occurred_at"]))
        ref_id = str(decoded["ref"])
        parse_public_ref(ref_id)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DocketError(
            code="invalid_history_cursor",
            message="History cursor is malformed.",
        ) from exc
    return occurred_at, ref_id


class HistoryService:
    """Bounded internal inspection over public provenance and operational refs."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _summary(self, object_type: str, item: Any) -> dict[str, Any]:
        base: dict[str, Any] = {"ref": item.ref_id, "type": object_type}
        if isinstance(item, OperatorUtterance):
            return {
                **base,
                "actor_ref": item.actor_ref,
                "transport": item.transport,
                "conversation_ref": item.conversation_ref,
                "source_message_ref": item.source_message_ref,
                "said_at": _iso(item.said_at),
                "recorded_at": _iso(item.recorded_at),
                "content_hash": item.content_hash,
            }
        if isinstance(item, AgentResponse):
            return {
                **base,
                "conversation_ref": item.conversation_ref,
                "responds_to_utterance_refs": item.responds_to_utterance_refs,
                "basis_refs": item.basis_refs,
                "tool_call_refs": item.tool_call_refs,
                "generation_state": item.generation_state,
                "delivery_state": item.delivery_state,
                "generated_at": _iso(item.generated_at),
                "delivered_at": _iso(item.delivered_at),
                "projection_ref": item.projection_ref,
            }
        if isinstance(item, InterpretedStatement):
            return {
                **base,
                "statement_kind": item.statement_kind,
                "subject_refs": item.subject_refs,
                "predicate": item.predicate,
                "affected_fields": item.affected_fields,
                "effective_from": item.effective_from.isoformat() if item.effective_from else None,
                "effective_to": item.effective_to.isoformat() if item.effective_to else None,
                "interpreter_version": item.interpreter_version,
                "created_at": _iso(item.created_at),
            }
        if isinstance(item, Decision):
            return {
                **base,
                "decision_kind": item.decision_kind,
                "actor_ref": item.actor_ref,
                "basis_refs": item.basis_refs,
                "document_ref": item.document_ref,
                "frozen_artifact_hash": item.frozen_artifact_hash,
                "authorized_scope": item.authorized_scope,
                "architecture_authority": item.architecture_authority,
                "implementation_authority": item.implementation_authority,
                "created_at": _iso(item.created_at),
            }
        if isinstance(item, ToolInvocation):
            return {
                **base,
                "tool_name": item.tool_name,
                "tool_contract_version": item.tool_contract_version,
                "tool_contract_hash": item.tool_contract_hash,
                "caller_profile": item.caller_profile,
                "actor_ref": item.actor_ref,
                "utterance_refs": item.utterance_refs,
                "transport_state": item.transport_state,
                "domain_state": item.domain_state,
                "result_refs": item.result_refs,
                "result_disposition": item.result_disposition,
                "error_code": item.error_code,
                "started_at": _iso(item.started_at),
                "completed_at": _iso(item.completed_at),
            }
        if isinstance(item, IntentSession):
            return {
                **base,
                "conversation_ref": item.conversation_ref,
                "source_utterance_ref": item.source_utterance_ref,
                "case_refs": item.case_refs,
                "case_revision_refs": item.case_revision_refs,
                "brief_ref": item.brief_ref,
                "trusted_context_refs": item.trusted_context_refs,
                "semantic_state": item.semantic_state,
                "commit_state": item.commit_state,
                "semantic_request_ref": item.semantic_request_ref,
                "version": item.version,
                "committed_changeset_ref": item.committed_changeset_ref,
                "created_at": _iso(item.created_at),
                "updated_at": _iso(item.updated_at),
            }
        if isinstance(item, IntentTurn):
            return {
                **base,
                "intent_session_ref": item.intent_session_ref,
                "utterance_ref": item.utterance_ref,
                "statement_refs": item.statement_refs,
                "context_refs": item.context_refs,
                "tool_call_refs": item.tool_call_refs,
                "agent_response_ref": item.agent_response_ref,
                "resulting_semantic_refs": item.resulting_semantic_refs,
                "response_disposition": item.response_disposition,
                "created_at": _iso(item.created_at),
            }
        if isinstance(item, SemanticRequest):
            return {
                **base,
                "intent_session_ref": item.intent_session_ref,
                "authority_scope_hash": item.authority_scope_hash,
                "current_precondition_hash": item.current_precondition_hash,
                "origin_utterance_refs": item.origin_utterance_refs,
                "authority_availability": item.authority_availability,
                "commit_state": item.commit_state,
                "current_case_revision_ref": item.current_case_revision_ref,
                "committed_changeset_ref": item.committed_changeset_ref,
                "created_at": _iso(item.created_at),
                "updated_at": _iso(item.updated_at),
            }
        if isinstance(item, SemanticRequestAttempt):
            return {
                **base,
                "semantic_request_ref": item.semantic_request_ref,
                "attempt_number": item.attempt_number,
                "authority_scope_hash": item.authority_scope_hash,
                "precondition_hash": item.precondition_hash,
                "case_revision_ref": item.case_revision_ref,
                "change_set_ref": item.change_set_ref,
                "tool_call_ref": item.tool_call_ref,
                "gateway_instance_ref": item.gateway_instance_ref,
                "state": item.state,
                "error_code": item.error_code,
                "started_at": _iso(item.started_at),
                "completed_at": _iso(item.completed_at),
            }
        if isinstance(item, ChangeSet):
            return {
                **base,
                "intent_session_ref": item.intent_session_ref,
                "basis_refs": item.basis_refs,
                "state": item.state,
                "version": item.version,
                "current_revision": item.current_revision,
                "created_at": _iso(item.created_at),
                "committed_at": _iso(item.committed_at),
            }
        if isinstance(item, Conflict):
            return {
                **base,
                "subject_refs": item.subject_refs,
                "affected_fields": item.affected_fields,
                "prior_statement_refs": item.prior_statement_refs,
                "incoming_statement_refs": item.incoming_statement_refs,
                "status": item.status,
                "version": item.version,
                "resolution_decision_ref": item.resolution_decision_ref,
                "created_at": _iso(item.created_at),
                "resolved_at": _iso(item.resolved_at),
            }
        if isinstance(item, TriageRun):
            return {
                **base,
                "status": item.status,
                "claimed_by": item.claimed_by,
                "source_refs": item.source_refs,
                "context_refs": item.context_refs,
                "contract_version": item.contract_version,
                "contract_hash": item.contract_hash,
                "started_at": _iso(item.started_at),
                "completed_at": _iso(item.completed_at),
                "error_code": item.error_code,
            }
        if isinstance(item, ContextPacket):
            return {
                **base,
                "triage_run_ref": item.triage_run_ref,
                "source_ref": item.source_ref,
                "serialized_bytes": item.serialized_bytes,
                "contract_version": item.contract_version,
                "contract_hash": item.contract_hash,
                "created_at": _iso(item.created_at),
            }
        if isinstance(item, AttentionCase):
            return {
                **base,
                "title": item.title,
                "status": item.status,
                "priority": item.priority,
                "semantic_classes": item.semantic_classes,
                "entity_refs": item.entity_refs,
                "source_refs": item.source_refs,
                "latest_revision": item.latest_revision,
                "resolution_decision_ref": item.resolution_decision_ref,
                "version": item.version,
                "created_at": _iso(item.created_at),
                "resolved_at": _iso(item.resolved_at),
            }
        if isinstance(item, AttentionCaseRevision):
            return {
                **base,
                "case_ref": item.case_ref,
                "revision": item.revision,
                "semantic_classes": item.semantic_classes,
                "case_item_refs": item.case_item_refs,
                "source_refs": item.source_refs,
                "admission_rule_ref": item.admission_rule_ref,
                "admission_basis_refs": item.admission_basis_refs,
                "required_case_item_refs": item.required_case_item_refs,
                "canonical_consequence_classes": item.canonical_consequence_classes,
                "content_hash": item.content_hash,
                "created_at": _iso(item.created_at),
            }
        if isinstance(item, CaseItem):
            case_ref = self.session.scalar(
                select(AttentionCase.ref_id).where(AttentionCase.id == item.attention_case_id)
            )
            return {
                **base,
                "case_ref": case_ref,
                "item_key": item.item_key,
                "item_type": item.item_type,
                "resolution_role": item.resolution_role,
                "status": item.status,
                "candidate_refs": item.candidate_refs,
                "basis_refs": item.basis_refs,
                "source_refs": item.source_refs,
                "version": item.version,
                "created_at": _iso(item.created_at),
            }
        if isinstance(item, Item):
            return {
                **base,
                "title": item.title,
                "kind": item.kind,
                "context_entity_refs": item.context_entity_refs,
                "parent_item_ref": item.parent_item_ref,
                "canonical_status": item.canonical_status,
                "basis_refs": item.basis_refs,
                "decision_refs": item.decision_refs,
                "source_refs": item.source_refs,
                "created_by_changeset_ref": item.created_by_changeset_ref,
                "version": item.version,
                "created_at": _iso(item.created_at),
            }
        if isinstance(item, Task):
            return {
                **base,
                "item_ref": item.item_ref,
                "title": item.title,
                "task_state": item.task_state,
                "priority": item.priority,
                "canonical_status": item.canonical_status,
                "completed_at": _iso(item.completed_at),
                "basis_refs": item.basis_refs,
                "source_refs": item.source_refs,
                "created_by_changeset_ref": item.created_by_changeset_ref,
                "version": item.version,
                "created_at": _iso(item.created_at),
            }
        if isinstance(item, TemporalBinding):
            return {
                **base,
                "subject_ref": item.subject_ref,
                "role": item.role,
                "binding_key": item.binding_key,
                "temporal_value": item.temporal_value,
                "canonical_status": item.canonical_status,
                "basis_refs": item.basis_refs,
                "decision_refs": item.decision_refs,
                "source_refs": item.source_refs,
                "created_by_changeset_ref": item.created_by_changeset_ref,
                "version": item.version,
                "created_at": _iso(item.created_at),
            }
        if isinstance(item, BriefEntry):
            return {
                **base,
                "source_ref": item.source_ref,
                "semantic_classes": item.semantic_classes,
                "title": item.title,
                "disposition": item.disposition,
                "reason": item.reason,
                "included_brief_ref": item.included_brief_ref,
                "created_at": _iso(item.created_at),
            }
        if isinstance(item, AuditEvent):
            return {
                **base,
                "event_type": item.event_type,
                "primary_ref": item.primary_ref,
                "affected_refs": item.affected_refs,
                "basis_refs": item.basis_refs,
                "actor": {"type": item.actor_type, "id": item.actor_id},
                "occurred_at": _iso(item.created_at),
            }
        if isinstance(item, RuntimeLogEntry):
            return {
                **base,
                "severity": item.severity,
                "component": item.component,
                "event_code": item.event_code,
                "message": item.message,
                "related_refs": item.related_refs,
                "occurred_at": _iso(item.occurred_at),
            }
        if isinstance(item, Entity):
            return {
                **base,
                "entity_kind": item.entity_kind,
                "display_name": item.display_name,
                "canonical_status": item.canonical_status,
                "basis_refs": item.basis_refs,
                "decision_refs": item.decision_refs,
                "source_refs": item.source_refs,
                "created_by_changeset_ref": item.created_by_changeset_ref,
                "version": item.version,
                "created_at": _iso(item.created_at),
            }
        if isinstance(item, IdentityHandle):
            entity_ref = (
                self.session.scalar(select(Entity.ref_id).where(Entity.id == item.entity_id))
                if item.entity_id is not None
                else None
            )
            identity_summary = {
                **base,
                "handle_type": item.handle_type,
                "value": item.value,
                "status": item.status,
                "binding_rule": item.binding_rule,
                "entity_ref": entity_ref,
                "binding_basis_refs": item.binding_basis_refs,
                "basis_refs": item.basis_refs,
                "created_by_changeset_ref": item.created_by_changeset_ref,
                "version": item.version,
                "created_at": _iso(item.created_at),
            }
            if item.handle_type == "sender_label":
                identity_summary["associated_emails"] = associated_sender_emails(self.session, item)
            elif item.handle_type == "email":
                identity_summary["sender_handles"] = sender_handles_for_email(self.session, item)
            return identity_summary
        if isinstance(item, Affiliation):
            subject_ref = self.session.scalar(
                select(Entity.ref_id).where(Entity.id == item.subject_entity_id)
            )
            organization_ref = self.session.scalar(
                select(Entity.ref_id).where(Entity.id == item.organization_entity_id)
            )
            return {
                **base,
                "subject_ref": subject_ref,
                "organization_ref": organization_ref,
                "role": item.role,
                "domain": item.domain,
                "valid_from": item.valid_from.isoformat() if item.valid_from else None,
                "valid_to": item.valid_to.isoformat() if item.valid_to else None,
                "status": item.status,
                "basis_refs": item.basis_refs,
                "created_by_changeset_ref": item.created_by_changeset_ref,
                "version": item.version,
                "created_at": _iso(item.created_at),
            }
        if isinstance(item, Relationship):
            subject_ref = self.session.scalar(
                select(Entity.ref_id).where(Entity.id == item.subject_entity_id)
            )
            object_ref = self.session.scalar(
                select(Entity.ref_id).where(Entity.id == item.object_entity_id)
            )
            return {
                **base,
                "subject_ref": subject_ref,
                "object_ref": object_ref,
                "relationship_type": item.relationship_type,
                "context": item.context,
                "valid_from": item.valid_from.isoformat() if item.valid_from else None,
                "valid_to": item.valid_to.isoformat() if item.valid_to else None,
                "status": item.status,
                "basis_refs": item.basis_refs,
                "created_by_changeset_ref": item.created_by_changeset_ref,
                "version": item.version,
                "created_at": _iso(item.created_at),
            }
        if isinstance(item, Fact):
            return {
                **base,
                "subject_ref": item.subject_ref,
                "predicate": item.predicate,
                "valid_from": item.valid_from.isoformat() if item.valid_from else None,
                "valid_to": item.valid_to.isoformat() if item.valid_to else None,
                "status": item.status,
                "basis_refs": item.basis_refs,
                "created_by_changeset_ref": item.created_by_changeset_ref,
                "version": item.version,
                "created_at": _iso(item.created_at),
            }
        if isinstance(item, Interaction):
            return {
                **base,
                "interaction_type": item.interaction_type,
                "occurred_at": _iso(item.occurred_at),
                "summary": item.summary,
                "event_ref": item.event_ref,
                "organization_refs": item.organization_refs,
                "status": item.status,
                "basis_refs": item.basis_refs,
                "created_by_changeset_ref": item.created_by_changeset_ref,
                "version": item.version,
                "created_at": _iso(item.created_at),
            }
        if isinstance(item, CanonicalEvent):
            return {
                **base,
                "title": item.title,
                "status": item.status,
                "lane_ref": item.lane_ref,
                "routing_decision_ref": item.routing_decision_ref,
                "authority": item.authority,
                "basis_refs": item.basis_refs,
                "decision_refs": item.decision_refs,
                "source_refs": item.source_refs,
                "created_by_changeset_ref": item.created_by_changeset_ref,
                "version": item.version,
                "created_at": _iso(item.created_at),
            }
        if isinstance(item, DailyBrief):
            return {
                **base,
                "brief_kind": item.brief_kind,
                "local_date": item.local_date.isoformat(),
                "status": item.status,
                "interval_start": _iso(item.interval_start),
                "interval_end": _iso(item.interval_end),
                "case_refs": item.case_refs,
                "basis_refs": item.basis_refs,
                "projection_revision": item.projection_revision,
                "version": item.version,
                "created_at": _iso(item.created_at),
                "published_at": _iso(item.published_at),
            }
        if isinstance(item, GmailSource):
            return {
                **base,
                "provider": item.provider,
                "source_version": item.source_version,
                "source_fingerprint": item.source_fingerprint,
                "sender_identity": gmail_sender_identity(self.session, item, materialize=False),
                "status": item.status,
                "received_at": _iso(item.received_at),
                "created_at": _iso(item.created_at),
            }
        if isinstance(item, Source):
            summary = {
                **base,
                "source_kind": item.source_kind,
                "external_ref": item.external_ref,
                "observed_at": _iso(item.observed_at),
                "content_hash": item.content_hash,
                "created_at": _iso(item.created_at),
            }
            attachment = self.session.scalar(
                select(AttachmentEvidence).where(AttachmentEvidence.ref_id == item.ref_id)
            )
            if attachment is not None:
                summary["attachment"] = {
                    "transport": attachment.transport,
                    "transport_attachment_ref": attachment.transport_attachment_ref,
                    "source_message_ref": attachment.source_message_ref,
                    "operator_utterance_ref": attachment.operator_utterance_ref,
                    "filename": attachment.filename,
                    "media_type": attachment.media_type,
                    "byte_size": attachment.byte_size,
                    "content_hash": attachment.content_hash,
                    "received_at": _iso(attachment.received_at),
                    "recorded_at": _iso(attachment.recorded_at),
                    "ingest_state": attachment.ingest_state,
                    "retention_disposition": attachment.retention_disposition,
                    "derived_content_refs": attachment.derived_content_refs,
                    "source_revision": 1,
                    "untrusted_content": True,
                }
            return summary
        if isinstance(item, ProviderAccount):
            return {
                **base,
                "provider": item.provider,
                "external_account_id": item.external_account_id,
                "display_name": item.display_name,
                "email_address": item.email_address,
                "capabilities": item.capabilities,
                "enabled": item.enabled,
                "created_at": _iso(item.created_at),
            }
        if isinstance(item, Operation):
            return {
                **base,
                "operation_type": item.operation_type,
                "status": item.status,
                "originating_changeset_ref": item.originating_changeset_ref,
                "basis_refs": item.basis_refs,
                "canonical_target_refs": item.canonical_target_refs,
                "attempt_count": item.attempt_count,
                "last_error_code": item.last_error_code,
                "created_at": _iso(item.created_at),
                "updated_at": _iso(item.updated_at),
            }
        if isinstance(item, CalendarLane):
            return {
                **base,
                "lane": item.lane,
                "display_name": item.display_name,
                "operator_policy_text": item.operator_policy_text,
                "metadata_json": item.metadata_json,
                "enabled": item.enabled,
                "priority": item.priority,
                "status": item.status,
                "basis_refs": item.basis_refs,
                "source_refs": item.source_refs,
                "created_by_changeset_ref": item.created_by_changeset_ref,
                "version": item.version,
                "created_at": _iso(item.created_at),
            }
        if isinstance(item, Preference):
            return {
                **base,
                "preference_key": item.preference_key,
                "policy_kind": item.policy_kind,
                "target_type": item.target_type,
                "target_ref": item.target_ref,
                "semantic_class": item.semantic_class,
                "policy_text": item.policy_text,
                "policy_json": item.policy_json,
                "scope_json": item.scope_json,
                "priority": item.priority,
                "status": item.status,
                "basis_refs": item.basis_refs,
                "created_by_changeset_ref": item.created_by_changeset_ref,
                "version": item.version,
                "created_at": _iso(item.created_at),
            }
        if isinstance(item, LaneRoutingDecision):
            return {
                **base,
                "lane_ref": item.lane_ref,
                "event_ref": item.event_ref,
                "organization_ref": item.organization_ref,
                "recurring_identity": item.recurring_identity,
                "decision_kind": item.decision_kind,
                "operator_confirmed": item.operator_confirmed,
                "status": item.status,
                "basis_refs": item.basis_refs,
                "created_by_changeset_ref": item.created_by_changeset_ref,
                "decided_at": _iso(item.decided_at),
            }
        raise TypeError(f"Unsupported history object: {type(item).__name__}")

    @staticmethod
    def _timestamp(item: Any, field_name: str) -> datetime:
        value = getattr(item, field_name)
        if not isinstance(value, datetime):
            raise TypeError(f"History timestamp field {field_name} is not a datetime")
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _related(summary: dict[str, Any], related_ref: str) -> bool:
        for value in summary.values():
            if value == related_ref:
                return True
            if isinstance(value, list) and related_ref in value:
                return True
        return False

    def get_entry(
        self,
        ref_id: str,
        *,
        view: str = "summary",
        text_offset: int = 0,
        text_limit: int = MAX_TEXT_CHUNK_BYTES,
    ) -> dict[str, Any]:
        try:
            prefix, _payload = parse_public_ref(ref_id)
        except ValueError as exc:
            raise DocketError(
                code="invalid_public_ref",
                message="History lookup requires a typed public reference.",
            ) from exc
        spec = _PREFIX_MODELS.get(prefix)
        if spec is None:
            raise DocketError(
                code="history_type_not_available",
                message="This public-reference type is not available in the current migration.",
            )
        object_type, model, _time_field = spec
        item = (
            self.session.scalar(select(GmailSource).where(GmailSource.ref_id == ref_id))
            if prefix == "src"
            else self.session.scalar(select(model).where(model.ref_id == ref_id))
        )
        if item is None and prefix == "src":
            item = self.session.scalar(select(Source).where(Source.ref_id == ref_id))
        if item is None:
            raise DocketError(code="history_entry_not_found", message="Public reference not found.")
        entry = self._summary(object_type, item)
        if isinstance(item, InterpretedStatement):
            entry["utterance_ref"] = self.session.scalar(
                select(OperatorUtterance.ref_id).where(OperatorUtterance.id == item.utterance_id)
            )
        if view == "audit":
            if isinstance(item, OperatorUtterance | AgentResponse):
                entry.update(
                    _text_chunk(
                        item.verbatim_text,
                        max(text_offset, 0),
                        min(max(text_limit, 1), MAX_TEXT_CHUNK_BYTES),
                    )
                )
            if isinstance(item, InterpretedStatement):
                entry["value_json"] = item.value_json
                entry["interpretation_json"] = item.interpretation_json
            elif isinstance(item, Decision):
                entry["payload_json"] = item.payload_json
            elif isinstance(item, RuntimeLogEntry):
                entry["metadata_json"] = item.metadata_json
            elif isinstance(item, AuditEvent):
                entry["metadata_json"] = item.data
            elif isinstance(item, IntentSession):
                entry["resolved_intent_json"] = item.resolved_intent_json
                entry["blocking_clarifications"] = item.blocking_clarifications
            elif isinstance(item, ChangeSet):
                entry["expected_versions"] = item.expected_versions
                entry["registry_changes"] = item.registry_changes
                entry["preference_changes"] = item.preference_changes
                entry["lane_changes"] = item.lane_changes
                entry["event_changes"] = item.event_changes
                entry["resolution_changes"] = item.resolution_changes
                entry["provider_intents"] = item.provider_intents
                entry["validation_errors"] = item.validation_errors
            elif isinstance(item, Conflict):
                entry["conflicting_effects_json"] = item.conflicting_effects_json
            elif isinstance(item, ContextPacket):
                entry["trusted_context_json"] = item.trusted_context_json
            elif isinstance(item, AttentionCase):
                entry["summary"] = item.summary
            elif isinstance(item, AttentionCaseRevision):
                entry["title"] = item.title
                entry["summary"] = item.summary
            elif isinstance(item, CaseItem):
                entry["payload_json"] = item.payload_json
            elif isinstance(item, BriefEntry):
                entry["summary"] = item.summary
            elif isinstance(item, Fact):
                entry["value_json"] = item.value_json
                entry["decision_refs"] = item.decision_refs
                entry["source_refs"] = item.source_refs
            elif isinstance(item, Source):
                entry["metadata_json"] = item.metadata_json
            elif isinstance(item, IdentityHandle):
                bindings = list(
                    self.session.scalars(
                        select(IdentityBinding)
                        .where(IdentityBinding.identity_handle_id == item.id)
                        .order_by(IdentityBinding.valid_from)
                    )
                )
                entry["binding_history"] = [
                    {
                        "entity_ref": self.session.scalar(
                            select(Entity.ref_id).where(Entity.id == binding.entity_id)
                        ),
                        "binding_rule": binding.binding_rule,
                        "status": binding.status,
                        "valid_from": _iso(binding.valid_from),
                        "valid_to": _iso(binding.valid_to),
                        "basis_refs": binding.basis_refs,
                        "created_by_changeset_ref": binding.created_by_changeset_ref,
                    }
                    for binding in bindings
                ]
                if item.handle_type == "sender_label":
                    entry["associated_email_history"] = associated_sender_emails(
                        self.session,
                        item,
                        include_inactive=True,
                    )
        envelope = {"ok": True, "ref": ref_id, "object_type": object_type, "entry": entry}
        budget = AUDIT_OUTPUT_BYTES if view == "audit" else DEFAULT_OUTPUT_BYTES
        if len(json.dumps(envelope, separators=(",", ":")).encode()) > budget:
            raise DocketError(
                code="history_output_budget_exceeded",
                message="History entry exceeds its serialized UTF-8 output budget.",
            )
        return envelope

    def search(
        self,
        *,
        object_type: str | None = None,
        ref_id: str | None = None,
        conversation_ref: str | None = None,
        related_ref: str | None = None,
        tool_name: str | None = None,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        limit = min(max(limit, 1), HARD_PAGE_SIZE)
        known_types = {spec[0] for spec in _SEARCH_SPECS}
        if object_type is not None and object_type not in known_types:
            raise DocketError(
                code="invalid_history_object_type",
                message="History object_type is not available in the current migration.",
            )
        if ref_id is not None:
            exact = self.get_entry(ref_id)
            return {
                "ok": True,
                "items": [exact["entry"]],
                "count": 1,
                "total_if_known": 1,
                "truncated": False,
                "cursor": None,
            }
        if related_ref is not None:
            try:
                parse_public_ref(related_ref)
            except ValueError as exc:
                raise DocketError(
                    code="invalid_related_ref",
                    message="related_ref must be a typed public reference.",
                ) from exc
        cursor_value = _cursor_decode(cursor) if cursor else None
        candidates: list[tuple[datetime, str, dict[str, Any]]] = []
        for candidate_type, model, time_field in _SEARCH_SPECS:
            if object_type is not None and object_type != candidate_type:
                continue
            if conversation_ref is not None and model not in {
                OperatorUtterance,
                AgentResponse,
                IntentSession,
            }:
                continue
            if tool_name is not None and model is not ToolInvocation:
                continue
            time_column = getattr(model, time_field)
            statement: Select[Any] = select(model)
            if conversation_ref is not None:
                statement = statement.where(model.conversation_ref == conversation_ref)
            if tool_name is not None:
                statement = statement.where(ToolInvocation.tool_name == tool_name)
            if occurred_from is not None:
                statement = statement.where(time_column >= occurred_from)
            if occurred_to is not None:
                statement = statement.where(time_column < occurred_to)
            if cursor_value is not None:
                cursor_time, cursor_ref = cursor_value
                statement = statement.where(
                    (time_column < cursor_time)
                    | ((time_column == cursor_time) & (model.ref_id < cursor_ref))
                )
            scan_limit = 4 * HARD_PAGE_SIZE + 1 if related_ref is not None else limit + 1
            statement = statement.order_by(time_column.desc(), model.ref_id.desc()).limit(
                scan_limit
            )
            for item in self.session.scalars(statement):
                summary = self._summary(candidate_type, item)
                if related_ref is not None and not self._related(summary, related_ref):
                    continue
                candidates.append((self._timestamp(item, time_field), item.ref_id, summary))

        candidates.sort(key=lambda candidate: (candidate[0], candidate[1]), reverse=True)
        selected = candidates[: limit + 1]
        truncated = len(selected) > limit
        selected = selected[:limit]
        items = [candidate[2] for candidate in selected]
        next_cursor = (
            _cursor_encode(selected[-1][0], selected[-1][1]) if truncated and selected else None
        )
        envelope: dict[str, Any] = {
            "ok": True,
            "items": items,
            "count": len(items),
            "total_if_known": None,
            "truncated": truncated,
            "cursor": next_cursor,
        }
        while (
            items
            and len(json.dumps(envelope, separators=(",", ":")).encode()) > DEFAULT_OUTPUT_BYTES
        ):
            items.pop()
            envelope["count"] = len(items)
            envelope["truncated"] = True
            envelope["cursor"] = (
                _cursor_encode(
                    next(candidate[0] for candidate in selected if candidate[2] is items[-1]),
                    items[-1]["ref"],
                )
                if items
                else cursor
            )
        return envelope

    def conversation(
        self,
        conversation_ref: str,
        *,
        view: str = "summary",
        limit: int = HARD_PAGE_SIZE,
    ) -> dict[str, Any]:
        limit = min(max(limit, 1), HARD_PAGE_SIZE)
        utterances = list(
            self.session.scalars(
                select(OperatorUtterance)
                .where(OperatorUtterance.conversation_ref == conversation_ref)
                .order_by(OperatorUtterance.said_at)
                .limit(limit + 1)
            )
        )
        responses = list(
            self.session.scalars(
                select(AgentResponse)
                .where(AgentResponse.conversation_ref == conversation_ref)
                .order_by(AgentResponse.generated_at)
                .limit(limit + 1)
            )
        )
        turns: list[tuple[datetime, dict[str, Any]]] = []
        for utterance in utterances:
            entry = self._summary("operator_utterance", utterance)
            entry["role"] = "operator"
            if view == "audit":
                entry.update(_text_chunk(utterance.verbatim_text, 0, MAX_TEXT_CHUNK_BYTES))
            turns.append((self._timestamp(utterance, "said_at"), entry))
        for response in responses:
            entry = self._summary("agent_response", response)
            entry["role"] = "agent"
            if view == "audit":
                entry.update(_text_chunk(response.verbatim_text, 0, MAX_TEXT_CHUNK_BYTES))
            turns.append((self._timestamp(response, "generated_at"), entry))
        turns.sort(key=lambda turn: turn[0])
        truncated = len(turns) > limit
        entries = [turn[1] for turn in turns[:limit]]
        envelope = {
            "ok": True,
            "conversation_ref": conversation_ref,
            "items": entries,
            "count": len(entries),
            "total_if_known": None,
            "truncated": truncated,
            "cursor": None,
        }
        budget = AUDIT_OUTPUT_BYTES if view == "audit" else DEFAULT_OUTPUT_BYTES
        while entries and len(json.dumps(envelope, separators=(",", ":")).encode()) > budget:
            entries.pop()
            envelope["count"] = len(entries)
            envelope["truncated"] = True
        return envelope
