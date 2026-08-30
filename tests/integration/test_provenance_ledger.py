from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from docket.config import get_settings
from docket.internal_api.schemas import OperatorUtteranceCapture
from docket.models import AuditEvent, OperatorUtterance
from docket.services.provenance import ProvenanceService


def _request() -> OperatorUtteranceCapture:
    settings = get_settings()
    message_id = "1542778234028953620"
    return OperatorUtteranceCapture(
        request_id=uuid.uuid4(),
        guild_id=settings.discord_guild_id,
        channel_id=settings.chat_channel_id,
        message_id=message_id,
        actor_id=settings.operator_discord_user_id,
        verbatim_text="lol\nexactly as typed",
        request_key=(
            f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
            f"{message_id}:0"
        ),
    )


@pytest.mark.integration
def test_operator_utterance_is_verbatim_idempotent_and_immutable(session_factory) -> None:
    request = _request()
    with session_factory.begin() as session:
        created = ProvenanceService(session).capture_operator_utterance(request)
    with session_factory.begin() as session:
        replay = ProvenanceService(session).capture_operator_utterance(request)

    assert replay["ref"] == created["ref"]
    assert replay["disposition"] == "replayed_request"
    with session_factory() as session:
        utterance = session.scalar(select(OperatorUtterance))
        assert utterance is not None
        assert utterance.verbatim_text == "lol\nexactly as typed"
        assert session.scalar(select(func.count(OperatorUtterance.id))) == 1
        assert session.scalar(select(func.count(AuditEvent.id))) == 1

    with pytest.raises(ValueError, match="immutable"), session_factory.begin() as session:
        utterance = session.scalar(select(OperatorUtterance))
        assert utterance is not None
        utterance.verbatim_text = "rewritten"
