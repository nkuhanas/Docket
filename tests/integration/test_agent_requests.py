from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from docket.agent_auth import AgentCallContext, AgentPrincipal, agent_call_context
from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import (
    AuditEvent,
    AuthenticatedRequest,
    ChangeSet,
    ChangeSetRevision,
    ConversationRecord,
    Item,
    OperatorUtterance,
    SemanticRequest,
)
from docket.schemas.assembly import StageChangesInput
from docket.services.agent_requests import AgentRequestService
from docket.services.changeset_assembly import (
    ChangeSetAssemblyAdmissionService,
    ChangeSetAssemblyService,
)


def _principal(role="interactive"):
    return AgentPrincipal(
        principal_ref=f"agent:{role}",
        operator_ref=f"operator:{get_settings().operator_discord_user_id}",
        role=role,
        permissions=frozenset({"read", "stage", "commit", "resolve_conflict"}),
    )


@pytest.fixture
def interactive_context():
    token = agent_call_context.set(AgentCallContext(_principal()))
    try:
        yield
    finally:
        agent_call_context.reset(token)


@pytest.mark.integration
def test_request_is_durable_without_a_transcript(session_factory, interactive_context):
    with session_factory.begin() as session:
        ref = AgentRequestService(session).admit(request_key="synthetic-request-1").ref_id
    with session_factory.begin() as session:
        repeated = AgentRequestService(session).admit(request_key="synthetic-request-1")
        assert repeated.ref_id == ref
        assert repeated.state == "active"
        assert repeated.role == "interactive"
        assert session.scalar(select(func.count(AuthenticatedRequest.id))) == 1
        assert session.scalar(select(func.count(AuditEvent.id))) == 1
        assert session.scalar(select(func.count(OperatorUtterance.id))) == 0
        assert session.scalar(select(func.count(ConversationRecord.id))) == 0


@pytest.mark.integration
@pytest.mark.parametrize("role", ["triage", "read_only"])
def test_background_cannot_admit_even_with_claimed_permissions(session, role):
    token = agent_call_context.set(AgentCallContext(_principal(role)))
    try:
        with pytest.raises(DocketError) as error:
            AgentRequestService(session).admit(request_key="copied-interactive-request")
        assert error.value.code == "agent_authority_denied"
        assert session.scalar(select(func.count(AuthenticatedRequest.id))) == 0
    finally:
        agent_call_context.reset(token)


@pytest.mark.integration
def test_request_requires_server_authentication(session):
    token = agent_call_context.set(None)
    try:
        with pytest.raises(DocketError) as error:
            AgentRequestService(session).admit(request_key="untrusted")
        assert error.value.code == "agent_authentication_required"
    finally:
        agent_call_context.reset(token)


