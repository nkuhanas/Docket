from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from docket.config import get_settings
from docket.internal_api.router import semantic_option_selection
from docket.internal_api.schemas import OperatorUtteranceCapture, SemanticOptionSelection
from docket.models import (
    AgentResponse,
    ChangeSet,
    DiscordMcpTrace,
    Entity,
    GatewayLifetime,
    OperatorUtterance,
    OutboxEvent,
    PersistedSemanticOption,
    SemanticPromptProjection,
    SemanticRequest,
    SemanticRequestAttempt,
    ToolInvocation,
)
from docket.providers.discord import FakeDiscordProjectionAdapter
from docket.schemas.authority import SemanticOptionDraft
from docket.security import issue_semantic_option_token
from docket.services.deferred_ingress import DeferredIngressRunner
from docket.services.gateway_lifetimes import GatewayLifetimeService
from docket.services.ingress_deployment import IngressDeploymentService
from docket.services.ingress_ledger import IngressIdentity, IngressLedgerService
from docket.services.interactive_authority import InteractiveAuthorityService
from docket.services.provenance import ProvenanceService
from docket.services.semantic_options import CURRENT_SELECTION_UTTERANCE


def _capture_utterance(session, *, message_id: str, text: str) -> str:
    settings = get_settings()
    return str(
        ProvenanceService(session).capture_operator_utterance(
            OperatorUtteranceCapture(
                request_id=uuid.uuid4(),
                guild_id=settings.discord_guild_id,
                channel_id=settings.chat_channel_id,
                message_id=message_id,
                actor_id=settings.operator_discord_user_id,
                verbatim_text=text,
                request_key=(
                    f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
                    f"{message_id}:0"
                ),
            )
        )["ref"]
    )


def _entity_option(authority_ref: str) -> SemanticOptionDraft:
    return SemanticOptionDraft.model_validate(
        {
            "option_id": "create-cal-poly",
            "selection_authority_ref": authority_ref,
            "content": {
                "basis_refs": [authority_ref],
                "registry_changes": [
                    {
                        "mutation_type": "entity_create",
                        "change_id": "create-cal-poly",
                        "action": "create",
                        "object_type": "entity",
                        "create_spec": {
                            "entity_kind": "institution",
                            "display_name": "Cal Poly",
                        },
                        "affected_fields": ["identity"],
                        "basis_refs": [authority_ref],
                    }
                ],
            },
        }
    )


def _invalid_dependency_option(authority_ref: str) -> SemanticOptionDraft:
    return SemanticOptionDraft.model_validate(
        {
            "option_id": "create-orphaned-institution",
            "selection_authority_ref": authority_ref,
            "content": {
                "basis_refs": [authority_ref],
                "registry_changes": [
                    {
                        "mutation_type": "entity_create",
                        "change_id": "create-cal-poly",
                        "action": "create",
                        "object_type": "entity",
                        "create_spec": {
                            "entity_kind": "institution",
                            "display_name": "Cal Poly",
                            "parent_entity_change_id": "missing-parent",
                        },
                        "affected_fields": ["identity", "hierarchy"],
                        "basis_refs": [authority_ref],
                    }
                ],
            },
        }
    )


