from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import Settings, get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import (
    AttentionCase,
    AttentionCaseRevision,
    AuditEvent,
    CalendarLane,
    CaseItem,
    CaseSource,
    ContextPacket,
    Entity,
    IdentityHandle,
    OutboxEvent,
    Preference,
    QueueItem,
    SourceItem,
    TriageBriefEntry,
    TriageRun,
)
from docket.models.base import utc_now
from docket.providers.google.gmail import GmailReadProvider
from docket.schemas.intelligence import TriageAnalysisInput
from docket.services.network import NetworkQueryService
from docket.services.policies import PreferenceMatcher
from docket.services.provenance_refs import ProvenanceRefService
from docket.services.source_identities import gmail_sender_identity
from docket.services.triage import TriageService
from docket.tool_contracts import CONTRACT_VERSION, contract_hash

_CASE_CLASSES = frozenset({"action_request", "event_invitation", "deadline_or_required_response"})


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _sender_identity_refs(sender_resolution: object) -> set[str]:
    if not isinstance(sender_resolution, dict):
        return set()
    refs = sender_resolution.get("identity_refs")
    if isinstance(refs, list):
        return {item for item in refs if isinstance(item, str)}
    exact_ref = sender_resolution.get("identity_ref")
    return {exact_ref} if isinstance(exact_ref, str) else set()