@pytest.mark.integration
@pytest.mark.parametrize(
    "change",
    [
        {"principal_ref": "agent:another"},
        {"operator_ref": "operator:another"},
        {"enabled": False},
        {"audience": "another-service"},
        {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
    ],
)
def test_request_cannot_be_used_by_wrong_or_revoked_principal(session, interactive_context, change):
    row = AgentRequestService(session).admit(request_key="owned-request")
    token = agent_call_context.set(AgentCallContext(replace(_principal(), **change)))
    try:
        with pytest.raises(DocketError):
            AgentRequestService(session).require(row.ref_id, permission="commit")
    finally:
        agent_call_context.reset(token)


@pytest.mark.integration
def test_optional_capture_can_arrive_after_commit_and_remains_reported(
    session_factory, interactive_context
):
    with session_factory.begin() as session:
        row = AgentRequestService(session).admit(request_key="finished-request")
        row.state = "committed"
        ref = row.ref_id
    with session_factory.begin() as session:
        service = AgentRequestService(session)
        capture = service.record(
            request_ref=ref,
            record_key="transcript-1",
            record_kind="operator_transcript",
            text="Synthetic reported wording",
        )
        replay = service.record(
            request_ref=ref,
            record_key="transcript-1",
            record_kind="operator_transcript",
            text="Synthetic reported wording",
        )
        assert capture["ref"] == replay["ref"]
        record = session.scalar(select(ConversationRecord))
        assert record.capture_method == "agent_reported"
        assert b"Synthetic reported wording" not in record.ciphertext
        assert service.require(ref, permission="stage", allow_committed=True).state == "committed"
        with pytest.raises(DocketError) as error:
            service.record(
                request_ref=ref,
                record_key="transcript-1",
                record_kind="operator_transcript",
                text="A correction",
            )
        assert error.value.code == "record_idempotency_mismatch"


@pytest.mark.integration
def test_optional_capture_failure_does_not_erase_request(session_factory, interactive_context):
    with session_factory.begin() as session:
        ref = AgentRequestService(session).admit(request_key="capture-failure").ref_id
    with pytest.raises(DocketError), session_factory.begin() as session:
        AgentRequestService(session).record(
            request_ref=ref,
            record_key="oversize",
            record_kind="operator_transcript",
            text="x" * 65537,
        )
    with session_factory.begin() as session:
        assert AgentRequestService(session).require(ref, permission="commit").state == "active"
        assert session.scalar(select(func.count(ConversationRecord.id))) == 0


def _stage_request(root, *, title="Resolved item", scope=True):
    return StageChangesInput.model_validate(
        {
            "request_ref": root.ref_id,
            "request_key": root.request_key,
            **(
                {
                    "assembly_scope": {
                        "resolved_intent": {"intent": "track the requested item"},
                        "allowed_mutation_types": ["item_create"],
                        "planned_create_types": ["item"],
                    }
                }
                if scope
                else {}
            ),
            "patch": {
                "operations": [
                    {
                        "operation": "action_upsert",
                        "action": {
                            "change_id": "item-1",
                            "mutation_type": "item_create",
                            "action": "create",
                            "object_type": "item",
                            "affected_fields": ["title"],
                            "create_spec": {"title": title, "kind": "test.request"},
                        },
                    }
                ]
            },
        }
    )


def _admit_agent(session, root, *, name, key, digest, execution="foreground-1"):
    return ChangeSetAssemblyAdmissionService(session).admit_agent(
        request_ref=root.ref_id,
        execution_key=execution,
        operation_key=key,
        tool_name=name,
        argument_hash=digest,
    )["assembly_operation_token"]


def _stage_agent(session, root, *, key="stage-1", title="Resolved item", scope=True):
    request = _stage_request(root, title=title, scope=scope)
    digest = sha256_json(request.model_dump(mode="json", exclude_none=True))
    token = _admit_agent(session, root, name="docket_stage_changes", key=key, digest=digest)
    return ChangeSetAssemblyService(session).stage(
        request,
        assembly_operation_token=token,
        assembly_argument_hash=digest,
    )


@pytest.mark.integration
def test_agent_stages_corrects_and_commits_without_utterance_or_source(
    session_factory, interactive_context
):
    with session_factory.begin() as session:
        root = AgentRequestService(session).admit(request_key="resolved-work")
        result = _stage_agent(session, root, title="Misread item")
        assert result["disposition"] == "ready_to_commit", result
        corrected = _stage_agent(
            session, root, key="stage-correction", title="Correct item", scope=False
        )
        assert corrected["disposition"] == "ready_to_commit", corrected
        assert corrected["current_revision"] == 2
        token = _admit_agent(
            session, root, name="docket_commit_changeset", key="commit-1", digest="c" * 64
        )
        committed = ChangeSetAssemblyService(session).commit(
            request_ref=root.ref_id,
            request_key=root.request_key,
            assembly_operation_token=token,
            assembly_argument_hash="c" * 64,
        )
        assert committed["disposition"] == "committed", committed
        root_ref = root.ref_id
    with session_factory.begin() as session:
        root = AgentRequestService(session).require(
            root_ref, permission="commit", allow_committed=True
        )
        token = _admit_agent(
            session, root, name="docket_commit_changeset", key="commit-1", digest="c" * 64
        )
        repeated = ChangeSetAssemblyService(session).commit(
            request_ref=root.ref_id,
            request_key=root.request_key,
            assembly_operation_token=token,
            assembly_argument_hash="c" * 64,
        )
        assert repeated["changeset_ref"] == committed["changeset_ref"]
        assert session.scalar(select(Item.title)) == "Correct item"
        assert session.scalar(select(func.count(Item.id))) == 1
        assert session.scalar(select(func.count(ChangeSet.id))) == 1
        assert session.scalar(select(func.count(ChangeSetRevision.id))) == 2
        assert session.scalar(select(func.count(OperatorUtterance.id))) == 0
        assert session.scalar(select(func.count(ConversationRecord.id))) == 0
        assert session.scalar(select(SemanticRequest.authenticated_request_ref)) == root_ref
        assert root.state == "committed"
        assert root_ref in session.scalar(select(Item.basis_refs))


@pytest.mark.integration
def test_copied_request_and_operation_do_not_authorize_background_stage_or_commit(
    session, interactive_context
):
    root = AgentRequestService(session).admit(request_key="foreground-only")
    staged = _stage_agent(session, root)
    assert staged["disposition"] == "ready_to_commit"
    request = _stage_request(root)
    digest = sha256_json(request.model_dump(mode="json", exclude_none=True))
    token = _admit_agent(session, root, name="docket_stage_changes", key="stage-1", digest=digest)
    commit_token = _admit_agent(
        session, root, name="docket_commit_changeset", key="commit-1", digest="c" * 64
    )
    context_token = agent_call_context.set(AgentCallContext(_principal("triage")))
    try:
        with pytest.raises(DocketError):
            ChangeSetAssemblyService(session).stage(
                request, assembly_operation_token=token, assembly_argument_hash=digest
            )
        with pytest.raises(DocketError):
            ChangeSetAssemblyService(session).commit(
                request_ref=root.ref_id,
                request_key=root.request_key,
                assembly_operation_token=commit_token,
                assembly_argument_hash="c" * 64,
            )
        assert session.scalar(select(func.count(Item.id))) == 0
    finally:
        agent_call_context.reset(context_token)


@pytest.mark.integration
def test_agent_normalized_entries_allow_corrected_interpretations_without_archive(
    session, interactive_context
):
    root = AgentRequestService(session).admit(request_key="reported-batch")

    def stage(title, key, *, complete=True):
        request = StageChangesInput.model_validate(
            {
                "request_ref": root.ref_id,
                "request_key": root.request_key,
                "assembly_scope": {
                    "resolved_intent": {"title": title},
                    "normalized_entry_types": ["tracked_temporal_entry"],
                    "planned_create_types": ["item", "temporal_binding"],
                    "selected_entry_ids": ["entry-1"] if complete else ["entry-1", "entry-2"],
                },
                "patch": {
                    "operations": [
                        {
                            "operation": "normalized_entry_upsert",
                            "entry": {
                                "entry_type": "tracked_temporal_entry",
                                "import_entry_id": "entry-1",
                                "item": {"title": title, "kind": "test.reported"},
                                "temporal": {
                                    "role": "due_by",
                                    "temporal_value": {
                                        "kind": "date",
                                        "date": "2026-10-15",
                                        "timezone": "America/Los_Angeles",
                                    },
                                },
                                "evidence": {"unavailable_archive": True},
                            },
                        }
                    ]
                },
            }
        )
        digest = sha256_json(request.model_dump(mode="json", exclude_none=True))
        token = _admit_agent(session, root, name="docket_stage_changes", key=key, digest=digest)
        return ChangeSetAssemblyService(session).stage(
            request, assembly_operation_token=token, assembly_argument_hash=digest
        )

    incomplete = stage("Reported title", "incomplete", complete=False)
    assert incomplete["disposition"] != "ready_to_commit", incomplete
    corrected = stage("Corrected title", "correction")
    assert corrected["disposition"] == "ready_to_commit", corrected
    token = _admit_agent(
        session, root, name="docket_commit_changeset", key="commit", digest="c" * 64
    )
    committed = ChangeSetAssemblyService(session).commit(
        request_ref=root.ref_id,
        request_key=root.request_key,
        assembly_operation_token=token,
        assembly_argument_hash="c" * 64,
    )
    assert committed["disposition"] == "committed", committed
    assert session.scalar(select(Item.title)) == "Corrected title"


@pytest.mark.integration
def test_request_cannot_replay_another_requests_committed_changeset(session, interactive_context):
    from docket.schemas.authority import ChangeSetCommit
    from docket.services.change_sets import ChangeSetService

    root = AgentRequestService(session).admit(request_key="owner")
    _stage_agent(session, root)
    token = _admit_agent(
        session, root, name="docket_commit_changeset", key="commit", digest="c" * 64
    )
    ChangeSetAssemblyService(session).commit(
        request_ref=root.ref_id,
        request_key=root.request_key,
        assembly_operation_token=token,
        assembly_argument_hash="c" * 64,
    )
    other = AgentRequestService(session).admit(request_key="copied")
    changeset = session.scalar(select(ChangeSet))
    with pytest.raises(DocketError) as error:
        ChangeSetService(session).commit(
            ChangeSetCommit(
                changeset_ref=changeset.ref_id,
                expected_version=changeset.version,
                idempotency_key=changeset.idempotency_key,
                authority_request_ref=other.ref_id,
            )
        )
    assert error.value.code == "request_authority_denied"


@pytest.mark.asyncio
async def test_mcp_admits_and_commits_with_transport_context_only(
    session_factory, interactive_context
):
    from docket.mcp.instrumented import _result_envelope
    from docket.mcp.server import mcp
    from docket.models import ToolInvocation

    async def call(name, arguments, operation, *, execution="foreground"):
        token = agent_call_context.set(
            AgentCallContext(
                _principal(),
                "mcp-transport-request",
                execution,
                operation,
            )
        )
        try:
            return _result_envelope(await mcp.call_tool(name, arguments))
        finally:
            agent_call_context.reset(token)

    with session_factory.begin() as session:
        root = AgentRequestService(session).admit(request_key="mcp-transport-request")
        args = _stage_request(root).model_dump(mode="json", exclude_none=True)
        args.pop("request_ref")
        args.pop("request_key")
    staged = await call("docket_stage_changes", args, "stage")
    assert staged["disposition"] == "ready_to_commit", staged
    reviewed = await call("docket_review_changeset", {}, "review")
    assert reviewed["ok"], reviewed
    committed = await call("docket_commit_changeset", {}, "commit")
    assert committed["disposition"] == "committed", committed
    replay = await call("docket_commit_changeset", {}, "commit")
    assert replay["changeset_ref"] == committed["changeset_ref"]
    with session_factory.begin() as session:
        assert session.scalar(select(func.count(Item.id))) == 1
        assert session.scalar(select(func.count(ToolInvocation.id))) == 4
        invocations = list(session.scalars(select(ToolInvocation)))
        assert all(row.authenticated_request_ref == root.ref_id for row in invocations)
        assert all(not row.utterance_refs for row in invocations)
        assert session.scalar(select(func.count(OperatorUtterance.id))) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["triage", "read_only"])