@pytest.mark.integration
def test_ingress_handoff_quiesces_and_regenerates_exact_semantic_options(
    session_factory,
) -> None:
    settings = get_settings()
    adapter = FakeDiscordProjectionAdapter()
    with session_factory.begin() as session:
        utterance_ref = _capture_utterance(
            session,
            message_id="1542799000000000190",
            text="Offer the exact Cal Poly registration choice.",
        )
        InteractiveAuthorityService(session).process_turn(
            utterance_ref=utterance_ref,
            request_key=(
                f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
                "1542799000000000190:0"
            ),
            actor_id=settings.operator_discord_user_id,
            intent_session_ref=None,
            expected_session_version=None,
            statements=[],
            relations=[],
            resolved_intent_json={"kind": "identity_clarification"},
            blocking_clarifications=[
                {
                    "blocking": True,
                    "code": "identity_resolution_required",
                    "question": "Should Docket register Cal Poly?",
                }
            ],
            content=None,
            changeset_ref=None,
            expected_changeset_version=None,
            semantic_options=[_entity_option(utterance_ref)],
        )
        prompt = session.scalar(select(SemanticPromptProjection))
        option = session.scalar(select(PersistedSemanticOption))
        assert prompt is not None and option is not None
        prompt.status = "delivered"
        prompt.message_id = "1542799000000000191"
        original_ref = prompt.ref_id
        original_authority_hash = option.authority_scope_hash
        original_precondition_hash = option.precondition_hash
        original_visible_text = option.visible_text
        adapter.backend.semantic_prompts[str(prompt.id)] = {
            "message_id": prompt.message_id,
            "channel_id": prompt.channel_id,
            "controls": [{"custom_id": "persisted"}],
        }

    service = IngressDeploymentService(session_factory, adapter)
    assert service.quiesce()["projection_refs"] == [original_ref]
    assert service.regenerate()["count"] == 1

    with session_factory.begin() as session:
        prompts = list(
            session.scalars(
                select(SemanticPromptProjection).order_by(
                    SemanticPromptProjection.projection_version
                )
            )
        )
        assert len(prompts) == 2
        assert prompts[0].status == "superseded"
        assert prompts[0].last_error_code == "ingress_deployment_regenerated"
        assert prompts[1].status == "pending"
        replacement = session.scalar(
            select(PersistedSemanticOption).where(
                PersistedSemanticOption.prompt_projection_id == prompts[1].id
            )
        )
        assert replacement is not None
        assert replacement.authority_scope_hash == original_authority_hash
        assert replacement.precondition_hash != original_precondition_hash
        assert replacement.visible_text == original_visible_text
        assert session.scalar(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == prompts[1].id)
        ) is not None
    assert adapter.backend.semantic_prompts[str(prompts[0].id)]["controls"] == []


@pytest.mark.integration
def test_persisted_selection_compiles_once_and_preserves_exact_authority(
    session_factory,
) -> None:
    settings = get_settings()
    initial_message_id = "1542799000000000200"
    interaction_id = "1542799000000000201"
    prompt_message_id = "1542799000000000202"

    with session_factory.begin() as session:
        initial_utterance_ref = _capture_utterance(
            session,
            message_id=initial_message_id,
            text="Create Cal Poly or leave the sender unresolved.",
        )
        result = InteractiveAuthorityService(session).process_turn(
            utterance_ref=initial_utterance_ref,
            request_key=(
                f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
                f"{initial_message_id}:0"
            ),
            actor_id=settings.operator_discord_user_id,
            intent_session_ref=None,
            expected_session_version=None,
            statements=[],
            relations=[],
            resolved_intent_json={"kind": "identity_clarification"},
            blocking_clarifications=[
                {
                    "blocking": True,
                    "code": "identity_resolution_required",
                    "question": "Should Docket register Cal Poly?",
                }
            ],
            content=None,
            changeset_ref=None,
            expected_changeset_version=None,
            semantic_options=[_entity_option(initial_utterance_ref)],
        )
        assert result["disposition"] == "needs_clarification"
        prompt = session.scalar(select(SemanticPromptProjection))
        option = session.scalar(select(PersistedSemanticOption))
        assert prompt is not None and option is not None
        assert prompt.message_id is None
        assert option.visible_text == "Create institution “Cal Poly”."
        assert CURRENT_SELECTION_UTTERANCE in str(option.compilation_template_json)
        original_authority_hash = option.authority_scope_hash
        original_precondition_hash = option.precondition_hash
        prompt.message_id = prompt_message_id
        prompt.status = "delivered"
        token = issue_semantic_option_token(
            option_row_id=option.id,
            projection_version=option.prompt_projection_version,
            actor_id=settings.operator_discord_user_id,
            expires_at=datetime.now(UTC) + timedelta(days=1),
            signing_key=settings.read_secret(
                settings.interaction_signing_key_file
            ).encode(),
        )

    selection_payload = SemanticOptionSelection(
        request_id=uuid.uuid4(),
        discord_interaction_id=interaction_id,
        discord_user_id=settings.operator_discord_user_id,
        guild_id=settings.discord_guild_id,
        channel_id=settings.chat_channel_id,
        message_id=prompt_message_id,
        option_token=token,
        responded_at=datetime.now(UTC),
    )
    selection = semantic_option_selection(selection_payload)
    selected_utterance_ref = str(selection["utterance_ref"])
    assert selection["authority_scope_hash"] == original_authority_hash
    assert selection["precondition_hash"] == original_precondition_hash
    assert selected_utterance_ref in str(selection["compiled_content"])
    assert initial_utterance_ref not in str(selection["compiled_content"])
    assert selection["state"] == "committed"
    assert selection["response_text"] == "Done — Create institution “Cal Poly”."
    replay = semantic_option_selection(selection_payload)
    assert replay["utterance_ref"] == selected_utterance_ref
    assert replay["semantic_request_ref"] == selection["semantic_request_ref"]
    assert replay["response_ref"] == selection["response_ref"]

    with session_factory.begin() as session:
        utterance = session.scalar(
            select(OperatorUtterance).where(
                OperatorUtterance.ref_id == selected_utterance_ref
            )
        )
        semantic_request = session.scalar(
            select(SemanticRequest).where(
                SemanticRequest.ref_id == selection["semantic_request_ref"]
            )
        )
        assert utterance is not None and semantic_request is not None
        assert utterance.utterance_kind == "button_selection"
        assert utterance.verbatim_text == "Create institution “Cal Poly”."
        assert utterance.authority_scope_hash == original_authority_hash
        assert utterance.selected_precondition_hash == original_precondition_hash
        assert semantic_request.authority_availability == "consumed_committed"
        response = session.scalar(
            select(AgentResponse).where(AgentResponse.ref_id == selection["response_ref"])
        )
        assert response is not None
        assert response.responds_to_utterance_refs == [selected_utterance_ref]
        assert response.verbatim_text == selection["response_text"]
        assert semantic_request.committed_changeset_ref == selection["execution"]["ref"]
        assert session.scalar(select(func.count(ChangeSet.id))) == 1
        assert session.scalar(select(func.count(SemanticRequestAttempt.id))) == 1
        assert session.scalar(select(func.count(Entity.id))) == 1


