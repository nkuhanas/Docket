from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import (
    AttachmentEvidence,
    AuditEvent,
    InterpretedStatement,
    OperatorUtterance,
    Source,
    StatementRelation,
)
from docket.schemas.authority import StatementInput, StatementRelationInput


class StatementService:
    """Persist immutable semantic interpretations without canonical authority."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def derive(
        self,
        utterance_ref: str,
        statements: list[StatementInput],
    ) -> list[InterpretedStatement]:
        utterance = self.session.scalar(
            select(OperatorUtterance).where(OperatorUtterance.ref_id == utterance_ref)
        )
        if utterance is None:
            raise DocketError(
                code="operator_utterance_not_found",
                message="Statements require one persisted source OperatorUtterance.",
                details={"utterance_ref": utterance_ref},
            )
        attachment_evidence_by_ref = {
            evidence.ref_id: evidence
            for evidence in self.session.scalars(
                select(AttachmentEvidence).where(
                    AttachmentEvidence.ref_id.in_(utterance.attachment_source_refs)
                )
            )
        }
        existing = list(
            self.session.scalars(
                select(InterpretedStatement).where(
                    InterpretedStatement.utterance_id == utterance.id
                )
            )
        )
        existing_by_hash = {
            str(item.interpretation_json.get("_derivation_hash")): item
            for item in existing
            if item.interpretation_json.get("_derivation_hash")
        }
        results: list[InterpretedStatement] = []
        created_refs: list[str] = []
        for ordinal, statement_input in enumerate(statements):
            if statement_input.source_ref is not None:
                source = self.session.scalar(
                    select(Source).where(Source.ref_id == statement_input.source_ref)
                )
                if source is None:
                    raise DocketError(
                        code="statement_source_not_found",
                        message="A derived statement references an unknown Source.",
                        details={"source_ref": statement_input.source_ref},
                    )
                attachment = attachment_evidence_by_ref.get(statement_input.source_ref)
                if source.source_kind == "attachment" and (
                    attachment is None or attachment.ingest_state != "available"
                ):
                    raise DocketError(
                        code="attachment_evidence_unavailable",
                        message=(
                            "An attachment-derived statement requires available evidence "
                            "bound to this OperatorUtterance."
                        ),
                        details={"source_ref": statement_input.source_ref},
                    )
            payload = statement_input.model_dump(mode="json")
            derivation_hash = sha256_json(
                {"utterance_ref": utterance_ref, "ordinal": ordinal, "statement": payload}
            )
            replay = existing_by_hash.get(derivation_hash)
            if replay is not None:
                results.append(replay)
                continue
            interpretation_json: dict[str, Any] = dict(
                statement_input.interpretation_json
            )
            interpretation_json["_derivation_hash"] = derivation_hash
            statement = InterpretedStatement(
                utterance_id=utterance.id,
                statement_kind=statement_input.statement_kind,
                subject_refs=list(statement_input.subject_refs),
                predicate=statement_input.predicate,
                value_json=statement_input.value_json,
                affected_fields=list(statement_input.affected_fields),
                effective_from=statement_input.effective_from,
                effective_to=statement_input.effective_to,
                interpretation_json=interpretation_json,
                interpreter_version=statement_input.interpreter_version,
                source_ref=statement_input.source_ref,
                source_fragment_locator=statement_input.source_fragment_locator,
                source_fragment_hash=statement_input.source_fragment_hash,
                extractor_identifier=statement_input.extractor_identifier,
                extractor_version=statement_input.extractor_version,
            )
            self.session.add(statement)
            self.session.flush()
            if statement_input.source_ref is not None:
                attachment = attachment_evidence_by_ref.get(statement_input.source_ref)
                if (
                    attachment is not None
                    and statement.ref_id not in attachment.derived_content_refs
                ):
                    attachment.derived_content_refs = [
                        *attachment.derived_content_refs,
                        statement.ref_id,
                    ]
            results.append(statement)
            created_refs.append(statement.ref_id)
        if created_refs:
            self.session.add(
                AuditEvent(
                    event_type="statements.derived",
                    entity_type="operator_utterance",
                    entity_id=utterance.id,
                    actor_type="docket_interpreter",
                    actor_id=None,
                    request_id=None,
                    primary_ref=utterance.ref_id,
                    affected_refs=created_refs,
                    basis_refs=[utterance.ref_id],
                    data={"statement_count": len(created_refs)},
                )
            )
        return results

    def relate(self, relation_input: StatementRelationInput) -> StatementRelation:
        source = self.session.scalar(
            select(InterpretedStatement).where(
                InterpretedStatement.ref_id == relation_input.source_statement_ref
            )
        )
        target = self.session.scalar(
            select(InterpretedStatement).where(
                InterpretedStatement.ref_id == relation_input.target_statement_ref
            )
        )
        if source is None or target is None:
            missing = (
                relation_input.source_statement_ref
                if source is None
                else relation_input.target_statement_ref
            )
            raise DocketError(
                code="statement_not_found",
                message="Statement relation references an unknown statement.",
                details={"statement_ref": missing},
            )
        existing = self.session.scalar(
            select(StatementRelation).where(
                StatementRelation.source_statement_id == source.id,
                StatementRelation.target_statement_id == target.id,
                StatementRelation.relation_kind == relation_input.relation_kind,
            )
        )
        if existing is not None:
            return existing
        relation = StatementRelation(
            source_statement_id=source.id,
            target_statement_id=target.id,
            relation_kind=relation_input.relation_kind,
        )
        self.session.add(relation)
        self.session.flush()
        self.session.add(
            AuditEvent(
                event_type="statement.relation_created",
                entity_type="statement_relation",
                entity_id=relation.id,
                actor_type="docket_interpreter",
                actor_id=None,
                request_id=None,
                primary_ref=source.ref_id,
                affected_refs=[source.ref_id, target.ref_id],
                basis_refs=[source.ref_id, target.ref_id],
                data={"relation_kind": relation.relation_kind},
            )
        )
        return relation
