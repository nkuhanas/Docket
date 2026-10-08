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