async def test_mcp_audits_background_rejection_before_admission(
    session,
    interactive_context,
    role,
):
    from docket.mcp.instrumented import _result_envelope
    from docket.mcp.server import mcp
    from docket.models import ToolInvocation

    token = agent_call_context.set(
        AgentCallContext(
            _principal(role),
            "copied-request",
            "copied-execution",
            "copied-operation",
        )
    )
    try:
        rejected = _result_envelope(await mcp.call_tool("docket_commit_changeset", {}))
        assert rejected["error"]["code"] == "agent_authority_denied"
        row = session.scalar(select(ToolInvocation))
        assert row.domain_state == "rejected"
        assert row.authenticated_request_ref is None
        assert session.scalar(select(func.count(AuthenticatedRequest.id))) == 0
    finally:
        agent_call_context.reset(token)


def test_record_retention_preserves_hash_and_action_history(session_factory, interactive_context):
    from docket.models.base import utc_now
    from docket.services.conversation_retention import ConversationRetentionService
    from docket.services.history import HistoryService

    with session_factory.begin() as session:
        root = AgentRequestService(session).admit(request_key="retained-action")
        record = AgentRequestService(session).record(
            request_ref=root.ref_id,
            record_key="reported",
            record_kind="operator_transcript",
            text="Synthetic retained wording",
        )
        row = session.scalar(select(ConversationRecord))
        digest = row.content_hash
    assert (
        ConversationRetentionService(session_factory, get_settings()).run_once(
            utc_now() + timedelta(days=31),
        )
        == 1
    )
    with session_factory.begin() as session:
        row = session.scalar(select(ConversationRecord))
        assert row.content_hash == digest and row.ciphertext is None and row.purged_at is not None
        history = HistoryService(session).get_entry(record["ref"], view="audit")
        assert "Synthetic retained wording" not in repr(history)
        assert session.scalar(select(AuthenticatedRequest.state)) == "active"


