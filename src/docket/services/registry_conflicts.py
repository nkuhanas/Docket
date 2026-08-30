from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.errors import DocketError
from docket.domain.public_refs import parse_public_ref
from docket.models import (
    Entity,
    Fact,
    IdentityBinding,
    IdentityHandle,
    InterpretedStatement,
    TemporalBinding,
)
from docket.schemas.authority import ChangeSetContent, ConflictOpen, StatementInput
from docket.services.conflicts import ConflictService
from docket.services.registry import normalize_registry_text
from docket.services.statements import StatementService


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
                        Fact.subject_ref == entity.ref_id,
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


class IdentityBindingConflictCompiler:
    """Open a scoped Conflict when selected binding authority meets newer state."""

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def selected_statements(content: ChangeSetContent) -> list[StatementInput]:
        entity_creates = {
            change.change_id: change
            for change in content.registry_changes
            if change.mutation_type == "entity_create"
        }
        statements: list[StatementInput] = []
        for change in content.registry_changes:
            if change.mutation_type != "identity_binding_bind" or change.object_ref is None:
                continue
            target: dict[str, object]
            if change.payload.entity_ref is not None:
                target = {"entity_ref": change.payload.entity_ref}
            else:
                create = entity_creates.get(str(change.payload.entity_change_id))
                if create is None:
                    continue
                target = {
                    "entity_spec": {
                        "entity_kind": create.create_spec.entity_kind,
                        "display_name": create.create_spec.display_name,
                    }
                }
            statements.append(
                StatementInput(
                    statement_kind="selected_identity_binding",
                    subject_refs=[change.object_ref],
                    predicate="identity_binding",
                    value_json=target,
                    affected_fields=["identity_binding"],
                    interpretation_json={
                        "change_id": change.change_id,
                        "deterministic_from_typed_changeset": True,
                    },
                    interpreter_version="docket-semantic-option-binding-v1",
                )
            )
        return statements

    def _same_target(self, statement: InterpretedStatement, entity: Entity) -> bool:
        if statement.value_json.get("entity_ref") == entity.ref_id:
            return True
        spec = statement.value_json.get("entity_spec")
        return bool(
            isinstance(spec, dict)
            and spec.get("entity_kind") == entity.entity_kind
            and normalize_registry_text(str(spec.get("display_name", "")))
            == entity.normalized_name
        )

    def _prior_statement(
        self,
        *,
        handle: IdentityHandle,
        binding: IdentityBinding,
        entity: Entity,
    ) -> InterpretedStatement | None:
        statement_refs = [
            ref
            for ref in binding.basis_refs
            if isinstance(ref, str) and ref.startswith("stm_")
        ]
        if statement_refs:
            return self.session.scalar(
                select(InterpretedStatement).where(
                    InterpretedStatement.ref_id.in_(statement_refs)
                )
            )
        utterance_ref = next(
            (
                ref
                for ref in [*binding.basis_refs, *handle.binding_basis_refs]
                if isinstance(ref, str) and ref.startswith("utt_")
            ),
            None,
        )
        if utterance_ref is None:
            return None
        return StatementService(self.session).derive(
            utterance_ref,
            [
                StatementInput(
                    statement_kind="canonical_identity_binding_basis",
                    subject_refs=[handle.ref_id],
                    predicate="identity_binding",
                    value_json={"entity_ref": entity.ref_id},
                    affected_fields=["identity_binding"],
                    interpretation_json={
                        "identity_ref": handle.ref_id,
                        "entity_ref": entity.ref_id,
                        "deterministic_from_canonical_binding": True,
                    },
                    interpreter_version="docket-canonical-binding-v1",
                )
            ],
        )[0]

    def compile(self, incoming_refs: list[str]) -> list[str]:
        conflicts: list[str] = []
        incoming = list(
            self.session.scalars(
                select(InterpretedStatement).where(
                    InterpretedStatement.ref_id.in_(incoming_refs),
                    InterpretedStatement.predicate == "identity_binding",
                )
            )
        )
        for statement in incoming:
            for identity_ref in statement.subject_refs:
                handle = self.session.scalar(
                    select(IdentityHandle).where(IdentityHandle.ref_id == identity_ref)
                )
                if handle is None or handle.entity_id is None or handle.status != "bound":
                    continue
                entity = self.session.get(Entity, handle.entity_id)
                if entity is None or self._same_target(statement, entity):
                    continue
                binding = self.session.scalar(
                    select(IdentityBinding).where(
                        IdentityBinding.identity_handle_id == handle.id,
                        IdentityBinding.status == "active",
                    )
                )
                if binding is None:
                    continue
                prior = self._prior_statement(
                    handle=handle,
                    binding=binding,
                    entity=entity,
                )
                if prior is None:
                    continue
                conflict = ConflictService(self.session).open(
                    ConflictOpen(
                        subject_refs=[handle.ref_id],
                        affected_fields=["identity_binding"],
                        prior_statement_refs=[prior.ref_id],
                        incoming_statement_refs=[statement.ref_id],
                        conflicting_effects_json={
                            "identity_ref": handle.ref_id,
                            "current_entity_ref": entity.ref_id,
                            "requested_target": statement.value_json,
                        },
                    )
                )
                if conflict.ref_id not in conflicts:
                    conflicts.append(conflict.ref_id)
        return conflicts


