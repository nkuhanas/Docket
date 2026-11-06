"""A standing policy imports a Task without manufacturing another user turn."""

import uuid
from datetime import date

import pytest
from sqlalchemy import func, select

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.models import (
    AuditEvent,
    AuthenticatedRequest,
    ChangeSet,
    GoogleTaskList,
    GoogleTaskObservation,
    IntentSession,
    Item,
    OAuthPrincipal,
    OperatorUtterance,
    ProviderAccount,
    Task,
    TaskListPolicyRevision,
    TaskPolicyEffect,
    TemporalBinding,
)
from docket.schemas.authority import ChangeSetCommit, ChangeSetContent, ChangeSetPrepare
from docket.services.change_sets import ChangeSetService, _content_payload
from docket.services.tracked_context import TrackedContextService


@pytest.fixture(autouse=True)
def enabled_tasks(monkeypatch):
    monkeypatch.setattr(get_settings(), "tasks_enabled", True)


def admitted_import(session, *, oauth=False):
    owner = get_settings().canonical_operator_ref
    principal_ref, epoch = "agent:interactive", None
    if oauth:
        principal = OAuthPrincipal(
            issuer=get_settings().plugin_oauth_issuer,
            subject="synthetic-task-owner",
            operator_ref=owner,
            scopes=["docket.read", "docket.write"],
            enabled=True,
        )
        session.add(principal)
        session.flush()
        principal_ref, epoch = principal.ref_id, principal.valid_after
    # This fixture represents the single authentic configuration request.
    request = AuthenticatedRequest(
        principal_ref=principal_ref,
        operator_ref=owner,
        role="interactive",
        permissions=["read", "stage", "commit"],
        request_key="fixture:configure-tasks",
        conversation_ref="fixture:configuration",
        state="committed",
    )
    session.add(request)
    session.flush()
    original = IntentSession(
        conversation_ref="fixture:configuration",
        source_request_ref=request.ref_id,
        semantic_state="ready",
    )
    session.add(original)
    session.flush()
    config = ChangeSet(
        intent_session_id=original.id,
        intent_session_ref=original.ref_id,
        idempotency_key="fixture:configured-policy",
        state="committed",
    )
    account = ProviderAccount(
        provider="google", external_account_id="synthetic-tasks", capabilities=["google_tasks"]
    )
    session.add_all([config, account])
    session.flush()
    listing = GoogleTaskList(
        operator_ref=owner,
        account_id=account.id,
        provider_list_id="synthetic-list",
        title="Validation",
        policy_mode="two_way",
    )
    session.add(listing)
    session.flush()
    policy = TaskListPolicyRevision(
        task_list_ref=listing.ref_id,
        revision=1,
        operator_ref=owner,
        mode="two_way",
        propagate_google_deletion=True,
        authorized_request_ref=request.ref_id,
        principal_ref=request.principal_ref,
        grant_epoch=epoch,
        created_by_changeset_ref=config.ref_id,
    )
    observed = GoogleTaskObservation(
        task_list_ref=listing.ref_id,
        provider_task_id="synthetic-id",
        source_hash="1" * 64,
        inventory_generation=uuid.uuid4(),
        fields={"title": "A" * 1024, "status": "needsAction", "due": "2026-11-13T00:00:00Z"},
    )
    session.add_all([policy, observed])
    session.flush()
    listing.policy_ref = policy.ref_id
    effect_ref = new_public_ref("teff")
    basis = [effect_ref, policy.ref_id, observed.ref_id]
    content = ChangeSetContent.model_validate(
        {
            "basis_refs": basis,
            "tracked_context_changes": [
                {
                    "mutation_type": "task_create",
                    "change_id": "import-task",
                    "action": "create",
                    "object_type": "task",
                    "basis_refs": basis,
                    "affected_fields": ["title"],
                    "create_spec": {
                        "task_list_ref": listing.ref_id,
                        "title": "A" * 1024,
                        "scheduled_date": "2026-11-13",
                    },
                }
            ],
        }
    )
    payload = _content_payload(content)
    effect = TaskPolicyEffect(
        ref_id=effect_ref,
        policy_ref=policy.ref_id,
        observation_ref=observed.ref_id,
        effect_hash=sha256_json(payload),
        content=payload,
        planner_version=1,
    )
    session.add(effect)
    session.flush()
    intent = IntentSession(
        conversation_ref="task-sync:" + listing.ref_id,
        source_policy_effect_ref=effect.ref_id,
        semantic_state="ready",
    )
    session.add(intent)
    session.flush()
    service = ChangeSetService(session, handlers=TrackedContextService(session).handlers())
    prepared, _ = service.prepare(
        ChangeSetPrepare(
            intent_session_ref=intent.ref_id,
            expected_session_version=intent.version,
            idempotency_key="task-import:" + effect.ref_id,
            content=content,
        )
    )
    return service, prepared, effect, listing


