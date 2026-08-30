from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import (
    AttentionCase,
    AttentionCaseRevision,
    AuditEvent,
    CaseItem,
    ChangeSet,
    Decision,
    GmailSource,
    IntentSession,
    IntentTurn,
    InterpretedStatement,
    ProviderAccount,
)
from docket.models.base import utc_now
from docket.schemas.authority import AttentionCaseResolutionInput
from docket.services.provenance_refs import ProvenanceRefService

_DURABLE_CASE_PREDICATES = frozenset({"application_status"})


def _stale_details(
    case: AttentionCase,
    current_revision: AttentionCaseRevision | None,
) -> dict[str, Any]:
    return {
        "case_ref": case.ref_id,
        "current_case_revision_ref": (
            current_revision.ref_id if current_revision is not None else None
        ),
        "current_version": case.version,
        "next": "read_current_attention_case_and_restate_intent",
    }


class AttentionCaseResolutionService:
    """Apply exact, revision-bound Operator dispositions to AttentionCases."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def handlers(self) -> dict[str, Any]:
        return {"attention_case_resolution": self.apply}

    def _current_revision(
        self,
        case: AttentionCase,
        *,
        lock: bool = False,
    ) -> AttentionCaseRevision | None:
        statement = select(AttentionCaseRevision).where(
            AttentionCaseRevision.attention_case_id == case.id,
            AttentionCaseRevision.revision == case.latest_revision,
        )
        if lock:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def validate(
        self,
        intent_session: IntentSession,
        change: AttentionCaseResolutionInput,
    ) -> list[dict[str, Any]]:
        case = self.session.scalar(
            select(AttentionCase).where(AttentionCase.ref_id == change.object_ref)
        )
        if case is None:
            return []
        current_revision = self._current_revision(case)
        revision = self.session.scalar(
            select(AttentionCaseRevision).where(
                AttentionCaseRevision.ref_id == change.case_revision_ref
            )
        )
        if (
            revision is None
            or revision.attention_case_id != case.id
            or current_revision is None
            or revision.id != current_revision.id
        ):
            return [
                {
                    "code": "attention_case_revision_stale",
                    "details": _stale_details(case, current_revision),
                }
            ]
        if (
            case.ref_id in intent_session.case_refs
            and change.case_revision_ref not in intent_session.case_revision_refs
        ):
            return [
                {
                    "code": "attention_case_reply_binding_mismatch",
                    "details": _stale_details(case, current_revision),
                }
            ]
        if case.status != "open":
            return [
                {
                    "code": "attention_case_not_open",
                    "details": {"case_ref": case.ref_id, "status": case.status},
                }
            ]
        items = list(
            self.session.scalars(
                select(CaseItem).where(CaseItem.attention_case_id == case.id)
            )
        )
        by_ref = {item.ref_id: item for item in items}
        visible_refs = set(revision.case_item_refs)
        errors: list[dict[str, Any]] = []
        explicit = {
            disposition.case_item_ref: disposition.disposition
            for disposition in change.item_dispositions
        }
        for item_ref in explicit:
            item = by_ref.get(item_ref)
            if item is None or item_ref not in visible_refs:
                errors.append(
                    {
                        "code": "attention_case_item_revision_mismatch",
                        "details": {"case_ref": case.ref_id, "case_item_ref": item_ref},
                    }
                )
            elif item.status != "open":
                errors.append(
                    {
                        "code": "attention_case_item_not_open",
                        "details": {
                            "case_ref": case.ref_id,
                            "case_item_ref": item_ref,
                            "status": item.status,
                        },
                    }
                )
        if change.case_outcome == "keep_open" and not explicit:
            errors.append(
                {
                    "code": "attention_case_disposition_required",
                    "details": {"case_ref": case.ref_id},
                }
            )
        if change.case_outcome == "resolved":
            remaining_blockers = sorted(
                item.ref_id
                for item in items
                if item.status == "open"
                and item.ref_id not in explicit
                and item.resolution_role == "required"
            )
            if remaining_blockers:
                errors.append(
                    {
                        "code": "attention_case_items_unresolved",
                        "details": {
                            "case_ref": case.ref_id,
                            "remaining_required_case_item_refs": remaining_blockers,
                            "next": "clarify_remaining_required_items_together",
                        },
                    }
                )
        return errors

    def _resolution_decision(
        self,
        changeset: ChangeSet,
        change: AttentionCaseResolutionInput,
        *,
        explicit: Mapping[str, str],
        not_pursued_refs: list[str],
    ) -> Decision:
        decision = Decision(
            decision_kind="attention_case_resolution",
            actor_ref=f"discord_user:{get_settings().operator_discord_user_id}",
            basis_refs=list(change.basis_refs),
            authorized_scope="attention_case_resolution",
            payload_json={
                "case_ref": change.object_ref,
                "case_revision_ref": change.case_revision_ref,
                "case_outcome": change.case_outcome,
                "operator_case_item_dispositions": dict(explicit),
                "system_not_pursued_case_item_refs": not_pursued_refs,
                "created_by_changeset_ref": changeset.ref_id,
            },
        )
        self.session.add(decision)
        self.session.flush()
        return decision

    def _correlation_scope(self, case: AttentionCase) -> dict[str, Any]:
        exact_sources: list[dict[str, str]] = []
        for source in self.session.scalars(
            select(GmailSource).where(GmailSource.ref_id.in_(case.source_refs))
        ):
            account_ref = self.session.scalar(
                select(ProviderAccount.ref_id).where(ProviderAccount.id == source.account_id)
            )
            conversation = source.external_parent_id or source.external_object_id
            if account_ref is None or not conversation:
                continue
            exact_sources.append(
                {
                    "provider": source.provider,
                    "account_ref": account_ref,
                    "conversation": conversation,
                }
            )
        return {
            "situation_key": case.situation_key,
            "exact_provider_conversations": exact_sources,
        }

    def _semantic_resolution_decisions(
        self,
        changeset: ChangeSet,
        change: AttentionCaseResolutionInput,
        case: AttentionCase,
        items: list[CaseItem],
        explicit: Mapping[str, str],
    ) -> list[Decision]:
        resolved_required_refs = {
            item.ref_id
            for item in items
            if explicit.get(item.ref_id) == "resolved"
            and item.resolution_role == "required"
        }
        if not resolved_required_refs:
            return []
        intent_session = self.session.get(IntentSession, changeset.intent_session_id)
        if intent_session is None:
            return []
        authority_utterances = ProvenanceRefService(
            self.session
        ).authority_utterance_refs(change.basis_refs)
        statement_refs: list[str] = []
        for turn in self.session.scalars(
            select(IntentTurn).where(
                IntentTurn.intent_session_id == intent_session.id,
                IntentTurn.utterance_ref.in_(authority_utterances),
            )
        ):
            statement_refs.extend(turn.statement_refs)
        if not statement_refs:
            return []
        relevant_subjects = {case.ref_id, *resolved_required_refs}
        statements = list(
            self.session.scalars(
                select(InterpretedStatement).where(
                    InterpretedStatement.ref_id.in_(statement_refs)
                )
            )
        )
        decisions: list[Decision] = []
        scope = self._correlation_scope(case)
        for statement in statements:
            explicitly_durable = (
                statement.interpretation_json.get("durable_case_resolution") is True
            )
            if (
                statement.predicate not in _DURABLE_CASE_PREDICATES
                and not explicitly_durable
            ):
                continue
            if not relevant_subjects.intersection(statement.subject_refs):
                continue
            basis_refs = list(dict.fromkeys([statement.ref_id, *change.basis_refs]))
            decision = Decision(
                decision_kind="case_semantic_resolution",
                actor_ref=f"discord_user:{get_settings().operator_discord_user_id}",
                basis_refs=basis_refs,
                authorized_scope="deterministic_case_source_correlation",
                payload_json={
                    "case_ref": case.ref_id,
                    "case_revision_ref": change.case_revision_ref,
                    "statement_refs": [statement.ref_id],
                    "source_refs": list(case.source_refs),
                    "predicate": statement.predicate,
                    "value_json": statement.value_json,
                    "correlation_scope_json": scope,
                    "created_by_changeset_ref": changeset.ref_id,
                    "basis_refs": basis_refs,
                },
            )
            self.session.add(decision)
            self.session.flush()
            decisions.append(decision)
        return decisions

    def apply(
        self,
        _session: Session,
        changeset: ChangeSet,
        change: AttentionCaseResolutionInput,
    ) -> list[str]:
        case = self.session.scalar(
            select(AttentionCase)
            .where(AttentionCase.ref_id == change.object_ref)
            .with_for_update()
        )
        if case is None:
            raise DocketError(
                code="attention_case_not_found",
                message="AttentionCase public reference was not found.",
            )
        intent_session = self.session.get(IntentSession, changeset.intent_session_id)
        if intent_session is None:
            raise DocketError(
                code="intent_session_not_found",
                message="AttentionCase resolution lost its IntentSession binding.",
            )
        errors = self.validate(intent_session, change)
        if errors:
            first = errors[0]
            raise DocketError(
                code=str(first["code"]),
                message="AttentionCase resolution no longer matches current case state.",
                details=dict(first.get("details", {})),
            )
        items = list(
            self.session.scalars(
                select(CaseItem)
                .where(CaseItem.attention_case_id == case.id)
                .order_by(CaseItem.created_at, CaseItem.ref_id)
                .with_for_update()
            )
        )
        explicit = {
            disposition.case_item_ref: disposition.disposition
            for disposition in change.item_dispositions
        }
        explicitly_changed: list[str] = []
        for item in items:
            disposition = explicit.get(item.ref_id)
            if disposition is not None:
                item.status = disposition
                item.version += 1
                explicitly_changed.append(item.ref_id)

        not_pursued_refs: list[str] = []
        if change.case_outcome != "keep_open":
            for item in items:
                if item.status != "open":
                    continue
                if (
                    change.case_outcome == "resolved"
                    and item.resolution_role == "required"
                ):
                    raise DocketError(
                        code="attention_case_items_unresolved",
                        message="Required CaseItems remain open.",
                        details={"remaining_required_case_item_refs": [item.ref_id]},
                    )
                item.status = "not_pursued"
                item.version += 1
                not_pursued_refs.append(item.ref_id)

        resolution_decision = self._resolution_decision(
            changeset,
            change,
            explicit=explicit,
            not_pursued_refs=not_pursued_refs,
        )
        semantic_decisions = self._semantic_resolution_decisions(
            changeset,
            change,
            case,
            items,
            explicit,
        )
        now = utc_now()
        case.version += 1
        if change.case_outcome != "keep_open":
            case.status = change.case_outcome
            case.resolved_at = now
            case.resolution_decision_ref = resolution_decision.ref_id

        affected_refs = [
            case.ref_id,
            *explicitly_changed,
            *not_pursued_refs,
            resolution_decision.ref_id,
            *[decision.ref_id for decision in semantic_decisions],
        ]
        audit_basis = list(
            dict.fromkeys(
                [
                    *change.basis_refs,
                    changeset.ref_id,
                    resolution_decision.ref_id,
                ]
            )
        )
        self.session.add(
            AuditEvent(
                event_type=(
                    "attention_case.partially_resolved"
                    if change.case_outcome == "keep_open"
                    else "attention_case.resolved"
                ),
                entity_type="attention_case",
                entity_id=case.id,
                actor_type="operator",
                actor_id=get_settings().operator_discord_user_id,
                primary_ref=case.ref_id,
                affected_refs=affected_refs,
                basis_refs=audit_basis,
                data={
                    "changeset_ref": changeset.ref_id,
                    "case_revision_ref": change.case_revision_ref,
                    "case_outcome": change.case_outcome,
                    "operator_case_item_dispositions": explicit,
                    "system_closure_rule": (
                        "supporting_items_not_pursued_on_terminal_case_closure"
                        if not_pursued_refs
                        else None
                    ),
                    "system_not_pursued_case_item_refs": not_pursued_refs,
                    "resolution_decision_ref": resolution_decision.ref_id,
                    "semantic_decision_refs": [
                        decision.ref_id for decision in semantic_decisions
                    ],
                    "content_hash": sha256_json(
                        {
                            "case_outcome": change.case_outcome,
                            "operator_case_item_dispositions": explicit,
                            "system_not_pursued_case_item_refs": not_pursued_refs,
                        }
                    ),
                },
            )
        )
        return affected_refs