def test_cancelled_request_cannot_resume_or_commit(session_factory, interactive_context):
    with session_factory.begin() as session:
        root = AgentRequestService(session).admit(request_key="cancelled-work")
        _stage_agent(session, root)
        commit_token = _admit_agent(
            session, root, name="docket_commit_changeset", key="commit", digest="c" * 64
        )
        cancelled = AgentRequestService(session).cancel(root.ref_id)
        assert cancelled["state"] == "cancelled"
    with session_factory.begin() as session:
        with pytest.raises(DocketError) as error:
            ChangeSetAssemblyService(session).commit(
                request_ref=root.ref_id,
                request_key=root.request_key,
                assembly_operation_token=commit_token,
                assembly_argument_hash="c" * 64,
            )
        assert error.value.code == "request_authority_denied"
        with pytest.raises(DocketError):
            AgentRequestService(session).admit(request_key=root.request_key)
        assert AgentRequestService(session).cancel(root.ref_id)["state"] == "cancelled"
        assert session.scalar(select(func.count(Item.id))) == 0
        assert (
            session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.event_type == "request.cancelled"
                )
            )
            == 1
        )


def test_request_calendar_date_keeps_admission_zone(session, interactive_context):
    from zoneinfo import ZoneInfo

    from docket.services.event_occurrences import bind_calendar_date

    root = AgentRequestService(session).admit(request_key="relative-date-work")
    instant = (
        root.admitted_at.replace(tzinfo=UTC)
        if root.admitted_at.tzinfo is None
        else root.admitted_at
    )
    expected = instant.astimezone(ZoneInfo(root.admitted_timezone)).date() + timedelta(days=1)
    first = bind_calendar_date(
        session, utterance_ref=root.ref_id, timezone="Pacific/Kiritimati", relative_day="tomorrow"
    )
    repeated = bind_calendar_date(
        session, utterance_ref=root.ref_id, timezone="Pacific/Honolulu", relative_day="tomorrow"
    )
    assert first.date == repeated.date == expected
    assert first.timezone == repeated.timezone == root.admitted_timezone