@pytest.mark.integration
def test_selection_validation_failure_preserves_authority_without_duplicate_attempt(
    session_factory,
) -> None:
    settings = get_settings()
    with session_factory.begin() as session:
        initial_ref = _capture_utterance(
            session,
            message_id="1542799000000000205",
            text="Offer the Cal Poly creation choice.",
        )
        InteractiveAuthorityService(session).process_turn(
            utterance_ref=initial_ref,
            request_key=(
                f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
                "1542799000000000205:0"
            ),
            actor_id=settings.operator_discord_user_id,
            intent_session_ref=None,
            expected_session_version=None,
            statements=[],
            relations=[],
            resolved_intent_json={"kind": "identity_clarification"},
            blocking_clarifications=[
                {
                    "blocking": True,
                    "code": "identity_resolution_required",
                    "question": "Should Docket register Cal Poly?",
                }
            ],
            content=None,
            changeset_ref=None,
            expected_changeset_version=None,
            semantic_options=[_invalid_dependency_option(initial_ref)],
        )
        prompt = session.scalar(select(SemanticPromptProjection))
        option = session.scalar(select(PersistedSemanticOption))
        assert prompt is not None and option is not None
        prompt.message_id = "1542799000000000206"
        prompt.status = "delivered"
        token = issue_semantic_option_token(
            option_row_id=option.id,
            projection_version=option.prompt_projection_version,
            actor_id=settings.operator_discord_user_id,
            expires_at=datetime.now(UTC) + timedelta(days=1),
            signing_key=settings.read_secret(settings.interaction_signing_key_file).encode(),
        )

    payload = SemanticOptionSelection(
        request_id=uuid.uuid4(),
        discord_interaction_id="1542799000000000207",
        discord_user_id=settings.operator_discord_user_id,
        guild_id=settings.discord_guild_id,
        channel_id=settings.chat_channel_id,
        message_id="1542799000000000206",
        option_token=token,
        responded_at=datetime.now(UTC),
    )
    first = semantic_option_selection(payload)
    second = semantic_option_selection(payload.model_copy(update={"request_id": uuid.uuid4()}))
    assert first["state"] == second["state"] == "blocked_validation"
    assert first["utterance_ref"] == second["utterance_ref"]
    assert first["semantic_request_ref"] == second["semantic_request_ref"]

    retry_payload = payload.model_copy(
        update={
            "request_id": uuid.uuid4(),
            "resume_authorized_execution": True,
        }
    )
    retry = semantic_option_selection(retry_payload)
    retry_replay = semantic_option_selection(retry_payload)
    assert retry["state"] == retry_replay["state"] == "blocked_validation"
    assert retry["utterance_ref"] == first["utterance_ref"]
    assert retry["semantic_request_ref"] == first["semantic_request_ref"]

    with session_factory.begin() as session:
        semantic_request = session.scalar(select(SemanticRequest))
        assert semantic_request is not None
        assert semantic_request.authority_availability == "available"
        assert semantic_request.commit_state == "blocked_validation"
        assert session.scalar(select(func.count(SemanticRequestAttempt.id))) == 2
        assert session.scalar(select(func.count(OperatorUtterance.id))) == 2
        assert session.scalar(select(func.count(Entity.id))) == 0


