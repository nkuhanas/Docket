"""Native workflow adapters retain existing authority and conflict services."""

from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import select

from docket.agent_auth import require_principal
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import ChangeSet, NativeOutcome, PreparedCommand, PreparedConsumption
from docket.schemas.authority import (
    CanonicalChangeInput,
    ChangeSetCommit,
    ChangeSetContent,
    ChangeSetPrepare,
    ConflictResolve,
    IntentSessionOpen,
    StatementInput,
)
from docket.schemas.native_workflows import (
    RecordContext,
    RequestClarification,
    ResolveAttentionCase,
    ResolveConflict,
)
from docket.services.agent_requests import AgentRequestService
from docket.services.conflicts import ConflictService
from docket.services.intent_sessions import IntentSessionService
from docket.services.interactive_authority import InteractiveAuthorityService
from docket.services.native_commands import NativeCommandService
from docket.services.native_context import NativeContextService
from docket.services.prepared_batches import consume


class NativeWorkflowService(NativeCommandService):
    def perform(
        self,
        value: RecordContext | RequestClarification | ResolveConflict | ResolveAttentionCase,
        request_ref: str,
        request_key: str,
    ) -> dict[str, Any]:
        principal = require_principal("stage")
        AgentRequestService(self.session).require(
            request_ref,
            permission="stage",
            allow_committed=True,
        )
        command = type(value).__name__
        digest = sha256_json(value.model_dump(mode="json"))
        prior = self.session.get(NativeOutcome, request_ref)
        if prior is not None:
            if (prior.command, prior.argument_hash) != (command, digest):
                raise DocketError(
                    code="operation_id_reused",
                    message="Use a new operation_id for changed content.",
                )
            return {**prior.receipt, "replayed": True}
        if (
            self.session.scalar(
                select(PreparedCommand.ref_id).where(
                    PreparedCommand.request_ref == request_ref,
                )
            )
            is not None
        ):
            raise DocketError(code="operation_id_reused", message="Choose a new operation_id.")
        if isinstance(value, RecordContext):
            result = AgentRequestService(self.session).record(
                request_ref=value.for_request or request_ref,
                record_key=value.operation_id,
                record_kind=value.record_kind,
                text=value.text,
                gap_code=value.gap_code,
            )
        elif isinstance(value, RequestClarification):
            result = InteractiveAuthorityService(self.session).process_turn(
                utterance_ref=request_ref,
                request_key=request_key,
                actor_id=principal.principal_ref,
                intent_session_ref=None,
                expected_session_version=None,
                statements=[],
                relations=[],
                resolved_intent_json={},
                content=None,
                changeset_ref=None,
                expected_changeset_version=None,
                blocking_clarifications=[
                    {
                        "blocking": True,
                        "code": "operator_choice_required",
                        "question": value.question,
                    }
                ],
                semantic_options=list(value.choices),
            )
        else:
            result = (
                self.resolve_case(value, request_ref, request_key)
                if isinstance(value, ResolveAttentionCase)
                else self.resolve(value, request_ref, request_key, digest)
            )
        receipt = dict(result)
        self.session.add(
            NativeOutcome(
                request_ref=request_ref,
                command=command,
                argument_hash=digest,
                receipt=receipt,
            )
        )
        self.session.flush()
        return receipt

    def resolve_case(
        self,
        value: ResolveAttentionCase,
        request_ref: str,
        request_key: str,
    ) -> dict[str, Any]:
        AgentRequestService(self.session).require(request_ref, permission="commit")
        context = NativeContextService(self.session, self.settings).require(value.context_id)
        observed = next(
            (obj for obj in context.snapshot.get("objects", []) if obj["ref"] == value.case_ref),
            None,
        )
        if observed is None or value.case_ref not in context.versions:
            raise DocketError(code="case_not_in_context", message="Read context for this case.")
        revision_ref = observed["state"]["current_revision_ref"]
        intent, _ = IntentSessionService(self.session).open(
            IntentSessionOpen(source_request_ref=request_ref),
        )
        service = InteractiveAuthorityService(self.session).changesets
        draft, _ = service.prepare(
            ChangeSetPrepare(
                intent_session_ref=intent.ref_id,
                expected_session_version=intent.version,
                idempotency_key=request_key + ":attention-case",
                content=ChangeSetContent.model_validate(
                    {
                        "basis_refs": [request_ref],
                        "expected_versions": {value.case_ref: context.versions[value.case_ref]},
                        "resolution_changes": [
                            {
                                "change_id": "case-resolution",
                                "action": "update",
                                "object_type": "attention_case_resolution",
                                "object_ref": value.case_ref,
                                "case_revision_ref": revision_ref,
                                "case_outcome": value.outcome,
                                "item_dispositions": [item.model_dump() for item in value.items],
                                "basis_refs": [request_ref],
                            }
                        ],
                    }
                ),
            )
        )
        if draft.validation_errors:
            raise DocketError(
                code="invalid_case_disposition",
                message="Correct only the indicated case items.",
                details={"issues": self.public_issues(draft.validation_errors)},
            )
        committed, receipt = service.commit(
            ChangeSetCommit(
                changeset_ref=draft.ref_id,
                expected_version=draft.version,
                idempotency_key=draft.idempotency_key,
                authority_request_ref=request_ref,
            )
        )
        return {
            "ok": True,
            "disposition": "committed",
            "changeset_ref": committed.ref_id,
            "request_ref": request_ref,
            "affected_refs": receipt.affected_refs,
            **receipt.projection(),
        }

    def resolve(
        self,
        value: ResolveConflict,
        request_ref: str,
        request_key: str,
        digest: str,
    ) -> dict[str, Any]:
        principal = require_principal("resolve_conflict")
        context = NativeContextService(self.session, self.settings).require(value.context_id)
        if value.conflict_ref not in context.versions:
            raise DocketError(
                code="conflict_not_in_context",
                message="Read this conflict with docket_get_context first.",
            )
        conflict = ConflictService(self.session).get(value.conflict_ref)
        effects: list[CanonicalChangeInput] = []
        versions: dict[str, int] = {}
        member = None
        if value.change_id:
            member = self.session.scalar(
                select(PreparedCommand).where(
                    PreparedCommand.ref_id == value.change_id,
                    PreparedCommand.principal_ref == principal.principal_ref,
                )
            )
            if member is None:
                raise DocketError(code="change_unavailable", message="Choose your exact preview.")
            AgentRequestService(self.session).require(member.request_ref, permission="commit")
            draft = self.session.scalar(
                select(ChangeSet)
                .where(
                    ChangeSet.ref_id == member.changeset_ref,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                draft is None
                or self.fingerprint(draft) != member.revision_hash
                or draft.state == "committed"
                or self.session.get(PreparedConsumption, member.ref_id) is not None
            ):
                raise DocketError(code="prepared_change_stale", message="Read a current preview.")
            if draft.normalized_entries_json or member.command == "batch":
                raise DocketError(
                    code="unsupported_conflict_preview",
                    message="Use one current business-command preview.",
                )
            adapter: TypeAdapter[CanonicalChangeInput] = TypeAdapter(CanonicalChangeInput)
            effects = [
                adapter.validate_python(action) for action in (draft.staged_actions_json or [])
            ]
            versions = draft.expected_versions
        resolution = ConflictResolve(
            conflict_ref=conflict.ref_id,
            expected_version=context.versions[conflict.ref_id],
            authority_request_ref=request_ref,
            resolution=value.resolution,
            chosen_interpretation=value.chosen_interpretation,
            statements_superseded=value.statements_superseded,
            statements_retained=value.statements_retained,
            effective_scope=value.effective_scope,
            expected_versions=versions,
            canonical_effects=effects,
        )
        result = InteractiveAuthorityService(self.session).process_conflict_resolution(
            utterance_ref=request_ref,
            request_key=request_key,
            actor_id=principal.principal_ref,
            intent_session_ref=None,
            expected_session_version=None,
            statement=StatementInput(
                statement_kind="conflict_resolution",
                subject_refs=conflict.subject_refs,
                predicate="conflict_resolution",
                value_json=value.chosen_interpretation,
                affected_fields=conflict.affected_fields,
                interpretation_json={"conflict_ref": conflict.ref_id},
                interpreter_version="docket-native-conflict-v1",
            ),
            resolution=resolution,
        )
        if member is not None:
            committed = self.session.scalar(
                select(ChangeSet).where(
                    ChangeSet.ref_id == result["ref"],
                )
            )
            assert committed is not None and committed.state == "committed"
            parent = PreparedCommand(
                principal_ref=principal.principal_ref,
                operation_id=value.operation_id,
                command="resolve_conflict",
                argument_hash=digest,
                context_ref=context.ref_id,
                request_ref=request_ref,
                changeset_ref=committed.ref_id,
                revision=committed.current_revision,
                revision_hash=self.fingerprint(committed),
                preview={"conflict_ref": conflict.ref_id, "member_change_ids": [member.ref_id]},
            )
            self.session.add(parent)
            self.session.flush()
            consume(self, parent, [member])
            result = {**result, "change_id": parent.ref_id}
        return result
