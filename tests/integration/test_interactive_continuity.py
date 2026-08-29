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
    Entity,
    OperatorUtterance,
    PersistedSemanticOption,
    SemanticPromptProjection,
    SemanticRequest,
    SemanticRequestAttempt,
)
from docket.schemas.authority import SemanticOptionDraft
from docket.security import issue_semantic_option_token
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