def test_policy_import_uses_shared_engine_and_genuine_service_root(session):
    service, prepared, effect, listing = admitted_import(session)
    assert prepared.state == "validated", prepared.validation_errors
    committed, receipt = service.commit(
        ChangeSetCommit(
            changeset_ref=prepared.ref_id,
            expected_version=prepared.version,
            idempotency_key=prepared.idempotency_key,
            authority_policy_effect_ref=effect.ref_id,
        )
    )
    assert committed.state == "committed"
    task = session.scalar(select(Task))
    assert task.title == "A" * 1024 and task.task_list_ref == listing.ref_id
    assert session.scalar(select(Item.title)) == task.title
    assert task.scheduled_date == date(2026, 11, 13)
    assert session.scalar(select(func.count()).select_from(TemporalBinding)) == 0
    assert task.ref_id in receipt.affected_refs
    assert session.scalar(select(func.count()).select_from(AuthenticatedRequest)) == 1
    assert session.scalar(select(func.count()).select_from(OperatorUtterance)) == 0
    audit = session.scalar(select(AuditEvent).where(AuditEvent.event_type == "changeset.committed"))
    assert audit.actor_type == "task_sync_policy" and audit.actor_id == effect.policy_ref


@pytest.mark.parametrize(
    "revocation", ["list", "account", "local_grant", "oauth_grant", "tasks_switch"]
)
def test_revocation_between_preparation_and_commit_stops_import(session, monkeypatch, revocation):
    if revocation == "oauth_grant":
        monkeypatch.setattr(get_settings(), "plugin_oauth_issuer", "https://issuer.example.test/")
    service, prepared, effect, listing = admitted_import(session, oauth=revocation == "oauth_grant")
    if revocation == "list":
        listing.policy_mode = "disabled"
    elif revocation == "account":
        session.get(ProviderAccount, listing.account_id).enabled = False
    elif revocation == "local_grant":
        monkeypatch.setattr(get_settings(), "interactive_agent_enabled", False)
    elif revocation == "tasks_switch":
        monkeypatch.setattr(get_settings(), "tasks_enabled", False)
    else:
        session.scalar(select(OAuthPrincipal)).enabled = False
    with pytest.raises(DocketError) as caught:
        service.commit(
            ChangeSetCommit(
                changeset_ref=prepared.ref_id,
                expected_version=prepared.version,
                idempotency_key=prepared.idempotency_key,
                authority_policy_effect_ref=effect.ref_id,
            )
        )
    assert caught.value.code == "task_sync_policy_denied"
    assert session.scalar(select(func.count()).select_from(Task)) == 0
    assert session.scalar(select(func.count()).select_from(AuthenticatedRequest)) == 1


def test_triage_caller_cannot_borrow_a_standing_policy_effect(session):
    from docket.agent_auth import AgentCallContext, AgentPrincipal, agent_call_context

    service, prepared, effect, _listing = admitted_import(session)
    context = agent_call_context.set(
        AgentCallContext(
            AgentPrincipal(
                "agent:triage",
                get_settings().canonical_operator_ref,
                "triage",
                frozenset({"read", "triage"}),
            )
        )
    )
    try:
        with pytest.raises(DocketError) as caught:
            service.commit(
                ChangeSetCommit(
                    changeset_ref=prepared.ref_id,
                    expected_version=prepared.version,
                    idempotency_key=prepared.idempotency_key,
                    authority_policy_effect_ref=effect.ref_id,
                )
            )
        assert caught.value.code == "agent_authority_denied"
        assert session.scalar(select(func.count()).select_from(Task)) == 0
    finally:
        agent_call_context.reset(context)


def test_policy_effect_rejects_a_different_payload(session):
    service, prepared, _effect, _listing = admitted_import(session)
    content = service.verify_execution_revision(prepared)
    change = content.tracked_context_changes[0]
    altered = content.model_copy(
        update={
            "tracked_context_changes": [
                change.model_copy(
                    update={
                        "create_spec": change.create_spec.model_copy(
                            update={"title": "Unobserved content"}
                        ),
                    }
                )
            ]
        }
    )
    intent = session.get(IntentSession, prepared.intent_session_id)
    rejected, _ = service.prepare(
        ChangeSetPrepare(
            intent_session_ref=intent.ref_id,
            expected_session_version=intent.version,
            idempotency_key="different-effect-attempt",
            content=altered,
        )
    )
    assert rejected.state != "validated"
    assert "task_sync_policy_denied" in {error["code"] for error in rejected.validation_errors}
    assert session.scalar(select(func.count()).select_from(Task)) == 0
