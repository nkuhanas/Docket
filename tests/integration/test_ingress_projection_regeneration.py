from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import get_settings
from docket.models import (
    IntentSession,
    OperatorProjection,
    OperatorUtterance,
    PersistedSemanticOption,
    ProjectionDelivery,
)
from docket.providers.discord import FakeDiscordProjectionAdapter
from docket.schemas.authority import SemanticOptionDraft
from docket.services.discord_projection import DiscordProjectionRunner
from docket.services.ingress_deployment import IngressDeploymentService
from docket.services.semantic_options import SemanticOptionService


@pytest.mark.integration
def test_ingress_rollout_regenerates_clean_projection_without_changing_authority(
    session_factory: sessionmaker[Session],
) -> None:
    settings = get_settings()
    text = "Create Cal Poly."
    message_id = "1542799000000000701"
    with session_factory.begin() as session:
        utterance = OperatorUtterance(
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
            drafts=[
                SemanticOptionDraft.model_validate(
                    {
                        "option_id": "create-cal-poly",
                        "selection_authority_ref": utterance.ref_id,
                        "content": {
                            "basis_refs": [utterance.ref_id],
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
                                    "basis_refs": [utterance.ref_id],
                                }
                            ],
                        },
                    }
                )
            ],
        )
        prior_ref = projection.ref_id
        prior_option = session.scalar(
            select(PersistedSemanticOption).where(
                PersistedSemanticOption.projection_ref == prior_ref
            )
        )
        assert prior_option is not None
        authority_hash = prior_option.authority_scope_hash
        precondition_hash = prior_option.precondition_hash

    adapter = FakeDiscordProjectionAdapter()
    assert DiscordProjectionRunner(session_factory, adapter, settings).run_due_once() is True

    service = IngressDeploymentService(session_factory, adapter)
    quiesced = service.quiesce()
    assert quiesced["projection_refs"] == [prior_ref]
    regenerated = service.regenerate()
    assert regenerated["count"] == 1
    replacement_ref = regenerated["projection_refs"][0]

    with session_factory() as session:
        prior = session.scalar(
            select(OperatorProjection).where(OperatorProjection.ref_id == prior_ref)
        )
        replacement = session.scalar(
            select(OperatorProjection).where(
                OperatorProjection.ref_id == replacement_ref
            )
        )
        replacement_option = session.scalar(
            select(PersistedSemanticOption).where(
                PersistedSemanticOption.projection_ref == replacement_ref
            )
        )
        prior_delivery = session.scalar(
            select(ProjectionDelivery).where(
                ProjectionDelivery.projection_ref == prior_ref
            )
        )
        replacement_delivery = session.scalar(
            select(ProjectionDelivery).where(
                ProjectionDelivery.projection_ref == replacement_ref
            )
        )
        assert prior is not None
        assert replacement is not None
        assert replacement_option is not None
        assert prior_delivery is not None
        assert replacement_delivery is not None
        assert replacement.supersedes_projection_ref == prior.ref_id
        assert replacement_option.authority_scope_hash == authority_hash
        assert replacement_option.precondition_hash != precondition_hash
        assert replacement_option.execution_preconditions_json["projection_ref"] == (
            replacement.ref_id
        )
        assert prior_delivery.last_error_code == "ingress_deployment_regenerated"
        assert replacement_delivery.status == "pending"

    prior_message = adapter.backend.semantic_prompts[str(prior.id)]
    assert prior_message["controls"] == []