def test_request_attachment_capture_is_optional_and_honestly_attributed(
    session_factory, interactive_context
):
    from docket.models import AttachmentEvidence, EncryptedAttachmentBlob
    from docket.models.base import utc_now
    from docket.services.attachment_evidence import AttachmentCapture, AttachmentEvidenceService
    from docket.services.history import HistoryService

    with session_factory.begin() as session:
        root = AgentRequestService(session).admit(
            request_key="attachment-work", conversation_ref="synthetic-conversation"
        )
        captures = [
            AttachmentCapture(
                transport_attachment_ref="reported-file",
                filename="context.txt",
                media_type="text/plain",
                byte_size=17,
                plaintext=b"Synthetic context",
                received_at=utc_now(),
            ),
            AttachmentCapture(
                transport_attachment_ref="missing-file",
                filename="unavailable.txt",
                media_type="text/plain",
                byte_size=100,
                plaintext=None,
                received_at=utc_now(),
            ),
        ]
        settings = get_settings()
        service = AttachmentEvidenceService(
            session,
            encryption_key=settings.attachment_encryption_key(),
            encryption_key_ref=settings.attachment_encryption_key_ref,
            max_attachment_bytes=settings.attachment_max_bytes,
            max_total_bytes=settings.attachment_total_max_bytes,
        )
        refs = service.plan_request(root, captures)
        assert service.plan_request(root, captures) == refs
        evidence = list(session.scalars(select(AttachmentEvidence)))
        assert len(evidence) == 2
        assert all(row.authenticated_request_ref == root.ref_id for row in evidence)
        assert all(row.operator_utterance_ref is None for row in evidence)
        assert {row.ingest_state for row in evidence} == {"available", "failed"}
        assert session.scalar(select(func.count(EncryptedAttachmentBlob.id))) == 1
        assert _stage_agent(session, root)["disposition"] == "ready_to_commit"
        commit_token = _admit_agent(
            session, root, name="docket_commit_changeset", key="commit", digest="c" * 64
        )
        assert (
            ChangeSetAssemblyService(session).commit(
                request_ref=root.ref_id,
                request_key=root.request_key,
                assembly_operation_token=commit_token,
                assembly_argument_hash="c" * 64,
            )["disposition"]
            == "committed"
        )
        AgentRequestService(session).record(
            request_ref=root.ref_id,
            record_key="reported",
            record_kind="operator_transcript",
            text="Synthetic conversation wording",
        )
    with session_factory.begin() as session:
        history = HistoryService(session).conversation("synthetic-conversation", view="audit")
        assert {row["type"] for row in history["items"]} == {
            "authenticated_request",
            "conversation_record",
        }
        assert "Synthetic conversation wording" not in repr(history)
        assert session.scalar(select(func.count(Item.id))) == 1


