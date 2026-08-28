from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.errors import DocketError
from docket.domain.public_refs import parse_public_ref
from docket.models import Entity, Fact, InterpretedStatement
from docket.schemas.authority import ConflictOpen
from docket.services.conflicts import ConflictService


class RegistryConflictCompiler:
    """Compile ambiguous fact contradictions against effective canonical state."""

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _statement_basis(fact: Fact) -> list[str]:
        return [
            ref_id
            for ref_id in fact.basis_refs
            if parse_public_ref(ref_id)[0] == "stm"
        ]

    def compile(self, incoming_refs: list[str]) -> list[str]:
        conflicts: list[str] = []
        incoming = list(
            self.session.scalars(
                select(InterpretedStatement).where(
                    InterpretedStatement.ref_id.in_(incoming_refs)
                )
            )
        )
        for statement in incoming:
            subject_entities = list(
                self.session.scalars(
                    select(Entity).where(Entity.ref_id.in_(statement.subject_refs))
                )
            )
            for entity in subject_entities:
                facts = self.session.scalars(
                    select(Fact).where(
                        Fact.subject_entity_id == entity.id,
                        Fact.predicate == statement.predicate,
                        Fact.status == "active",
                    )
                )
                for fact in facts:
                    if fact.value_json == statement.value_json:
                        continue
                    prior_refs = self._statement_basis(fact)
                    if not prior_refs:
                        # Legacy/pre-ledger facts cannot be assigned invented statement
                        # provenance; they require an explicit correction or clarification.
                        continue
                    try:
                        conflict = ConflictService(self.session).open(
                            ConflictOpen(
                                subject_refs=[entity.ref_id],
                                affected_fields=list(statement.affected_fields),
                                prior_statement_refs=prior_refs,
                                incoming_statement_refs=[statement.ref_id],
                                conflicting_effects_json={
                                    "canonical_ref": fact.ref_id,
                                    "prior": fact.value_json,
                                    "incoming": statement.value_json,
                                },
                            )
                        )
                    except DocketError as exc:
                        if exc.code == "conflict_not_applicable":
                            continue
                        raise
                    if conflict.ref_id not in conflicts:
                        conflicts.append(conflict.ref_id)
        return conflicts