@pytest.mark.integration
def test_stable_ingress_selection_persists_before_worker_execution(session_factory) -> None:
    settings = get_settings()
    initial_message_id = "1542799000000000210"
    interaction_id = "1542799000000000211"
    prompt_message_id = "1542799000000000212"
    with session_factory.begin() as session:
        initial_utterance_ref = _capture_utterance(
            session,
            message_id=initial_message_id,
            text="Create Cal Poly.",
        )
        InteractiveAuthorityService(session).process_turn(
            utterance_ref=initial_utterance_ref,
            request_key=(
                f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
                f"{initial_message_id}:0"
            ),
            actor_id=settings.operator_discord_user_id,
            intent_session_ref=None,
            expected_session_version=None,
            statements=[],
            relations=[],
            resolved_intent_json={"kind": "identity_clarification"},
            blocking_clarifications=[
                {
                    "blocking": True,
                    "code": "identity_resolution_required",
                    "question": "Should Docket register Cal Poly?",
                }
            ],
            content=None,
            changeset_ref=None,
            expected_changeset_version=None,
            semantic_options=[_entity_option(initial_utterance_ref)],
        )
        prompt = session.scalar(select(SemanticPromptProjection))
        option = session.scalar(select(PersistedSemanticOption))
        assert prompt is not None and option is not None
        prompt.message_id = prompt_message_id
        prompt.status = "delivered"
        token = issue_semantic_option_token(
            option_row_id=option.id,
            projection_version=option.prompt_projection_version,
            actor_id=settings.operator_discord_user_id,
            expires_at=datetime.now(UTC) + timedelta(days=1),
            signing_key=settings.read_secret(settings.interaction_signing_key_file).encode(),
        )
        recorded = IngressLedgerService(
            session,
            identity=IngressIdentity(
                operator_id=settings.operator_discord_user_id,
                guild_id=settings.discord_guild_id,
                chat_channel_id=settings.chat_channel_id,
                queue_channel_id=settings.queue_channel_id,
            ),
            signing_key=settings.read_secret(settings.interaction_signing_key_file).encode(),
        ).capture_semantic_selection(
            actor_id=settings.operator_discord_user_id,
            guild_id=settings.discord_guild_id,
            channel_id=settings.chat_channel_id,
            parent_channel_id=None,
            interaction_id=interaction_id,
            message_id=prompt_message_id,
            option_token=token,
            responded_at=datetime.now(UTC),
        )
        assert recorded["state"] == "pending"
        assert session.scalar(select(func.count(SemanticRequest.id))) == 0
        assert session.scalar(select(func.count(Entity.id))) == 0

    adapter = FakeDiscordProjectionAdapter()
    assert DeferredIngressRunner(session_factory, adapter).run_once() is True
    assert len(adapter.deferred_ingress) == 1
    binding = adapter.deferred_ingress[0]["selected_option_binding"]
    selection_payload = SemanticOptionSelection.model_validate(
        {
            key: value
            for key, value in binding.items()
            if key
            in {
                "discord_interaction_id",
                "discord_user_id",
                "guild_id",
                "channel_id",
                "parent_channel_id",
                "message_id",
                "option_token",
                "responded_at",
            }
        }
        | {"request_id": str(uuid.uuid4())}
    )
    result = semantic_option_selection(selection_payload)
    assert result["state"] == "committed"
    with session_factory.begin() as session:
        assert session.scalar(select(func.count(OperatorUtterance.id))) == 2
        assert session.scalar(select(func.count(SemanticRequest.id))) == 1
        assert session.scalar(select(func.count(Entity.id))) == 1