class TemporalBindingConflictCompiler:
    """Compile ambiguous Time replacements against one effective binding identity."""

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _predicate(*, role: str, binding_key: str) -> str:
        return f"temporal_binding:{role}:{binding_key}"

    @classmethod
    def selected_statements(cls, content: ChangeSetContent) -> list[StatementInput]:
        statements: list[StatementInput] = []
        for change in content.tracked_context_changes:
            if change.mutation_type != "temporal_binding_create":
                continue
            spec = change.create_spec
            if spec.subject_ref is None:
                continue
            predicate = cls._predicate(
                role=spec.role,
                binding_key=spec.binding_key,
            )
            statements.append(
                StatementInput(
                    statement_kind="selected_temporal_binding",
                    subject_refs=[spec.subject_ref],
                    predicate=predicate,
                    value_json=spec.temporal_value.model_dump(mode="json"),
                    affected_fields=[predicate],
                    interpretation_json={
                        "change_id": change.change_id,
                        "deterministic_from_typed_changeset": True,
                    },
                    interpreter_version="docket-temporal-binding-v1",
                )
            )
        return statements

    def _prior_statement(self, binding: TemporalBinding) -> InterpretedStatement | None:
        statement_refs = [
            ref
            for ref in binding.basis_refs
            if isinstance(ref, str) and ref.startswith("stm_")
        ]
        if statement_refs:
            statement = self.session.scalar(
                select(InterpretedStatement).where(
                    InterpretedStatement.ref_id.in_(statement_refs),
                    InterpretedStatement.predicate
                    == self._predicate(
                        role=binding.role,
                        binding_key=binding.binding_key,
                    ),
                )
            )
            if statement is not None:
                return statement
        utterance_ref = next(
            (
                ref
                for ref in binding.basis_refs
                if isinstance(ref, str) and ref.startswith("utt_")
            ),
            None,
        )
        if utterance_ref is None:
            return None
        predicate = self._predicate(
            role=binding.role,
            binding_key=binding.binding_key,
        )
        return StatementService(self.session).derive(
            utterance_ref,
            [
                StatementInput(
                    statement_kind="canonical_temporal_binding_basis",
                    subject_refs=[binding.subject_ref],
                    predicate=predicate,
                    value_json=dict(binding.temporal_value),
                    affected_fields=[predicate],
                    interpretation_json={
                        "temporal_binding_ref": binding.ref_id,
                        "deterministic_from_canonical_binding": True,
                    },
                    interpreter_version="docket-canonical-temporal-binding-v1",
                )
            ],
        )[0]

    def compile(self, incoming_refs: list[str]) -> list[str]:
        conflicts: list[str] = []
        incoming = list(
            self.session.scalars(
                select(InterpretedStatement).where(
                    InterpretedStatement.ref_id.in_(incoming_refs),
                    InterpretedStatement.statement_kind
                    == "selected_temporal_binding",
                )
            )
        )
        for statement in incoming:
            for subject_ref in statement.subject_refs:
                bindings = list(
                    self.session.scalars(
                        select(TemporalBinding).where(
                            TemporalBinding.subject_ref == subject_ref,
                            TemporalBinding.canonical_status == "active",
                        )
                    )
                )
                for binding in bindings:
                    predicate = self._predicate(
                        role=binding.role,
                        binding_key=binding.binding_key,
                    )
                    if statement.predicate != predicate:
                        continue
                    if statement.value_json == binding.temporal_value:
                        continue
                    prior = self._prior_statement(binding)
                    if prior is None:
                        continue
                    conflict = ConflictService(self.session).open(
                        ConflictOpen(
                            subject_refs=[subject_ref],
                            affected_fields=[predicate],
                            prior_statement_refs=[prior.ref_id],
                            incoming_statement_refs=[statement.ref_id],
                            conflicting_effects_json={
                                "temporal_binding_ref": binding.ref_id,
                                "prior": binding.temporal_value,
                                "incoming": statement.value_json,
                            },
                        )
                    )
                    if conflict.ref_id not in conflicts:
                        conflicts.append(conflict.ref_id)
        return conflicts