def _truncate_utf8(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


class IntelligenceService:
    """Compile restricted cron intelligence into cases and brief entries only."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: GmailReadProvider,
        settings: Settings | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.provider = provider
        self.settings = settings or get_settings()
        self.legacy_claims = TriageService(
            session_factory,
            provider,
            self.settings.model_copy(update={"gmail_claim_batch_size": 1}),
        )

    def _active_window(self, instant: datetime) -> bool:
        local_hour = _aware(instant).astimezone(ZoneInfo(self.settings.timezone)).hour
        start = self.settings.waking_window_start_hour
        end = self.settings.waking_window_end_hour
        return start <= local_hour < end if start < end else local_hour >= start or local_hour < end

    @staticmethod
    def _compact_untrusted(source: dict[str, Any]) -> dict[str, Any]:
        body, truncated = _truncate_utf8(str(source.get("body_text", "")), 8192)
        attachments = []
        for attachment in list(source.get("attachments", []))[:10]:
            if not isinstance(attachment, dict):
                continue
            attachments.append(
                {
                    "filename": str(attachment.get("filename", ""))[:255],
                    "mime_type": str(attachment.get("mime_type", ""))[:255],
                    "size": int(attachment.get("size", 0)),
                }
            )
        return {
            "trust": "untrusted_provider_content",
            "instruction_policy": source.get("instruction_policy"),
            "sender": str(source.get("sender", ""))[:1024],
            "subject": str(source.get("subject", ""))[:1024],
            "body_text": body,
            "body_truncated": truncated,
            "label_ids": [str(value)[:128] for value in source.get("label_ids", [])][:25],
            "attachments": attachments,
            "message_id": source.get("message_id"),
            "thread_id": source.get("thread_id"),
            "source_version": source.get("source_version"),
        }

    @staticmethod
    def _fit_trusted_context(context: dict[str, Any]) -> tuple[dict[str, Any], int]:
        compact = dict(context)
        for key in (
            "related_open_cases",
            "calendar_lanes",
            "sender_context",
            "routing_precedent",
        ):
            value = compact.get(key)
            if isinstance(value, list):
                compact[key] = value[:10]
        encoded = json.dumps(compact, separators=(",", ":"), default=str).encode("utf-8")
        while len(encoded) > 8 * 1024:
            list_values = [
                (key, value) for key, value in compact.items() if isinstance(value, list) and value
            ]
            if not list_values:
                raise DocketError(
                    code="context_packet_too_large",
                    message="Trusted triage context cannot fit its bounded projection.",
                )
            _key, longest = max(list_values, key=lambda item: len(item[1]))
            longest.pop()
            encoded = json.dumps(compact, separators=(",", ":"), default=str).encode("utf-8")
        return compact, len(encoded)

    def _trusted_context(self, session: Session, source: SourceItem) -> dict[str, Any]:
        sender_identity = gmail_sender_identity(session, source, materialize=True)
        identity_ref = (
            sender_identity.get("identity_ref") if isinstance(sender_identity, dict) else None
        )
        handle = (
            session.scalar(select(IdentityHandle).where(IdentityHandle.ref_id == identity_ref))
            if isinstance(identity_ref, str)
            else None
        )
        sender_entity = (
            session.get(Entity, handle.entity_id)
            if handle is not None and handle.entity_id is not None and handle.status == "bound"
            else None
        )
        sender_handles = (
            sender_identity.get("sender_handles", []) if isinstance(sender_identity, dict) else []
        )
        identity_refs = {
            *([handle.ref_id] if handle is not None else []),
            *[
                item["identity_ref"]
                for item in sender_handles
                if isinstance(item, dict) and isinstance(item.get("identity_ref"), str)
            ],
        }
        sender_context: list[dict[str, Any]] = []
        if (
            sender_entity is not None
            and sender_entity.registration_state == "registered"
            and sender_entity.entity_class == "person"
        ):
            sender_context.append(NetworkQueryService(session).person_context(sender_entity.ref_id))
        cases = [
            {
                "ref": item.ref_id,
                "title": item.title,
                "status": item.status,
                "entity_refs": item.entity_refs,
                "source_refs": item.source_refs,
            }
            for item in session.scalars(
                select(AttentionCase)
                .where(AttentionCase.status == "open")
                .order_by(AttentionCase.last_observed_at.desc())
                .limit(25)
            )
            if source.ref_id in item.source_refs
            or (sender_entity is not None and sender_entity.ref_id in item.entity_refs)
        ]
        lanes = [
            {
                "ref": item.ref_id,
                "name": item.display_name,
                "lane": item.lane,
                "status": item.status,
            }
            for item in session.scalars(
                select(CalendarLane)
                .where(CalendarLane.status == "active")
                .order_by(CalendarLane.display_name)
                .limit(25)
            )
        ]
        preferences = PreferenceMatcher(session).applicable(
            entity_refs={sender_entity.ref_id} if sender_entity is not None else set(),
            identity_refs=identity_refs,
            source_refs={source.ref_id},
        )
        return {
            "trust": "trusted_docket_context",
            "source_ref": source.ref_id,
            "sender_resolution": {
                "identity_ref": handle.ref_id if handle is not None else None,
                "identity_refs": sorted(identity_refs),
                "handle_type": (
                    sender_identity.get("handle_type")
                    if isinstance(sender_identity, dict)
                    else None
                ),
                "value": (
                    sender_identity.get("value") if isinstance(sender_identity, dict) else None
                ),
                "state": handle.status if handle is not None else "unknown",
                "entity_ref": sender_entity.ref_id if sender_entity is not None else None,
                "sender_handles": sender_handles,
                "resolution_rule": handle.binding_rule if handle is not None else None,
                "basis_refs": [source.ref_id],
            },
            "sender_context": sender_context,
            "explicit_preferences": [
                {
                    "ref": item.ref_id,
                    "policy_kind": item.policy_kind,
                    "target_type": item.target_type,
                    "target_ref": item.target_ref,
                    "policy_text": item.policy_text,
                    "policy_json": item.policy_json,
                    "scope_json": item.scope_json,
                    "priority": item.priority,
                }
                for item in preferences[:10]
            ],
            "calendar_lanes": lanes,
            "routing_precedent": [],
            "related_open_cases": cases,
        }

    @staticmethod
    def _serialized_bytes(value: dict[str, Any]) -> int:
        return len(json.dumps(value, separators=(",", ":"), default=str).encode("utf-8"))

    @classmethod
    def _fit_context_result(cls, result: dict[str, Any]) -> dict[str, Any]:
        source = result.get("untrusted_source")
        if not isinstance(source, dict):
            return result
        original_body = str(source.get("body_text", ""))
        body_budget = len(original_body.encode("utf-8"))
        while cls._serialized_bytes(result) > 16384 and body_budget > 0:
            body_budget //= 2
            source["body_text"], _ = _truncate_utf8(original_body, body_budget)
            source["body_truncated"] = True
        if cls._serialized_bytes(result) > 16384:
            raise DocketError(
                code="triage_output_budget_exceeded",
                message="The compact triage context cannot fit its output byte budget.",
            )
        return result

    def get_triage_context(self) -> dict[str, Any]:
        claim = self.legacy_claims.claim_batch(claimed_by="docket-intelligence")
        sources = claim.get("sources", [])
        with self.session_factory.begin() as session:
            triage_run = TriageRun(
                claimed_by="docket-intelligence",
                contract_version=CONTRACT_VERSION,
                contract_hash=contract_hash("triage"),
                stats_json={"claimed_source_count": len(sources)},
            )
            session.add(triage_run)
            session.flush()
            if not sources:
                triage_run.status = "completed"
                triage_run.completed_at = utc_now()
                return {
                    "ok": True,
                    "ref": triage_run.ref_id,
                    "state": "no_sources",
                    "summary": "No staged triage source is available.",
                    "affected_refs": [triage_run.ref_id],
                    "next": None,
                    "warnings": [],
                }
            run_ref = triage_run.ref_id
            claimed_source_id = uuid.UUID(str(sources[0]["source_id"]))
            claimed_source = session.get(SourceItem, claimed_source_id)
            if claimed_source is None:
                raise DocketError(
                    code="source_item_not_found", message="Claimed source disappeared."
                )
            triage_run.source_refs = [claimed_source.ref_id]
            claim_token = uuid.UUID(str(claim["claim_token"]))
        try:
            raw = self.legacy_claims.read_claimed_source(
                source_id=claimed_source_id,
                claim_token=claim_token,
            )
        except Exception as exc:
            with self.session_factory.begin() as session:
                failed_run = session.scalar(select(TriageRun).where(TriageRun.ref_id == run_ref))
                if failed_run is not None:
                    failed_run.status = "failed"
                    failed_run.completed_at = utc_now()
                    failed_run.error_code = type(exc).__name__[:128]
            raise
        if raw.get("triage_required") is False:
            with self.session_factory.begin() as session:
                completed_run = session.scalar(select(TriageRun).where(TriageRun.ref_id == run_ref))
                if completed_run is not None:
                    completed_run.status = "completed"
                    completed_run.completed_at = utc_now()
            return {
                "ok": True,
                "ref": run_ref,
                "state": "source_already_terminal",
                "summary": "The current provider source is already terminal.",
                "affected_refs": [run_ref],
                "next": None,
                "warnings": [],
            }
        source_id = uuid.UUID(str(raw["source_id"]))
        with self.session_factory.begin() as session:
            persisted_run = session.scalar(select(TriageRun).where(TriageRun.ref_id == run_ref))
            source = session.get(SourceItem, source_id)
            if persisted_run is None or source is None:
                raise DocketError(
                    code="source_item_not_found", message="Claimed source disappeared."
                )
            persisted_run.source_refs = [source.ref_id]
            source_ref = source.ref_id
            trusted, trusted_bytes = self._fit_trusted_context(
                self._trusted_context(session, source)
            )
            packet = ContextPacket(
                triage_run_id=persisted_run.id,
                triage_run_ref=persisted_run.ref_id,
                source_ref=source.ref_id,
                trusted_context_json=trusted,
                serialized_bytes=trusted_bytes,
                contract_version=persisted_run.contract_version,
                contract_hash=persisted_run.contract_hash,
            )
            session.add(packet)
            session.flush()
            persisted_run.context_refs = [packet.ref_id]
            context_ref = packet.ref_id
        result: dict[str, Any] = {
            "ok": True,
            "ref": context_ref,
            "state": "ready",
            "summary": "Bounded trusted context and untrusted source are ready.",
            "triage_run_ref": run_ref,
            "source_ref": source_ref,
            "claim_token": str(claim_token),
            "trusted_context": trusted,
            "untrusted_source": self._compact_untrusted(raw),
            "affected_refs": [run_ref, context_ref, source_ref],
            "next": "docket_submit_triage_analysis",
            "warnings": [],
        }
        return self._fit_context_result(result)

    @staticmethod
    def _situation_key(source: SourceItem) -> str:
        return sha256_json(
            {
                "provider": source.provider,
                "account_id": str(source.account_id),
                "conversation": source.external_parent_id or source.external_object_id,
            }
        )

    @staticmethod
    def _validate_analysis_classes(request: TriageAnalysisInput) -> str:
        classes = set(request.semantic_classes)
        if "noise" in classes and len(classes) > 1:
            raise DocketError(
                code="invalid_semantic_class_combination",
                message="noise cannot coexist with another semantic class.",
            )
        if classes & _CASE_CLASSES:
            return "case"
        if classes == {"noise"}:
            return "suppress"
        return "brief"

    def _case_revision(
        self,
        session: Session,
        case: AttentionCase,
    ) -> AttentionCaseRevision:
        items = list(
            session.scalars(
                select(CaseItem)
                .where(CaseItem.attention_case_id == case.id)
                .order_by(CaseItem.created_at, CaseItem.ref_id)
            )
        )
        content = {
            "case_ref": case.ref_id,
            "revision": case.latest_revision,
            "title": case.title,
            "summary": case.summary,
            "semantic_classes": case.semantic_classes,
            "item_refs": [item.ref_id for item in items],
            "source_refs": case.source_refs,
        }
        revision = AttentionCaseRevision(
            attention_case_id=case.id,
            case_ref=case.ref_id,
            revision=case.latest_revision,
            title=case.title,
            summary=case.summary,
            semantic_classes=case.semantic_classes,
            item_refs=content["item_refs"],
            source_refs=case.source_refs,
            content_hash=sha256_json(content),
        )
        session.add(revision)
        session.flush()
        return revision

    def _project_active_case(
        self,
        session: Session,
        case: AttentionCase,
        revision: AttentionCaseRevision,
        source: SourceItem,
    ) -> None:
        queue_item = (
            session.get(QueueItem, case.queue_item_id) if case.queue_item_id is not None else None
        )
        if queue_item is None:
            queue_item = QueueItem(
                primary_source_item_id=source.id,
                deduplication_key=f"attention_case:{case.ref_id}",
                material_fingerprint=revision.content_hash,
                category="attention_case",
                title=case.title,
                summary=(f"{case.summary}\n\nReply with context and a decision.\n\n{case.ref_id}"),
                status="pending",
                priority=case.priority,
                presentation="action_required",
                received_at=case.first_observed_at,
                attention_case_ref=case.ref_id,
                attention_case_revision_ref=revision.ref_id,
            )
            session.add(queue_item)
            session.flush()
            case.queue_item_id = queue_item.id
        else:
            queue_item.material_fingerprint = revision.content_hash
            queue_item.title = case.title
            queue_item.summary = (
                f"{case.summary}\n\nReply with context and a decision.\n\n{case.ref_id}"
            )
            queue_item.status = "pending"
            queue_item.priority = case.priority
            queue_item.presentation = "action_required"
            queue_item.resolved_at = None
            queue_item.resolution_code = None
            queue_item.resolution_note = None
            queue_item.attention_case_revision_ref = revision.ref_id
            queue_item.version += 1
        session.add(
            OutboxEvent(
                event_type="discord.projection.requested",
                aggregate_type="queue_item",
                aggregate_id=queue_item.id,
                deduplication_key=(f"discord_projection:{queue_item.id}:case-{revision.ref_id}"),
                payload={"queue_item_id": str(queue_item.id)},
                status="pending",
            )
        )

    def submit_analysis(self, request: TriageAnalysisInput) -> dict[str, Any]:
        disposition = self._validate_analysis_classes(request)
        now = utc_now()
        with self.session_factory.begin() as session:
            run = session.scalar(
                select(TriageRun).where(TriageRun.ref_id == request.triage_run_ref)
            )
            packet = session.scalar(
                select(ContextPacket).where(ContextPacket.ref_id == request.context_ref)
            )
            source = session.scalar(
                select(SourceItem).where(SourceItem.ref_id == request.source_ref)
            )
            if (
                run is None
                or packet is None
                or source is None
                or packet.triage_run_id != run.id
                or packet.source_ref != source.ref_id
                or run.status != "running"
            ):
                raise DocketError(
                    code="triage_context_binding_invalid",
                    message="Triage analysis does not match an active bounded context.",
                )
            try:
                claim_token = uuid.UUID(request.claim_token)
            except ValueError as exc:
                raise DocketError(
                    code="triage_claim_invalid", message="Claim token is malformed."
                ) from exc
            if (
                source.status != "claimed"
                or source.claim_token != claim_token
                or source.claimed_until is None
                or _aware(source.claimed_until) <= now
            ):
                raise DocketError(
                    code="triage_claim_invalid",
                    message="Source is no longer held by this TriageRun.",
                )
            ProvenanceRefService(session).require_all(request.entity_candidate_refs)
            for item in request.case_items:
                ProvenanceRefService(session).require_all(item.candidate_refs)

            sender_resolution = packet.trusted_context_json.get("sender_resolution", {})
            identity_refs = _sender_identity_refs(sender_resolution)
            sender_entity_ref = (
                sender_resolution.get("entity_ref") if isinstance(sender_resolution, dict) else None
            )
            suppressions = PreferenceMatcher(session).applicable(
                entity_refs={
                    *request.entity_candidate_refs,
                    *([sender_entity_ref] if isinstance(sender_entity_ref, str) else []),
                },
                identity_refs=identity_refs,
                source_refs={source.ref_id},
                semantic_classes=set(request.semantic_classes),
                policy_kind="suppression",
            )
            suppression = next(
                (
                    item
                    for item in suppressions
                    if item.policy_json.get("disposition") == "suppress"
                ),
                None,
            )
            if suppression is not None:
                disposition = "suppress"
            if disposition == "case" and not request.case_items:
                raise DocketError(
                    code="attention_case_items_required",
                    message="Actionable analysis requires one or more typed CaseItems.",
                )

            affected_refs = [run.ref_id, packet.ref_id, source.ref_id]
            case_ref: str | None = None
            case_revision_ref: str | None = None
            if disposition == "case":
                situation_key = self._situation_key(source)
                case = session.scalar(
                    select(AttentionCase).where(AttentionCase.situation_key == situation_key)
                )
                observed_at = source.received_at or source.created_at
                if case is None:
                    case = AttentionCase(
                        situation_key=situation_key,
                        title=request.title,
                        summary=request.summary,
                        status="open",
                        priority=request.priority,
                        semantic_classes=list(request.semantic_classes),
                        entity_refs=list(request.entity_candidate_refs),
                        source_refs=[source.ref_id],
                        first_observed_at=observed_at,
                        last_observed_at=observed_at,
                    )
                    session.add(case)
                    session.flush()
                else:
                    case.title = request.title
                    case.summary = request.summary
                    case.priority = request.priority
                    case.semantic_classes = list(
                        dict.fromkeys([*case.semantic_classes, *request.semantic_classes])
                    )
                    case.entity_refs = list(
                        dict.fromkeys([*case.entity_refs, *request.entity_candidate_refs])
                    )
                    case.source_refs = list(dict.fromkeys([*case.source_refs, source.ref_id]))
                    case.last_observed_at = max(_aware(case.last_observed_at), _aware(observed_at))
                    case.latest_revision += 1
                    case.version += 1
                if session.get(CaseSource, (case.id, source.ref_id)) is None:
                    session.add(CaseSource(attention_case_id=case.id, source_ref=source.ref_id))
                existing_by_key = {
                    item.item_key: item
                    for item in session.scalars(
                        select(CaseItem).where(CaseItem.attention_case_id == case.id)
                    )
                }
                for item_input in request.case_items:
                    case_item = existing_by_key.get(item_input.item_key)
                    if case_item is None:
                        case_item = CaseItem(
                            attention_case_id=case.id,
                            item_key=item_input.item_key,
                            item_type=item_input.item_type,
                            payload_json=item_input.payload,
                            candidate_refs=list(item_input.candidate_refs),
                            basis_refs=[source.ref_id, packet.ref_id],
                            source_refs=[source.ref_id],
                        )
                        session.add(case_item)
                        session.flush()
                    else:
                        case_item.item_type = item_input.item_type
                        case_item.payload_json = item_input.payload
                        case_item.candidate_refs = list(item_input.candidate_refs)
                        case_item.basis_refs = list(
                            dict.fromkeys([*case_item.basis_refs, source.ref_id, packet.ref_id])
                        )
                        case_item.source_refs = list(
                            dict.fromkeys([*case_item.source_refs, source.ref_id])
                        )
                        case_item.status = "open"
                        case_item.version += 1
                    affected_refs.append(case_item.ref_id)
                revision = self._case_revision(session, case)
                case_ref = case.ref_id
                case_revision_ref = revision.ref_id
                affected_refs.extend([case.ref_id, revision.ref_id])
                if self._active_window(now):
                    self._project_active_case(session, case, revision, source)
            else:
                entry = TriageBriefEntry(
                    triage_run_id=run.id,
                    source_ref=source.ref_id,
                    semantic_classes=list(request.semantic_classes),
                    title=request.title,
                    summary=request.summary,
                    disposition="suppress" if disposition == "suppress" else "include",
                    reason=(
                        suppression.ref_id
                        if suppression is not None
                        else "noise"
                        if disposition == "suppress"
                        else None
                    ),
                )
                session.add(entry)
                session.flush()
                affected_refs.append(entry.ref_id)

            source.status = "classified" if disposition != "suppress" else "ignored"
            source.classification = {
                "schema_version": 3,
                "triage_run_ref": run.ref_id,
                "context_ref": packet.ref_id,
                "semantic_classes": list(request.semantic_classes),
                "disposition": disposition,
                "case_ref": case_ref,
                "case_revision_ref": case_revision_ref,
                "title": request.title,
                "summary": request.summary,
                "explanation": request.explanation,
                "preference_ref": suppression.ref_id if suppression is not None else None,
            }
            source.claim_token = None
            source.claimed_by = None
            source.claimed_until = None
            run.status = "completed"
            run.completed_at = now
            run.stats_json = {
                **run.stats_json,
                "disposition": disposition,
                "case_item_count": len(request.case_items),
            }
            session.add(
                AuditEvent(
                    event_type="triage.analysis_compiled",
                    entity_type="triage_run",
                    entity_id=run.id,
                    actor_type="docket_intelligence",
                    actor_id=None,
                    primary_ref=run.ref_id,
                    affected_refs=affected_refs,
                    basis_refs=[
                        source.ref_id,
                        packet.ref_id,
                        *([suppression.ref_id] if suppression is not None else []),
                    ],
                    data={
                        "semantic_classes": list(request.semantic_classes),
                        "disposition": disposition,
                    },
                )
            )
            return {
                "ok": True,
                "ref": case_ref or affected_refs[-1],
                "state": "open" if case_ref is not None else disposition,
                "summary": (
                    "AttentionCase created or updated without canonical mutation."
                    if case_ref is not None
                    else "Brief intelligence recorded without canonical mutation."
                ),
                "affected_refs": affected_refs,
                "basis_refs": [
                    source.ref_id,
                    packet.ref_id,
                    *([suppression.ref_id] if suppression is not None else []),
                ],
                "next": None,
                "warnings": [],
                "case_revision_ref": case_revision_ref,
            }

    def apply_existing_suppression(
        self,
        *,
        triage_run_ref: str,
        context_ref: str,
        source_ref: str,
        claim_token: str,
        preference_ref: str,
        semantic_classes: Sequence[str],
    ) -> dict[str, Any]:
        now = utc_now()
        with self.session_factory.begin() as session:
            run = session.scalar(select(TriageRun).where(TriageRun.ref_id == triage_run_ref))
            packet = session.scalar(
                select(ContextPacket).where(ContextPacket.ref_id == context_ref)
            )
            source = session.scalar(select(SourceItem).where(SourceItem.ref_id == source_ref))
            preference = session.scalar(
                select(Preference).where(Preference.ref_id == preference_ref)
            )
            try:
                parsed_claim = uuid.UUID(claim_token)
            except ValueError as exc:
                raise DocketError(
                    code="triage_claim_invalid", message="Claim token is malformed."
                ) from exc
            if (
                run is None
                or packet is None
                or source is None
                or preference is None
                or packet.triage_run_id != run.id
                or packet.source_ref != source.ref_id
                or run.status != "running"
                or source.status != "claimed"
                or source.claim_token != parsed_claim
                or source.claimed_until is None
                or _aware(source.claimed_until) <= now
            ):
                raise DocketError(
                    code="triage_suppression_binding_invalid",
                    message="Suppression does not match an active bounded triage context.",
                )
            sender_resolution = packet.trusted_context_json.get("sender_resolution", {})
            identity_refs = _sender_identity_refs(sender_resolution)
            entity_ref = (
                sender_resolution.get("entity_ref") if isinstance(sender_resolution, dict) else None
            )
            applicable = (
                preference in PreferenceMatcher(session).active(at=now)
                and preference.policy_kind == "suppression"
                and preference.policy_json.get("disposition") == "suppress"
                and PreferenceMatcher.matches(
                    preference,
                    entity_refs={entity_ref} if isinstance(entity_ref, str) else set(),
                    identity_refs=identity_refs,
                    source_refs={source.ref_id},
                    semantic_classes=set(semantic_classes),
                )
            )
            if not applicable:
                raise DocketError(
                    code="preference_not_applicable",
                    message="Preference is not an active authoritative suppression here.",
                )
            entry = TriageBriefEntry(
                triage_run_id=run.id,
                source_ref=source.ref_id,
                semantic_classes=list(semantic_classes),
                title=str(source.minimal_headers.get("subject") or "Suppressed source"),
                summary="Suppressed by an existing explicit Operator Preference.",
                disposition="suppress",
                reason=preference.ref_id,
            )
            session.add(entry)
            session.flush()
            source.status = "ignored"
            source.classification = {
                "schema_version": 3,
                "triage_run_ref": run.ref_id,
                "context_ref": packet.ref_id,
                "semantic_classes": list(semantic_classes),
                "disposition": "suppress",
                "preference_ref": preference.ref_id,
            }
            source.claim_token = None
            source.claimed_by = None
            source.claimed_until = None
            run.status = "completed"
            run.completed_at = now
            session.add(
                AuditEvent(
                    event_type="triage.existing_suppression_applied",
                    entity_type="triage_run",
                    entity_id=run.id,
                    actor_type="docket_intelligence",
                    primary_ref=run.ref_id,
                    affected_refs=[run.ref_id, entry.ref_id, source.ref_id],
                    basis_refs=[source.ref_id, packet.ref_id, preference.ref_id],
                    data={"preference_ref": preference.ref_id},
                )
            )
            return {
                "ok": True,
                "ref": entry.ref_id,
                "state": "suppressed",
                "summary": "Existing authoritative Preference applied.",
                "affected_refs": [run.ref_id, entry.ref_id, source.ref_id],
                "basis_refs": [source.ref_id, packet.ref_id, preference.ref_id],
                "next": None,
                "warnings": [],
            }

    def get_case(self, case_ref: str) -> dict[str, Any]:
        with self.session_factory() as session:
            case = session.scalar(select(AttentionCase).where(AttentionCase.ref_id == case_ref))
            if case is None:
                raise DocketError(
                    code="attention_case_not_found",
                    message="AttentionCase public reference was not found.",
                )
            revision = session.scalar(
                select(AttentionCaseRevision).where(
                    AttentionCaseRevision.attention_case_id == case.id,
                    AttentionCaseRevision.revision == case.latest_revision,
                )
            )
            items = list(
                session.scalars(
                    select(CaseItem)
                    .where(CaseItem.attention_case_id == case.id)
                    .order_by(CaseItem.created_at, CaseItem.ref_id)
                )
            )
            source_identities: dict[tuple[str, str], dict[str, Any]] = {}
            for source_ref in case.source_refs[:25]:
                source = session.scalar(select(SourceItem).where(SourceItem.ref_id == source_ref))
                if source is None:
                    continue
                identity = gmail_sender_identity(session, source, materialize=False)
                if identity is None:
                    continue
                key = (str(identity["handle_type"]), str(identity["value"]))
                existing = source_identities.get(key)
                if existing is None:
                    identity["source_refs"] = [source.ref_id]
                    identity.pop("source_ref", None)
                    source_identities[key] = identity
                else:
                    existing["source_refs"].append(source.ref_id)
            result: dict[str, Any] = {
                "ok": True,
                "ref": case.ref_id,
                "state": case.status,
                "version": case.version,
                "revision_ref": revision.ref_id if revision is not None else None,
                "revision": case.latest_revision,
                "title": case.title,
                "summary": case.summary,
                "priority": case.priority,
                "semantic_classes": case.semantic_classes,
                "entity_refs": case.entity_refs,
                "source_refs": case.source_refs,
                "source_identities": list(source_identities.values()),
                "items": [
                    {
                        "ref": item.ref_id,
                        "type": item.item_type,
                        "status": item.status,
                        "payload": item.payload_json,
                        "candidate_refs": item.candidate_refs,
                        "basis_refs": item.basis_refs,
                    }
                    for item in items[:25]
                ],
                "count": min(len(items), 25),
                "total_if_known": len(items),
                "truncated": len(items) > 25,
            }
            projected_items = result["items"]
            if not isinstance(projected_items, list):
                raise TypeError("AttentionCase items projection must be a list")
            while projected_items and self._serialized_bytes(result) > 16384:
                projected_items.pop()
                result["count"] = len(projected_items)
                result["truncated"] = True
            if self._serialized_bytes(result) > 16384:
                raise DocketError(
                    code="triage_output_budget_exceeded",
                    message="The AttentionCase cannot fit its compact output budget.",
                )
            return result
