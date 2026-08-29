from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.internal_api.router import semantic_option_selection
from docket.internal_api.schemas import OperatorUtteranceCapture, SemanticOptionSelection
from docket.models import (
    AgentResponse,
    AttentionCase,
    AttentionCaseRevision,
    AuditEvent,
    CaseItem,
    ChangeSet,
    Conflict,
    DiscordMcpTrace,
    Entity,
    GatewayLifetime,
    IdentityBinding,
    IdentityHandle,
    IntentSession,
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
from docket.services.semantic_options import CURRENT_SELECTION_UTTERANCE, SemanticOptionService


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


def _cal_poly_case_option(
    authority_ref: str,
    *,
    identity_ref: str,
    case_ref: str,
    case_revision_ref: str,
    case_item_ref: str,
    identity_version: int,
    case_version: int,
) -> SemanticOptionDraft:
    return SemanticOptionDraft.model_validate(
        {
            "option_id": "create-cal-poly-and-bind",
            "selection_authority_ref": authority_ref,
            "content": {
                "basis_refs": [authority_ref],
                "expected_versions": {
                    identity_ref: identity_version,
                    case_ref: case_version,
                },
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
                    },
                    {
                        "mutation_type": "identity_binding_bind",
                        "change_id": "bind-eadvise",
                        "action": "bind",
                        "object_type": "identity_binding",
                        "object_ref": identity_ref,
                        "payload": {
                            "entity_change_id": "create-cal-poly",
                            "resolution_basis": {
                                "kind": "operator_selection",
                                "utterance_ref": authority_ref,
                            },
                        },
                        "affected_fields": ["identity_binding"],
                        "basis_refs": [authority_ref],
                    },
                ],
                "resolution_changes": [
                    {
                        "mutation_type": "attention_case_resolution",
                        "change_id": "resolve-sender-identity",
                        "action": "update",
                        "object_type": "attention_case_resolution",
                        "object_ref": case_ref,
                        "case_revision_ref": case_revision_ref,
                        "case_outcome": "resolved",
                        "item_dispositions": [
                            {"item_ref": case_item_ref, "disposition": "resolved"}
                        ],
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
def test_old_option_safe_rebase_preserves_immutable_selection_evidence(
    session_factory,
) -> None:
    settings = get_settings()
    adapter = FakeDiscordProjectionAdapter()
    with session_factory.begin() as session:
        initial_ref = _capture_utterance(
            session,
            message_id="1542799000000000192",
            text="Offer a revision-bound Cal Poly identity option.",
        )
        identity = IdentityHandle(
            handle_type="email",
            value="eadvise@calpoly.edu",
            normalized_value="eadvise@calpoly.edu",
            status="unbound",
            basis_refs=[initial_ref],
        )
        case = AttentionCase(
            situation_key="safe-rebase-cal-poly",
            title="Resolve a revision-bound sender",
            summary="The same required item survives a case refresh.",
            semantic_classes=["registry_candidate"],
            first_observed_at=datetime.now(UTC),
            last_observed_at=datetime.now(UTC),
        )
        session.add_all([identity, case])
        session.flush()
        item = CaseItem(
            attention_case_id=case.id,
            item_key="sender-identity",
            item_type="identity_resolution",
            resolution_role="required",
            basis_refs=[initial_ref],
        )
        session.add(item)
        session.flush()
        revision_one = AttentionCaseRevision(
            attention_case_id=case.id,
            case_ref=case.ref_id,
            revision=1,
            title=case.title,
            summary=case.summary,
            semantic_classes=list(case.semantic_classes),
            item_refs=[item.ref_id],
            source_refs=[],
            content_hash="3" * 64,
        )
        session.add(revision_one)
        session.flush()
        intent = IntentSession(
            conversation_ref=(
                f"discord_conversation:{settings.discord_guild_id}:"
                f"{settings.chat_channel_id}"
            ),
            source_utterance_ref=initial_ref,
            case_refs=[case.ref_id],
            case_revision_refs=[revision_one.ref_id],
            trusted_context_refs=[],
            blocking_clarifications=[
                {
                    "blocking": True,
                    "code": "identity_resolution_required",
                    "question": "Create Cal Poly and bind this sender?",
                }
            ],
            state="needs_clarification",
            semantic_state="needs_clarification",
        )
        session.add(intent)
        session.flush()
        original = SemanticOptionService(session).persist_prompt(
            utterance=session.scalar(
                select(OperatorUtterance).where(OperatorUtterance.ref_id == initial_ref)
            ),
            intent_session=intent,
            question="Create Cal Poly and bind this sender?",
            drafts=[
                _cal_poly_case_option(
                    initial_ref,
                    identity_ref=identity.ref_id,
                    case_ref=case.ref_id,
                    case_revision_ref=revision_one.ref_id,
                    case_item_ref=item.ref_id,
                    identity_version=identity.version,
                    case_version=case.version,
                )
            ],
        )
        old_option = session.scalar(
            select(PersistedSemanticOption).where(
                PersistedSemanticOption.prompt_projection_id == original.id
            )
        )
        assert old_option is not None
        original.message_id = "1542799000000000193"
        original.status = "delivered"
        old_authority_hash = old_option.authority_scope_hash
        old_precondition_hash = old_option.precondition_hash
        old_revision_ref = revision_one.ref_id
        old_token = issue_semantic_option_token(
            option_row_id=old_option.id,
            projection_version=old_option.prompt_projection_version,
            actor_id=settings.operator_discord_user_id,
            expires_at=datetime.now(UTC) + timedelta(days=1),
            signing_key=settings.read_secret(
                settings.interaction_signing_key_file
            ).encode(),
        )
        adapter.backend.semantic_prompts[str(original.id)] = {
            "message_id": original.message_id,
            "channel_id": original.channel_id,
            "controls": [{"custom_id": "persisted"}],
        }
        revision_two = AttentionCaseRevision(
            attention_case_id=case.id,
            case_ref=case.ref_id,
            revision=2,
            title=case.title,
            summary=case.summary + " Refreshed without semantic change.",
            semantic_classes=list(case.semantic_classes),
            item_refs=[item.ref_id],
            source_refs=[],
            content_hash="4" * 64,
        )
        session.add(revision_two)
        session.flush()
        case.latest_revision = 2
        case.version += 1
        intent.case_revision_refs = [revision_two.ref_id]
        intent.version += 1
        new_revision_ref = revision_two.ref_id

    deployment = IngressDeploymentService(session_factory, adapter)
    assert deployment.quiesce()["count"] == 1
    assert deployment.regenerate()["count"] == 1

    with session_factory.begin() as session:
        new_option = session.scalar(
            select(PersistedSemanticOption)
            .where(PersistedSemanticOption.prompt_projection_version == 2)
        )
        assert new_option is not None
        assert new_option.authority_scope_hash == old_authority_hash
        assert new_option.precondition_hash != old_precondition_hash
        assert new_option.case_revision_ref == new_revision_ref
        new_precondition_hash = new_option.precondition_hash

    result = semantic_option_selection(
        SemanticOptionSelection(
            request_id=uuid.uuid4(),
            discord_interaction_id="1542799000000000194",
            discord_user_id=settings.operator_discord_user_id,
            guild_id=settings.discord_guild_id,
            channel_id=settings.chat_channel_id,
            message_id="1542799000000000193",
            option_token=old_token,
            responded_at=datetime.now(UTC),
        )
    )
    assert result["state"] == "committed"
    assert result["authority_scope_hash"] == old_authority_hash
    assert result["precondition_hash"] == new_precondition_hash
    assert result["case_revision_ref"] == new_revision_ref

    with session_factory.begin() as session:
        utterance = session.scalar(
            select(OperatorUtterance).where(
                OperatorUtterance.ref_id == result["utterance_ref"]
            )
        )
        semantic_request = session.scalar(select(SemanticRequest))
        attempt = session.scalar(select(SemanticRequestAttempt))
        changeset = session.scalar(select(ChangeSet))
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.event_type == "semantic_option.safely_rebased"
            )
        )
        assert utterance is not None and semantic_request is not None
        assert attempt is not None and changeset is not None and audit is not None
        assert utterance.authority_scope_hash == old_authority_hash
        assert utterance.selected_precondition_hash == old_precondition_hash
        assert utterance.case_revision_ref == old_revision_ref
        assert semantic_request.current_precondition_hash == new_precondition_hash
        assert semantic_request.current_case_revision_ref == new_revision_ref
        assert attempt.precondition_hash == new_precondition_hash
        assert attempt.case_revision_ref == new_revision_ref
        assert changeset.precondition_hash == new_precondition_hash
        assert changeset.execution_binding_json["case_revision_ref"] == new_revision_ref
        assert audit.data == {
            "authority_scope_hash": old_authority_hash,
            "original_precondition_hash": old_precondition_hash,
            "rebased_precondition_hash": new_precondition_hash,
            "original_revision": old_revision_ref,
            "rebased_revision": new_revision_ref,
            "semantic_scope_changed": False,
        }


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
        invocation = session.scalar(select(ToolInvocation))
        attempt = session.scalar(select(SemanticRequestAttempt))
        assert invocation is not None and attempt is not None
        assert invocation.tool_name == "docket_commit_changeset"
        assert invocation.status == "succeeded"
        assert invocation.domain_state == "succeeded"
        assert invocation.result_disposition == "committed"
        assert invocation.semantic_request_ref == semantic_request.ref_id
        assert attempt.tool_call_ref == invocation.ref_id
        assert response.tool_call_refs == [invocation.ref_id]
        assert semantic_request.committed_changeset_ref == selection["execution"]["ref"]
        assert session.scalar(select(func.count(ChangeSet.id))) == 1
        assert session.scalar(select(func.count(SemanticRequestAttempt.id))) == 1
        assert session.scalar(select(func.count(Entity.id))) == 1


@pytest.mark.integration
def test_one_click_cal_poly_resolution_commits_one_complete_changeset(
    session_factory,
) -> None:
    settings = get_settings()
    initial_message_id = "1542799000000000240"
    prompt_message_id = "1542799000000000241"
    interaction_id = "1542799000000000242"
    with session_factory.begin() as session:
        initial_ref = _capture_utterance(
            session,
            message_id=initial_message_id,
            text="Offer the exact Cal Poly institution and identity resolution.",
        )
        identity = IdentityHandle(
            handle_type="email",
            value="eadvise@calpoly.edu",
            normalized_value="eadvise@calpoly.edu",
            status="unbound",
            basis_refs=[initial_ref],
        )
        case = AttentionCase(
            situation_key="cal-poly-mentor-collective",
            title="Resolve the Cal Poly sender",
            summary="The exact sender identity needs an Operator decision.",
            semantic_classes=["registry_candidate"],
            first_observed_at=datetime.now(UTC),
            last_observed_at=datetime.now(UTC),
        )
        session.add_all([identity, case])
        session.flush()
        item = CaseItem(
            attention_case_id=case.id,
            item_key="sender-identity",
            item_type="identity_resolution",
            resolution_role="required",
            basis_refs=[initial_ref],
        )
        session.add(item)
        session.flush()
        revision = AttentionCaseRevision(
            attention_case_id=case.id,
            case_ref=case.ref_id,
            revision=1,
            title=case.title,
            summary=case.summary,
            semantic_classes=list(case.semantic_classes),
            item_refs=[item.ref_id],
            source_refs=[],
            content_hash="1" * 64,
        )
        session.add(revision)
        session.flush()
        intent = IntentSession(
            conversation_ref=(
                f"discord_conversation:{settings.discord_guild_id}:"
                f"{settings.chat_channel_id}"
            ),
            source_utterance_ref=initial_ref,
            case_refs=[case.ref_id],
            case_revision_refs=[revision.ref_id],
            trusted_context_refs=[],
            resolved_intent_json={"kind": "identity_clarification"},
            blocking_clarifications=[
                {
                    "blocking": True,
                    "code": "identity_resolution_required",
                    "question": "Create Cal Poly and bind the exact sender?",
                }
            ],
            state="needs_clarification",
            semantic_state="needs_clarification",
        )
        session.add(intent)
        session.flush()
        projection = SemanticOptionService(session).persist_prompt(
            utterance=session.scalar(
                select(OperatorUtterance).where(OperatorUtterance.ref_id == initial_ref)
            ),
            intent_session=intent,
            question="Create Cal Poly and bind the exact sender?",
            drafts=[
                _cal_poly_case_option(
                    initial_ref,
                    identity_ref=identity.ref_id,
                    case_ref=case.ref_id,
                    case_revision_ref=revision.ref_id,
                    case_item_ref=item.ref_id,
                    identity_version=identity.version,
                    case_version=case.version,
                )
            ],
        )
        option = session.scalar(
            select(PersistedSemanticOption).where(
                PersistedSemanticOption.prompt_projection_id == projection.id
            )
        )
        assert option is not None
        assert "create institution “cal poly”" in option.visible_text.casefold()
        assert "bind email `eadvise@calpoly.edu`" in option.visible_text.casefold()
        assert "set " + case.ref_id.casefold() in option.visible_text.casefold()
        projection.message_id = prompt_message_id
        projection.status = "delivered"
        token = issue_semantic_option_token(
            option_row_id=option.id,
            projection_version=option.prompt_projection_version,
            actor_id=settings.operator_discord_user_id,
            expires_at=datetime.now(UTC) + timedelta(days=1),
            signing_key=settings.read_secret(
                settings.interaction_signing_key_file
            ).encode(),
        )

    result = semantic_option_selection(
        SemanticOptionSelection(
            request_id=uuid.uuid4(),
            discord_interaction_id=interaction_id,
            discord_user_id=settings.operator_discord_user_id,
            guild_id=settings.discord_guild_id,
            channel_id=settings.chat_channel_id,
            message_id=prompt_message_id,
            option_token=token,
            responded_at=datetime.now(UTC),
        )
    )
    assert result["state"] == "committed"
    assert result["disposition"] == "committed"

    with session_factory.begin() as session:
        selected = session.scalar(
            select(OperatorUtterance).where(
                OperatorUtterance.ref_id == result["utterance_ref"]
            )
        )
        created = session.scalar(
            select(Entity).where(Entity.normalized_name == "cal poly")
        )
        identity = session.scalar(
            select(IdentityHandle).where(
                IdentityHandle.normalized_value == "eadvise@calpoly.edu"
            )
        )
        case = session.scalar(
            select(AttentionCase).where(
                AttentionCase.situation_key == "cal-poly-mentor-collective"
            )
        )
        item = session.scalar(
            select(CaseItem).where(CaseItem.item_key == "sender-identity")
        )
        binding = session.scalar(select(IdentityBinding))
        changeset = session.scalar(select(ChangeSet))
        invocation = session.scalar(select(ToolInvocation))
        assert selected is not None and created is not None and identity is not None
        assert case is not None and item is not None and binding is not None
        assert changeset is not None and invocation is not None
        assert selected.utterance_kind == "button_selection"
        assert identity.entity_id == created.id
        assert identity.binding_rule == "operator_selection"
        assert identity.binding_basis_refs == [selected.ref_id]
        assert binding.binding_rule == "operator_selection"
        assert binding.basis_refs == [selected.ref_id]
        assert case.status == "resolved"
        assert item.status == "resolved"
        assert session.scalar(select(func.count(ChangeSet.id))) == 1
        assert len(changeset.registry_changes) == 2
        assert len(changeset.resolution_changes) == 1
        assert invocation.status == "succeeded"
        assert invocation.result_disposition == "committed"
        assert session.scalar(
            select(func.count(ToolInvocation.id)).where(
                ToolInvocation.status.like("rejected%")
            )
        ) == 0


@pytest.mark.integration
def test_intervening_identity_binding_opens_conflict_without_partial_commit(
    session_factory,
) -> None:
    settings = get_settings()
    with session_factory.begin() as session:
        prior_ref = _capture_utterance(
            session,
            message_id="1542799000000000250",
            text="Bind eadvise@calpoly.edu to the existing advising service.",
        )
        initial_ref = _capture_utterance(
            session,
            message_id="1542799000000000251",
            text="Offer the Cal Poly institution resolution.",
        )
        prior_entity = Entity(
            entity_class="organization",
            canonical_name="Existing Advising Service",
            normalized_name="existing advising service",
            status="active",
            authority="operator_utterance",
            registration_state="registered",
            basis_refs=[prior_ref],
            provenance_status="complete",
        )
        session.add(prior_entity)
        session.flush()
        identity = IdentityHandle(
            handle_type="email",
            value="eadvise@calpoly.edu",
            normalized_value="eadvise@calpoly.edu",
            entity_id=prior_entity.id,
            binding_rule="operator_selection",
            binding_basis_refs=[prior_ref],
            status="bound",
            basis_refs=[prior_ref],
        )
        case = AttentionCase(
            situation_key="cal-poly-binding-conflict",
            title="Resolve a changed sender binding",
            summary="The sender binding changes between projection and commit.",
            semantic_classes=["registry_candidate"],
            first_observed_at=datetime.now(UTC),
            last_observed_at=datetime.now(UTC),
        )
        session.add_all([identity, case])
        session.flush()
        session.add(
            IdentityBinding(
                identity_handle_id=identity.id,
                entity_id=prior_entity.id,
                binding_rule="operator_selection",
                status="active",
                basis_refs=[prior_ref],
                decision_refs=[],
                source_refs=[],
                created_by_changeset_ref=f"chg_{'1' * 26}",
            )
        )
        item = CaseItem(
            attention_case_id=case.id,
            item_key="sender-identity",
            item_type="identity_resolution",
            resolution_role="required",
            basis_refs=[initial_ref],
        )
        session.add(item)
        session.flush()
        revision = AttentionCaseRevision(
            attention_case_id=case.id,
            case_ref=case.ref_id,
            revision=1,
            title=case.title,
            summary=case.summary,
            semantic_classes=list(case.semantic_classes),
            item_refs=[item.ref_id],
            source_refs=[],
            content_hash="2" * 64,
        )
        session.add(revision)
        session.flush()
        intent = IntentSession(
            conversation_ref=(
                f"discord_conversation:{settings.discord_guild_id}:"
                f"{settings.chat_channel_id}"
            ),
            source_utterance_ref=initial_ref,
            case_refs=[case.ref_id],
            case_revision_refs=[revision.ref_id],
            trusted_context_refs=[],
            blocking_clarifications=[
                {
                    "blocking": True,
                    "code": "identity_resolution_required",
                    "question": "Create Cal Poly and replace the sender binding?",
                }
            ],
            state="needs_clarification",
            semantic_state="needs_clarification",
        )
        session.add(intent)
        session.flush()
        projection = SemanticOptionService(session).persist_prompt(
            utterance=session.scalar(
                select(OperatorUtterance).where(OperatorUtterance.ref_id == initial_ref)
            ),
            intent_session=intent,
            question="Create Cal Poly and replace the sender binding?",
            drafts=[
                _cal_poly_case_option(
                    initial_ref,
                    identity_ref=identity.ref_id,
                    case_ref=case.ref_id,
                    case_revision_ref=revision.ref_id,
                    case_item_ref=item.ref_id,
                    identity_version=identity.version,
                    case_version=case.version,
                )
            ],
        )
        option = session.scalar(
            select(PersistedSemanticOption).where(
                PersistedSemanticOption.prompt_projection_id == projection.id
            )
        )
        assert option is not None
        projection.message_id = "1542799000000000252"
        projection.status = "delivered"
        token = issue_semantic_option_token(
            option_row_id=option.id,
            projection_version=option.prompt_projection_version,
            actor_id=settings.operator_discord_user_id,
            expires_at=datetime.now(UTC) + timedelta(days=1),
            signing_key=settings.read_secret(
                settings.interaction_signing_key_file
            ).encode(),
        )

    result = semantic_option_selection(
        SemanticOptionSelection(
            request_id=uuid.uuid4(),
            discord_interaction_id="1542799000000000253",
            discord_user_id=settings.operator_discord_user_id,
            guild_id=settings.discord_guild_id,
            channel_id=settings.chat_channel_id,
            message_id="1542799000000000252",
            option_token=token,
            responded_at=datetime.now(UTC),
        )
    )
    assert result["state"] == "blocked_conflict"
    assert result["disposition"] == "rejected_conflict"
    assert "binding changed after" in result["response_text"]

    with session_factory.begin() as session:
        conflict = session.scalar(select(Conflict))
        semantic_request = session.scalar(select(SemanticRequest))
        identity = session.scalar(
            select(IdentityHandle).where(
                IdentityHandle.normalized_value == "eadvise@calpoly.edu"
            )
        )
        case = session.scalar(
            select(AttentionCase).where(
                AttentionCase.situation_key == "cal-poly-binding-conflict"
            )
        )
        invocation = session.scalar(select(ToolInvocation))
        assert conflict is not None and conflict.status == "open"
        assert conflict.subject_refs == [identity.ref_id]
        assert conflict.affected_fields == ["identity_binding"]
        assert semantic_request is not None
        assert semantic_request.authority_availability == "available"
        assert semantic_request.commit_state == "blocked_conflict"
        assert identity.status == "bound"
        assert case is not None and case.status == "open"
        assert session.scalar(select(func.count(Entity.id))) == 1
        assert session.scalar(select(func.count(ChangeSet.id))) == 1
        assert invocation is not None
        assert invocation.status == "rejected_conflict"
        assert invocation.result_disposition == "rejected_conflict"


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
        invocations = list(
            session.scalars(select(ToolInvocation).order_by(ToolInvocation.started_at))
        )
        attempts = list(
            session.scalars(
                select(SemanticRequestAttempt).order_by(
                    SemanticRequestAttempt.attempt_number
                )
            )
        )
        assert len(invocations) == 2
        assert all(item.status == "rejected_validation" for item in invocations)
        assert all(item.domain_state == "rejected" for item in invocations)
        assert all(
            item.result_disposition == "rejected_validation" for item in invocations
        )
        assert [item.tool_call_ref for item in attempts] == [
            item.ref_id for item in invocations
        ]
        assert session.scalar(select(func.count(OperatorUtterance.id))) == 2
        assert session.scalar(select(func.count(Entity.id))) == 0


@pytest.mark.integration
def test_repaired_runtime_retries_same_semantic_request_without_new_authority(
    session_factory,
    monkeypatch,
) -> None:
    settings = get_settings()
    with session_factory.begin() as session:
        initial_ref = _capture_utterance(
            session,
            message_id="1542799000000000208",
            text="Offer the exact Cal Poly creation option.",
        )
        InteractiveAuthorityService(session).process_turn(
            utterance_ref=initial_ref,
            request_key=(
                f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
                "1542799000000000208:0"
            ),
            actor_id=settings.operator_discord_user_id,
            intent_session_ref=None,
            expected_session_version=None,
            statements=[],
            relations=[],
            resolved_intent_json={"kind": "entity_clarification"},
            blocking_clarifications=[
                {
                    "blocking": True,
                    "code": "entity_resolution_required",
                    "question": "Create Cal Poly?",
                }
            ],
            content=None,
            changeset_ref=None,
            expected_changeset_version=None,
            semantic_options=[_entity_option(initial_ref)],
        )
        prompt = session.scalar(select(SemanticPromptProjection))
        option = session.scalar(select(PersistedSemanticOption))
        assert prompt is not None and option is not None
        prompt.message_id = "1542799000000000209"
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

    payload = SemanticOptionSelection(
        request_id=uuid.uuid4(),
        discord_interaction_id="1542799000000000213",
        discord_user_id=settings.operator_discord_user_id,
        guild_id=settings.discord_guild_id,
        channel_id=settings.chat_channel_id,
        message_id="1542799000000000209",
        option_token=token,
        responded_at=datetime.now(UTC),
    )
    original = InteractiveAuthorityService.process_turn

    def injected_failure(self, **_kwargs):
        raise DocketError(
            code="injected_compiler_defect",
            message="Injected compiler failure for retry continuity.",
        )

    monkeypatch.setattr(InteractiveAuthorityService, "process_turn", injected_failure)
    first = semantic_option_selection(payload)
    assert first["state"] == "blocked_validation"
    assert first["disposition"] == "rejected_validation"
    monkeypatch.setattr(InteractiveAuthorityService, "process_turn", original)

    retry = semantic_option_selection(
        payload.model_copy(
            update={
                "request_id": uuid.uuid4(),
                "resume_authorized_execution": True,
            }
        )
    )
    assert retry["state"] == "committed", retry.get("execution", {}).get("error")
    assert retry["utterance_ref"] == first["utterance_ref"]
    assert retry["semantic_request_ref"] == first["semantic_request_ref"]

    with session_factory.begin() as session:
        semantic_request = session.scalar(select(SemanticRequest))
        invocations = list(
            session.scalars(select(ToolInvocation).order_by(ToolInvocation.started_at))
        )
        attempts = list(
            session.scalars(
                select(SemanticRequestAttempt).order_by(
                    SemanticRequestAttempt.attempt_number
                )
            )
        )
        assert semantic_request is not None
        assert semantic_request.authority_availability == "consumed_committed"
        assert semantic_request.commit_state == "committed"
        assert semantic_request.committed_changeset_ref == retry["execution"]["ref"]
        assert session.scalar(select(func.count(OperatorUtterance.id))) == 2
        assert session.scalar(select(func.count(ChangeSet.id))) == 1
        assert session.scalar(select(func.count(Entity.id))) == 1
        assert [item.result_disposition for item in invocations] == [
            "rejected_validation",
            "committed",
        ]
        assert [item.state for item in attempts] == [
            "blocked_validation",
            "committed",
        ]
        assert [item.tool_call_ref for item in attempts] == [
            item.ref_id for item in invocations
        ]


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
