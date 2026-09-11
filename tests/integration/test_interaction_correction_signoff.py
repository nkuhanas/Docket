from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.internal_api.schemas import OperatorUtteranceCapture, SpecificationSignoffCapture
from docket.models import AuditEvent, CanonicalEvent, ChangeSet, Decision, Operation
from docket.services.provenance import ProvenanceService
from docket.specification_artifacts import specification_artifact

DOCUMENT = "ONT-DELTA-2026-09-11-INTERACTION-CORRECTION"
FROZEN_HASH = "39ca596f2ff00e70cad28fe6e3750f284efde53d4db47d8490531d61ae9fd23a"


def _capture(session, *, text: str | None = None) -> SpecificationSignoffCapture:
    artifact = specification_artifact(DOCUMENT, FROZEN_HASH)
    assert artifact is not None
    settings = get_settings()
    message_id = "1542778234028953699"
    capture = OperatorUtteranceCapture(
        request_id=uuid.uuid4(),
        guild_id=settings.discord_guild_id,
        channel_id=settings.chat_channel_id,
        message_id=message_id,
        actor_id=settings.operator_discord_user_id,
        request_key=(
            f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:{message_id}:0"
        ),
        verbatim_text=artifact.signoff_text if text is None else text,
    )
    result = ProvenanceService(session).capture_operator_utterance(capture)
    return SpecificationSignoffCapture(
        request_id=uuid.uuid4(),
        utterance_ref=result["ref"],
        document_ref=DOCUMENT,
        frozen_artifact_hash=FROZEN_HASH,
    )


def _prerequisites(session, *, changed_index: int | None = None) -> None:
    artifact = specification_artifact(DOCUMENT, FROZEN_HASH)
    assert artifact is not None
    for index, prerequisite in enumerate(artifact.prerequisites):
        session.add(
            Decision(
                ref_id=(
                    new_public_ref("dec")
                    if index == changed_index
                    else prerequisite.decision_ref
                ),
                decision_kind=prerequisite.decision_kind,
                document_ref=prerequisite.document_ref,
                frozen_artifact_hash=prerequisite.frozen_artifact_hash,
                architecture_authority=prerequisite.architecture_authority,
            )
        )
    session.flush()


@pytest.mark.integration
def test_interaction_signoff_is_exact_idempotent_and_has_no_domain_effects(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        _prerequisites(session)
        request = _capture(session)
        created = ProvenanceService(session).record_final_architecture_signoff(request)
    with session_factory.begin() as session:
        replay = ProvenanceService(session).record_final_architecture_signoff(request)
        assert replay["ref"] == created["ref"]
        assert replay["disposition"] == "replayed_request"
        assert created["production_reset_authority"] is False
        decision = session.scalar(select(Decision).where(Decision.document_ref == DOCUMENT))
        assert decision is not None
        assert decision.basis_refs == [request.utterance_ref]
        assert decision.implementation_authority == "amendment_scope"
        assert len(decision.payload_json["prerequisite_decision_refs"]) == 5
        audits = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.event_type == "decision.specification_signoff_recorded"
                )
            )
        )
        assert len(audits) == 1
        assert audits[0].primary_ref == decision.ref_id
        assert audits[0].basis_refs == [request.utterance_ref]
        for model in (CanonicalEvent, ChangeSet, Operation):
            assert session.scalar(select(func.count(model.id))) == 0


@pytest.mark.integration
@pytest.mark.parametrize("changed_index", range(5))
def test_interaction_signoff_rejects_each_substituted_prerequisite(
    session, changed_index: int
) -> None:
    _prerequisites(session, changed_index=changed_index)
    request = _capture(session)
    with pytest.raises(DocketError) as failure:
        ProvenanceService(session).record_final_architecture_signoff(request)
    assert failure.value.code == "specification_signoff_prerequisite_missing"
    assert session.scalar(
        select(func.count(Decision.id)).where(Decision.document_ref == DOCUMENT)
    ) == 0


@pytest.mark.integration
def test_interaction_signoff_requires_exact_text_and_hash(session) -> None:
    _prerequisites(session)
    request = _capture(session, text="Implement the plan.")
    with pytest.raises(DocketError) as failure:
        ProvenanceService(session).record_final_architecture_signoff(request)
    assert failure.value.code == "specification_signoff_not_explicit"
    with pytest.raises(DocketError) as failure:
        ProvenanceService(session).record_final_architecture_signoff(
            request.model_copy(update={"frozen_artifact_hash": "0" * 64})
        )
    assert failure.value.code == "specification_signoff_artifact_mismatch"
    assert session.scalar(
        select(func.count(Decision.id)).where(Decision.document_ref == DOCUMENT)
    ) == 0