def test_conflict_resolution_uses_request_authority_without_transcript(
    session_factory, interactive_context
):
    from docket.models import Conflict
    from docket.schemas.authority import ConflictOpen, ConflictResolve, StatementInput
    from docket.services.conflicts import ConflictService
    from docket.services.interactive_authority import InteractiveAuthorityService
    from docket.services.statements import StatementService

    with session_factory.begin() as session:
        prior_root = AgentRequestService(session).admit(request_key="prior-fact")
        root = AgentRequestService(session).admit(request_key="resolve-fact")
        subject = "ent_01M13MZZZZZZZZZZZZZZZZZZZZ"

        def statement(value):
            return StatementInput(
                statement_kind="fact_assertion",
                subject_refs=[subject],
                predicate="office_hours",
                value_json=value,
                affected_fields=["office_hours"],
                interpreter_version="test-v1",
            )

        prior = StatementService(session).derive(prior_root.ref_id, [statement("Monday")])[0]
        incoming = StatementService(session).derive(root.ref_id, [statement("Wednesday")])[0]
        conflict = ConflictService(session).open(
            ConflictOpen(
                subject_refs=[subject],
                affected_fields=["office_hours"],
                prior_statement_refs=[prior.ref_id],
                incoming_statement_refs=[incoming.ref_id],
                conflicting_effects_json={"prior": "Monday", "incoming": "Wednesday"},
            )
        )
        kwargs = dict(
            utterance_ref=root.ref_id,
            request_key=root.request_key,
            actor_id=root.principal_ref,
            intent_session_ref=None,
            expected_session_version=None,
            statement=StatementInput(
                statement_kind="conflict_resolution",
                subject_refs=[subject],
                predicate="conflict_resolution",
                value_json={"office_hours": "Wednesday"},
                affected_fields=["office_hours"],
                interpreter_version="test-v1",
            ),
            resolution=ConflictResolve(
                conflict_ref=conflict.ref_id,
                expected_version=1,
                authority_request_ref=root.ref_id,
                resolution="resolved_supersession",
                chosen_interpretation={"office_hours": "Wednesday"},
                statements_superseded=[prior.ref_id],
                statements_retained=[incoming.ref_id],
                effective_scope={},
                canonical_effects=[
                    {
                        "mutation_type": "item_create",
                        "change_id": "resolved-item",
                        "action": "create",
                        "object_type": "item",
                        "affected_fields": ["title"],
                        "create_spec": {"title": "Resolved work", "kind": "test.conflict"},
                    }
                ],
            ),
        )
        result = InteractiveAuthorityService(session).process_conflict_resolution(**kwargs)
        assert result["state"] == "committed", result
        assert root.ref_id in session.scalar(select(Item.basis_refs))
        assert session.scalar(select(Conflict.status)) == "resolved_supersession"
        assert root.state == "committed"
    with session_factory.begin() as session:
        replay = InteractiveAuthorityService(session).process_conflict_resolution(**kwargs)
        assert replay["ref"] == result["ref"]
        assert session.scalar(select(func.count(ChangeSet.id))) == 1
        assert session.scalar(select(func.count(OperatorUtterance.id))) == 0