@pytest.mark.integration
def test_expired_gateway_reconciles_terminal_and_unknown_call_outcomes(
    session_factory,
) -> None:
    trace_id = uuid.uuid4()
    registration_key = uuid.uuid4()
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        registered = GatewayLifetimeService(session).register(
            registration_key=registration_key,
            instance_kind="hermes_discord_gateway",
        )
        replay = GatewayLifetimeService(session).register(
            registration_key=registration_key,
            instance_kind="hermes_discord_gateway",
        )
        assert replay["ref"] == registered["ref"]
        gateway = session.scalar(
            select(GatewayLifetime).where(GatewayLifetime.ref_id == registered["ref"])
        )
        assert gateway is not None
        gateway.lease_expires_at = now - timedelta(seconds=1)
        trace = DiscordMcpTrace(
            id=trace_id,
            guild_id="000000000000000002",
            source_channel_id="000000000000000003",
            source_message_id="1542799000000000300",
            actor_id="000000000000000001",
            tool_contract_version="test",
            tool_contract_hash="0" * 64,
            caller_profile="interactive",
            gateway_instance_ref=gateway.ref_id,
            status="running",
            calls=[
                {
                    "call_id": "committed-call",
                    "ordinal": 1,
                    "tool_name": "docket_commit_changeset",
                    "transport_state": "running",
                },
                {
                    "call_id": "unknown-call",
                    "ordinal": 2,
                    "tool_name": "docket_commit_changeset",
                    "transport_state": "running",
                },
            ],
            last_ordinal=2,
            started_at=now,
        )
        session.add(trace)
        session.add_all(
            [
                ToolInvocation(
                    tool_name="docket_commit_changeset",
                    tool_contract_version="test",
                    caller_profile="interactive",
                    status="succeeded",
                    transport_state="completed",
                    domain_state="succeeded",
                    received_argument_hash="a" * 64,
                    normalized_argument_hash="a" * 64,
                    result_disposition="committed",
                    completed_at=now,
                    trace_id=trace_id,
                    trace_call_id="committed-call",
                    trace_ordinal=1,
                    gateway_instance_ref=gateway.ref_id,
                ),
                ToolInvocation(
                    tool_name="docket_commit_changeset",
                    tool_contract_version="test",
                    caller_profile="interactive",
                    status="received",
                    transport_state="running",
                    domain_state="unknown",
                    received_argument_hash="b" * 64,
                    trace_id=trace_id,
                    trace_call_id="unknown-call",
                    trace_ordinal=2,
                    gateway_instance_ref=gateway.ref_id,
                ),
            ]
        )

    with session_factory.begin() as session:
        expired = GatewayLifetimeService(session).expire_and_reconcile()
        assert expired == [registered["ref"]]

    with session_factory() as session:
        gateway = session.scalar(select(GatewayLifetime))
        trace = session.get(DiscordMcpTrace, trace_id)
        invocations = {
            item.trace_call_id: item
            for item in session.scalars(select(ToolInvocation))
        }
        assert gateway is not None and gateway.status == "expired"
        assert trace is not None and trace.status == "interrupted"
        assert trace.calls[0]["domain_state"] == "succeeded"
        assert trace.calls[0]["disposition"] == "committed"
        unknown = invocations["unknown-call"]
        assert unknown.transport_state == "timed_out"
        assert unknown.domain_state == "unknown"
        assert unknown.result_disposition == "unknown"
        assert trace.calls[1]["domain_state"] == "unknown"
        assert trace.calls[1]["disposition"] == "unknown"
