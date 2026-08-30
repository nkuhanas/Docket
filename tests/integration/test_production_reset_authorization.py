from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from sqlalchemy import func, select

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.domain.production_reset import (
    TRACKED_CONTEXT_DOCUMENT_REF,
    TRACKED_CONTEXT_FROZEN_ARTIFACT_HASH,
    ProductionResetAuthorityBinding,
    production_reset_authorization_text,
)
from docket.internal_api.schemas import (
    OperatorUtteranceCapture,
    ProductionResetAuthorizationCapture,
)
from docket.models import AuditEvent, Decision
from docket.services.provenance import ProvenanceService


def _binding() -> ProductionResetAuthorityBinding:
    return ProductionResetAuthorityBinding(
        document_ref=TRACKED_CONTEXT_DOCUMENT_REF,
        frozen_artifact_hash=TRACKED_CONTEXT_FROZEN_ARTIFACT_HASH,
        reset_manifest_sha256="a" * 64,
        verified_backup_ref="tracked-context-pre-reset-20260830.dump",
        verified_backup_sha256="b" * 64,
        deployment_revision="c" * 40,
    )


def _capture_request(text: str) -> OperatorUtteranceCapture:
    settings = get_settings()
    message_id = "1542778234028953999"
    return OperatorUtteranceCapture(
        request_id=uuid.uuid4(),
        guild_id=settings.discord_guild_id,
        channel_id=settings.chat_channel_id,
        message_id=message_id,
        actor_id=settings.operator_discord_user_id,
        verbatim_text=text,
        request_key=(
            f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:{message_id}:0"
        ),
    )


def _authorization_request(
    utterance_ref: str,
    binding: ProductionResetAuthorityBinding,
) -> ProductionResetAuthorizationCapture:
    return ProductionResetAuthorizationCapture(
        request_id=uuid.uuid4(),
        utterance_ref=utterance_ref,
        document_ref=binding.document_ref,
        frozen_artifact_hash=binding.frozen_artifact_hash,
        reset_manifest_sha256=binding.reset_manifest_sha256,
        verified_backup_ref=binding.verified_backup_ref,
        verified_backup_sha256=binding.verified_backup_sha256,
        deployment_revision=binding.deployment_revision,
    )


@pytest.mark.integration
def test_exact_reset_authority_creates_one_immutable_decision(session_factory) -> None:
    binding = _binding()
    with session_factory.begin() as session:
        utterance = ProvenanceService(session).capture_operator_utterance(
            _capture_request(production_reset_authorization_text(binding))
        )
    request = _authorization_request(str(utterance["ref"]), binding)

    with session_factory.begin() as session:
        created = ProvenanceService(session).record_production_reset_authorization(request)
    with session_factory.begin() as session:
        replay = ProvenanceService(session).record_production_reset_authorization(request)

    assert created["state"] == "authorized"
    assert created["disposition"] == "created"
    assert created["production_reset_executed"] is False
    assert replay["ref"] == created["ref"]
    assert replay["disposition"] == "replayed_request"
    with session_factory() as session:
        decision = session.scalar(
            select(Decision).where(Decision.decision_kind == "production_reset_authorization")
        )
        assert decision is not None
        assert decision.architecture_authority is False
        assert decision.authorized_scope == "production_reset_exact_manifest_revision"
        assert decision.basis_refs == [utterance["ref"]]
        assert decision.payload_json == {
            "reset_manifest_sha256": "a" * 64,
            "verified_backup_ref": "tracked-context-pre-reset-20260830.dump",
            "verified_backup_sha256": "b" * 64,
            "deployment_revision": "c" * 40,
        }
        assert (
            session.scalar(
                select(func.count(Decision.id)).where(
                    Decision.decision_kind == "production_reset_authorization"
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.event_type == "decision.production_reset_authorization_recorded"
                )
            )
            == 1
        )

    with pytest.raises(ValueError, match="immutable"), session_factory.begin() as session:
        decision = session.scalar(select(Decision).where(Decision.ref_id == created["ref"]))
        assert decision is not None
        decision.payload_json = {"reset_manifest_sha256": "d" * 64}


@pytest.mark.integration
def test_reset_authority_rejects_binding_not_named_by_utterance(session_factory) -> None:
    binding = _binding()
    with session_factory.begin() as session:
        utterance = ProvenanceService(session).capture_operator_utterance(
            _capture_request(production_reset_authorization_text(binding))
        )
    mismatched = replace(binding, reset_manifest_sha256="d" * 64)

    with (
        pytest.raises(DocketError) as captured,
        session_factory.begin() as session,
    ):
        ProvenanceService(session).record_production_reset_authorization(
            _authorization_request(str(utterance["ref"]), mismatched)
        )
    assert captured.value.code == "production_reset_authorization_not_explicit"

    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count(Decision.id)).where(
                    Decision.decision_kind == "production_reset_authorization"
                )
            )
            == 0
        )


def test_reset_authority_text_is_exact_and_human_auditable() -> None:
    text = production_reset_authorization_text(_binding())

    assert TRACKED_CONTEXT_DOCUMENT_REF in text
    assert TRACKED_CONTEXT_FROZEN_ARTIFACT_HASH in text
    assert "a" * 64 in text
    assert "tracked-context-pre-reset-20260830.dump" in text
    assert "b" * 64 in text
    assert "c" * 40 in text
    assert text.endswith(".")