def test_foreground_dispatch_ownership_survives_duplicate_and_late_archive(
    session_factory, interactive_context
):
    from docket.models import DeferredIngress, ExecutionLease
    from docket.providers.discord import FakeDiscordProjectionAdapter
    from docket.services.continuity import ContinuityService
    from docket.services.deferred_ingress import DeferredIngressRunner
    from docket.services.ingress_ledger import IngressIdentity, IngressLedgerService

    settings = get_settings()
    key = f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:1542799000000000600:0"
    with session_factory.begin() as session:
        service = AgentRequestService(session)
        root = service.admit(request_key=key)
        claimed = service.claim_foreground_execution(root.ref_id, execution_key="foreground")
        replay = service.claim_foreground_execution(root.ref_id, execution_key="foreground")
        assert claimed == replay
        assert service.claim_foreground_execution(root.ref_id, execution_key="duplicate") == {
            "execution_disposition": "already_dispatched"
        }
        ContinuityService(session).complete_execution_lease(claimed["completion_token"])
        assert service.claim_foreground_execution(root.ref_id, execution_key="duplicate") == {
            "execution_disposition": "already_dispatched"
        }
        IngressLedgerService(
            session,
            identity=IngressIdentity(
                operator_id=settings.operator_discord_user_id,
                guild_id=settings.discord_guild_id,
                chat_channel_id=settings.chat_channel_id,
                queue_channel_id=settings.queue_channel_id,
            ),
            signing_key=settings.read_secret(settings.interaction_signing_key_file).encode(),
            attachment_encryption_key=settings.attachment_encryption_key(),
            attachment_encryption_key_ref=settings.attachment_encryption_key_ref,
            attachment_max_bytes=settings.attachment_max_bytes,
            attachment_total_max_bytes=settings.attachment_total_max_bytes,
        ).capture_message(
            actor_id=settings.operator_discord_user_id,
            guild_id=settings.discord_guild_id,
            channel_id=settings.chat_channel_id,
            parent_channel_id=None,
            message_id="1542799000000000600",
            reply_to_message_id=None,
            verbatim_text="Synthetic delayed archive",
            said_at=datetime.now(UTC),
        )
    adapter = FakeDiscordProjectionAdapter()
    assert DeferredIngressRunner(session_factory, adapter).run_once() is True
    with session_factory.begin() as session:
        assert session.scalar(select(DeferredIngress.status)) == "completed"
        assert session.scalar(select(func.count(ExecutionLease.id))) == 1
        assert session.scalar(select(func.count(Item.id))) == 0
    assert DeferredIngressRunner(session_factory, adapter).run_once() is False
