from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from docket.config import get_settings
from docket.internal_api.schemas import SemanticOptionSelection
from docket.models import (
    DeferredIngress,
    IntentSession,
    OperatorProjection,
    OperatorUtterance,
    PersistedSemanticOption,
    ProjectionDelivery,
    SemanticRequest,
)
from docket.providers.discord import FakeDiscordProjectionAdapter
from docket.schemas.authority import SemanticOptionDraft
from docket.services.discord_projection import DiscordProjectionRunner
from docket.services.semantic_options import (
    CURRENT_SELECTION_UTTERANCE,
    SemanticOptionService,
)


def _utterance(message_id: str) -> OperatorUtterance:
    settings = get_settings()
    text = "Create Cal Poly."
    return OperatorUtterance(
        actor_ref=f"discord_user:{settings.operator_discord_user_id}",
        transport="discord",
        source_message_ref=(
            f"discord_message:{settings.discord_guild_id}:"
            f"{settings.chat_channel_id}:{message_id}"
        ),
        conversation_ref=(
            f"discord_conversation:{settings.discord_guild_id}:"
            f"{settings.chat_channel_id}"
        ),
        said_at=datetime.now(UTC),
        verbatim_text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        request_key=(
            f"discord:{settings.discord_guild_id}:"
            f"{settings.chat_channel_id}:{message_id}:0"
        ),
    )


def _draft(utterance_ref: str) -> SemanticOptionDraft:
    return SemanticOptionDraft.model_validate(
        {
            "option_id": "create-cal-poly",
            "selection_authority_ref": utterance_ref,
            "content": {
                "basis_refs": [utterance_ref],
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
                        "basis_refs": [utterance_ref],
                    }
                ],
            },
        }
    )


@pytest.mark.integration
def test_projection_option_selection_is_one_typed_authority_chain(session_factory) -> None:
    settings = get_settings()
    message_id = "1542799000000000501"
    interaction_id = "1542799000000000502"
    with session_factory.begin() as session:
        utterance = _utterance(message_id)
        session.add(utterance)
        session.flush()
        intent = IntentSession(
            conversation_ref=utterance.conversation_ref,
            source_utterance_ref=utterance.ref_id,
            semantic_state="needs_clarification",
            commit_state="not_attempted",
        )
        session.add(intent)
        session.flush()
        projection = SemanticOptionService(session).persist_prompt(
            utterance=utterance,
            intent_session=intent,
            question="Create the institution?",
            drafts=[_draft(utterance.ref_id)],
        )
        option = session.scalar(
            select(PersistedSemanticOption).where(
                PersistedSemanticOption.projection_ref == projection.ref_id
            )
        )
        delivery = session.scalar(
            select(ProjectionDelivery).where(
                ProjectionDelivery.projection_ref == projection.ref_id
            )
        )
        assert option is not None
        assert delivery is not None
        projection_id = projection.id
        projection_ref = projection.ref_id

    adapter = FakeDiscordProjectionAdapter()
    assert DiscordProjectionRunner(session_factory, adapter, settings).run_due_once() is True
    rendered = adapter.backend.semantic_prompts[str(projection_id)]
    prompt_message_id = rendered["message_id"]
    token = rendered["controls"][0]["custom_id"].removeprefix("dkt:s:")
    with session_factory() as session:
        delivery = session.scalar(
            select(ProjectionDelivery).where(
                ProjectionDelivery.projection_ref == projection_ref
            )
        )
        assert delivery is not None
        assert delivery.external_message_ref == (
            f"discord_message:{settings.discord_guild_id}:"
            f"{settings.chat_channel_id}:{prompt_message_id}"
        )

    request = SemanticOptionSelection(
        request_id=uuid.uuid4(),
        discord_interaction_id=interaction_id,
        discord_user_id=settings.operator_discord_user_id,
        guild_id=settings.discord_guild_id,
        channel_id=settings.chat_channel_id,
        message_id=prompt_message_id,
        option_token=token,
        responded_at=datetime.now(UTC),
    )
    with session_factory.begin() as session:
        result = SemanticOptionService(session).capture_selection(request)
        selection = session.scalar(
            select(OperatorUtterance).where(
                OperatorUtterance.discord_interaction_ref == interaction_id
            )
        )
        option = session.scalar(
            select(PersistedSemanticOption).where(
                PersistedSemanticOption.ref_id == result["selected_option_ref"]
            )
        )
        semantic_request = session.scalar(
            select(SemanticRequest).where(
                SemanticRequest.ref_id == result["semantic_request_ref"]
            )
        )
        ingress = session.scalar(
            select(DeferredIngress).where(
                DeferredIngress.ref_id == result["deferred_ingress_ref"]
            )
        )
        projection = session.scalar(
            select(OperatorProjection).where(
                OperatorProjection.ref_id == result["projection_ref"]
            )
        )
        assert selection is not None
        assert option is not None
        assert projection is not None
        assert semantic_request is not None
        assert ingress is not None
        assert selection.selected_option_ref == option.ref_id
        assert selection.projection_ref == projection.ref_id
        assert semantic_request.authority_availability == "available"
        assert semantic_request.commit_state == "not_attempted"
        assert result["compiled_content"]["basis_refs"] == [selection.ref_id]
        assert CURRENT_SELECTION_UTTERANCE not in str(result["compiled_content"])

    with session_factory.begin() as session:
        replay = SemanticOptionService(session).capture_selection(request)
        assert replay["disposition"] == "replayed_request"
        assert replay["utterance_ref"] == result["utterance_ref"]
