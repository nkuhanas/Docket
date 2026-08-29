from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.models import (
    AuditEvent,
    Conflict,
    Decision,
    InterpretedStatement,
    OperatorUtterance,
    StatementRelation,
)
from docket.models.base import utc_now
from docket.schemas.authority import ConflictOpen, ConflictResolve

_RESOLVING_RELATIONS = frozenset({"supersedes", "retracts", "scopes"})


def _intervals_overlap(
    left_from: date | None,
    left_to: date | None,
    right_from: date | None,
    right_to: date | None,
) -> bool:
    if left_to is not None and right_from is not None and left_to < right_from:
        return False
    return not (
        right_to is not None and left_from is not None and right_to < left_from
    )


class ConflictService:
    """Open and resolve field-scoped incompatibilities without erasing evidence."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _statements(self, refs: list[str]) -> list[InterpretedStatement]:
        items = list(
            self.session.scalars(
                select(InterpretedStatement).where(InterpretedStatement.ref_id.in_(refs))
            )
        )
        by_ref = {item.ref_id: item for item in items}
        missing = [ref_id for ref_id in refs if ref_id not in by_ref]
        if missing:
            raise DocketError(
                code="statement_not_found",
                message="Conflict references an unknown statement.",
                details={"statement_refs": missing},
            )
        return [by_ref[ref_id] for ref_id in refs]

    def open(self, request: ConflictOpen) -> Conflict:
        prior = self._statements(list(request.prior_statement_refs))
        incoming = self._statements(list(request.incoming_statement_refs))
        incompatible_pairs: list[tuple[InterpretedStatement, InterpretedStatement]] = []
        for previous in prior:
            for candidate in incoming:
                same_semantic_scope = bool(
                    set(previous.subject_refs) & set(candidate.subject_refs)
                ) and bool(set(previous.affected_fields) & set(candidate.affected_fields))
                if not same_semantic_scope or previous.predicate != candidate.predicate:
                    continue
                if not _intervals_overlap(
                    previous.effective_from,
                    previous.effective_to,
                    candidate.effective_from,
                    candidate.effective_to,
                ):
                    continue
                if previous.value_json == candidate.value_json:
                    continue
                resolving_relation = self.session.scalar(
                    select(StatementRelation.id).where(
                        StatementRelation.source_statement_id == candidate.id,
                        StatementRelation.target_statement_id == previous.id,
                        StatementRelation.relation_kind.in_(_RESOLVING_RELATIONS),
                    )
                )
                if resolving_relation is None:
                    incompatible_pairs.append((previous, candidate))
        if not incompatible_pairs:
            raise DocketError(
                code="conflict_not_applicable",
                message=(
                    "The supplied statements do not form an unresolved, overlapping "
                    "semantic conflict."
                ),
            )
        prior_refs = sorted(set(request.prior_statement_refs))
        incoming_refs = sorted(set(request.incoming_statement_refs))
        for existing in self.session.scalars(select(Conflict)):
            if (
                sorted(existing.prior_statement_refs) == prior_refs
                and sorted(existing.incoming_statement_refs) == incoming_refs
                and sorted(existing.affected_fields) == sorted(request.affected_fields)
            ):
                return existing
        conflict = Conflict(
            subject_refs=list(request.subject_refs),
            affected_fields=list(request.affected_fields),
            prior_statement_refs=list(request.prior_statement_refs),
            incoming_statement_refs=list(request.incoming_statement_refs),
            conflicting_effects_json=dict(request.conflicting_effects_json),
            status="open",
        )
        self.session.add(conflict)
        self.session.flush()
        basis_refs = [*conflict.prior_statement_refs, *conflict.incoming_statement_refs]
        self.session.add(
            AuditEvent(
                event_type="conflict.opened",
                entity_type="conflict",
                entity_id=conflict.id,
                actor_type="docket_compiler",
                actor_id=None,
                request_id=None,
                primary_ref=conflict.ref_id,
                affected_refs=[conflict.ref_id, *conflict.subject_refs],
                basis_refs=basis_refs,
                data={"affected_fields": conflict.affected_fields},
            )
        )
        return conflict

    def get(self, conflict_ref: str) -> Conflict:
        conflict = self.session.scalar(
            select(Conflict).where(Conflict.ref_id == conflict_ref)
        )
        if conflict is None:
            raise DocketError(
                code="conflict_not_found",
                message="Conflict public reference was not found.",
                details={"conflict_ref": conflict_ref},
            )
        return conflict

    def validate_resolution(
        self, request: ConflictResolve
    ) -> tuple[Conflict, OperatorUtterance]:
        conflict = self.get(request.conflict_ref)
        if conflict.status != "open":
            raise DocketError(
                code="conflict_not_open",
                message="Conflict is no longer open.",
                details={"status": conflict.status},
            )
        if conflict.version != request.expected_version:
            raise DocketError(
                code="version_conflict",
                message="Conflict changed after it was read.",
                details={
                    "conflict_ref": conflict.ref_id,
                    "expected_version": request.expected_version,
                    "current_version": conflict.version,
                },
            )
        utterance = self.session.scalar(
            select(OperatorUtterance).where(
                OperatorUtterance.ref_id == request.authority_utterance_ref
            )
        )
        settings = get_settings()
        expected_actor = f"discord_user:{settings.operator_discord_user_id}"
        if utterance is None or utterance.actor_ref != expected_actor:
            raise DocketError(
                code="operator_utterance_authority_required",
                message="Conflict resolution requires the current authenticated OperatorUtterance.",
            )
        conflict_statement_refs = {
            *conflict.prior_statement_refs,
            *conflict.incoming_statement_refs,
        }
        named_statement_refs = {
            *request.statements_superseded,
            *request.statements_retained,
        }
        if not named_statement_refs or not named_statement_refs.issubset(
            conflict_statement_refs
        ):
            raise DocketError(
                code="invalid_conflict_resolution",
                message="Resolution must name statements from the Conflict.",
            )
        return conflict, utterance

    def resolve(self, request: ConflictResolve) -> tuple[Conflict, Decision]:
        conflict, utterance = self.validate_resolution(request)
        settings = get_settings()
        decision = Decision(
            decision_kind="conflict_resolution",
            actor_ref=utterance.actor_ref,
            basis_refs=[utterance.ref_id, conflict.ref_id],
            authorized_scope="conflict_resolution",
            architecture_authority=False,
            implementation_authority="operator_utterance",
            payload_json={
                "conflict_ref": conflict.ref_id,
                "chosen_interpretation": request.chosen_interpretation,
                "statements_superseded": request.statements_superseded,
                "statements_retained": request.statements_retained,
                "effective_scope": request.effective_scope,
                "canonical_effects": [
                    effect.model_dump(mode="json", exclude_none=True)
                    for effect in request.canonical_effects
                ],
                "resolution": request.resolution,
            },
        )
        self.session.add(decision)
        self.session.flush()
        conflict.status = request.resolution
        conflict.resolution_decision_ref = decision.ref_id
        conflict.version += 1
        conflict.resolved_at = utc_now()
        self.session.add(
            AuditEvent(
                event_type="conflict.resolved",
                entity_type="conflict",
                entity_id=conflict.id,
                actor_type="operator",
                actor_id=settings.operator_discord_user_id,
                request_id=None,
                primary_ref=conflict.ref_id,
                affected_refs=[conflict.ref_id, decision.ref_id],
                basis_refs=[utterance.ref_id, conflict.ref_id],
                data={"resolution": request.resolution},
            )
        )
        return conflict, decision

    def projection(self, conflict: Conflict) -> dict[str, Any]:
        prior = self._statements(list(conflict.prior_statement_refs))
        incoming = self._statements(list(conflict.incoming_statement_refs))
        return {
            "ref": conflict.ref_id,
            "state": conflict.status,
            "version": conflict.version,
            "subject_refs": conflict.subject_refs,
            "affected_fields": conflict.affected_fields,
            "prior_statements": [
                {
                    "ref": item.ref_id,
                    "value": item.value_json,
                    "effective_from": (
                        item.effective_from.isoformat() if item.effective_from else None
                    ),
                    "effective_to": (
                        item.effective_to.isoformat() if item.effective_to else None
                    ),
                }
                for item in prior
            ],
            "incoming_statements": [
                {
                    "ref": item.ref_id,
                    "value": item.value_json,
                    "effective_from": (
                        item.effective_from.isoformat() if item.effective_from else None
                    ),
                    "effective_to": (
                        item.effective_to.isoformat() if item.effective_to else None
                    ),
                }
                for item in incoming
            ],
            "resolution_decision_ref": conflict.resolution_decision_ref,
            "allowed_resolution_actions": [
                "resolved_supersession",
                "resolved_scoped_coexistence",
                "resolved_retraction",
            ],
        }
