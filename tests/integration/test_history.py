import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.internal_api.schemas import AgentResponseCapture, OperatorUtteranceCapture
from docket.models import (
    Account,
    InterpretedStatement,
    OperatorUtterance,
    RuntimeLogEntry,
    ToolInvocation,
)
from docket.services.history import DEFAULT_OUTPUT_BYTES, HistoryService
from docket.services.mcp_traces import trace_id_for_source
from docket.services.provenance import ProvenanceService
from docket.services.runtime_logs import RuntimeLogService

MESSAGE_ID = "1542778234028953620"


def _utterance_request(text: str) -> OperatorUtteranceCapture:
    settings = get_settings()
    return OperatorUtteranceCapture.model_validate(
        {
            "request_id": str(uuid.uuid4()),
            "guild_id": settings.discord_guild_id,
            "channel_id": settings.chat_channel_id,
            "message_id": MESSAGE_ID,
            "actor_id": settings.operator_discord_user_id,
            "verbatim_text": text,
            "request_key": (
                f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:{MESSAGE_ID}:0"
            ),
        }
    )


@pytest.mark.integration
def test_exact_history_lookup_and_conversation_reconstruction_are_public_and_bounded(
    session_factory,
) -> None:
    settings = get_settings()
    verbatim = "Remember café ☕ and preserve this exact text."
    with session_factory.begin() as session:
        utterance_ref = ProvenanceService(session).capture_operator_utterance(
            _utterance_request(verbatim)
        )["ref"]
        utterance = session.scalar(
            select(OperatorUtterance).where(OperatorUtterance.ref_id == utterance_ref)
        )
        assert utterance is not None
        statement = InterpretedStatement(
            utterance_id=utterance.id,
            statement_kind="fact_candidate",
            subject_refs=[],
            predicate="remember",
            value_json={"text": "café"},
            affected_fields=["memory"],
            interpretation_json={"source": "test"},
            interpreter_version="test-v1",
        )
        session.add(statement)
        session.flush()
        statement_ref = statement.ref_id
        trace_id = trace_id_for_source(
            settings.discord_guild_id,
            settings.chat_channel_id,
            MESSAGE_ID,
        )
        response = ProvenanceService(session).capture_agent_response(
            AgentResponseCapture.model_validate(
                {
                    "request_id": str(uuid.uuid4()),
                    "guild_id": settings.discord_guild_id,
                    "channel_id": settings.chat_channel_id,
                    "source_message_id": MESSAGE_ID,
                    "actor_id": settings.operator_discord_user_id,
                    "utterance_ref": utterance_ref,
                    "turn_id": "history-turn",
                    "session_id": "history-session",
                    "model_identifier": "test-model",
                    "verbatim_text": "I preserved it.",
                    "generated_at": datetime.now(UTC).isoformat(),
                    "trace_id": str(trace_id),
                }
            )
        )
        response_ref = response["ref"]

    with session_factory() as session:
        history = HistoryService(session)
        summary = history.get_entry(utterance_ref)
        assert summary["entry"]["ref"] == utterance_ref
        assert "verbatim_text" not in summary["entry"]
        assert "id" not in summary["entry"]

        chunks = []
        offset = 0
        while True:
            audit = history.get_entry(
                utterance_ref,
                view="audit",
                text_offset=offset,
                text_limit=9,
            )["entry"]
            chunks.append(audit["verbatim_text_chunk"])
            next_offset = audit["text_next_offset"]
            if next_offset is None:
                break
            assert next_offset > offset
            offset = next_offset
        assert "".join(chunks) == verbatim

        statement_entry = history.get_entry(statement_ref, view="audit")["entry"]
        assert statement_entry["utterance_ref"] == utterance_ref
        assert statement_entry["value_json"] == {"text": "café"}

        conversation_ref = summary["entry"]["conversation_ref"]
        conversation = history.conversation(conversation_ref)
        assert [item["ref"] for item in conversation["items"]] == [
            utterance_ref,
            response_ref,
        ]
        assert [item["role"] for item in conversation["items"]] == ["operator", "agent"]


@pytest.mark.integration
def test_history_search_filters_paginates_and_stays_under_utf8_budget(session_factory) -> None:
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        utterance_ref = ProvenanceService(session).capture_operator_utterance(
            _utterance_request("search provenance")
        )["ref"]
        invocation = ToolInvocation(
            tool_name="docket_search_records",
            tool_contract_version="test",
            caller_profile="interactive",
            actor_ref="discord_user:test",
            utterance_refs=[utterance_ref],
            started_at=now,
            completed_at=now,
            status="succeeded",
            received_argument_hash="a" * 64,
            normalized_argument_hash="a" * 64,
            result_refs=[],
        )
        session.add(invocation)
        for index in range(40):
            RuntimeLogService(session).append(
                severity="info",
                component="history-test",
                event_code="history.page",
                message=f"{index:02d}:" + "é" * 450,
                related_refs=[utterance_ref] if index == 0 else [],
                metadata_json={"index": index},
            )

    with session_factory() as session:
        history = HistoryService(session)
        first = history.search(object_type="runtime_log_entry", limit=25)
        serialized = json.dumps(first, separators=(",", ":")).encode()
        assert len(serialized) <= DEFAULT_OUTPUT_BYTES
        assert first["count"] < 25
        assert first["truncated"] is True
        assert first["cursor"] is not None

        second = history.search(
            object_type="runtime_log_entry",
            limit=25,
            cursor=first["cursor"],
        )
        assert {item["ref"] for item in first["items"]}.isdisjoint(
            item["ref"] for item in second["items"]
        )

        related = history.search(related_ref=utterance_ref, limit=25)
        assert any(item["type"] == "tool_invocation" for item in related["items"])
        assert any(item["type"] == "runtime_log_entry" for item in related["items"])

        tool = history.search(tool_name="docket_search_records")
        assert [item["type"] for item in tool["items"]] == ["tool_invocation"]
        assert tool["items"][0]["utterance_refs"] == [utterance_ref]

        interval = history.search(
            object_type="runtime_log_entry",
            occurred_from=now - timedelta(seconds=1),
            occurred_to=now + timedelta(seconds=1),
        )
        assert interval["items"]
        assert all(item["type"] == "runtime_log_entry" for item in interval["items"])

        with pytest.raises(DocketError) as invalid_type:
            history.search(object_type="internal_uuid")
        assert invalid_type.value.code == "invalid_history_object_type"

        with pytest.raises(DocketError) as invalid_cursor:
            history.search(cursor="not-a-cursor")
        assert invalid_cursor.value.code == "invalid_history_cursor"


@pytest.mark.integration
def test_runtime_log_entry_is_operational_and_immutable(session_factory) -> None:
    with session_factory.begin() as session:
        entry = RuntimeLogService(session).append(
            severity="warning",
            component="mcp_auth",
            event_code="mcp.authentication_rejected",
            message="Rejected before authentication.",
            metadata_json={"profile": "interactive"},
        )
        ref_id = entry.ref_id
        assert ref_id.startswith("log_")

    with pytest.raises(ValueError, match="immutable"), session_factory.begin() as session:
        entry = session.scalar(select(RuntimeLogEntry).where(RuntimeLogEntry.ref_id == ref_id))
        assert entry is not None
        entry.message = "rewritten"


@pytest.mark.integration
def test_provider_identity_source_ref_is_searchable_without_internal_uuid(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        account = Account(
            provider="google",
            external_account_id="history-provider-test",
            display_name="History provider",
            capabilities=["google_calendar"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        account_ref = account.ref_id

    with session_factory() as session:
        history = HistoryService(session)
        entry = history.get_entry(account_ref)["entry"]
        assert entry == {
            "ref": account_ref,
            "type": "source",
            "source_kind": "provider_identity",
            "provider": "google",
            "external_account_id": "history-provider-test",
            "display_name": "History provider",
            "email_address": None,
            "capabilities": ["google_calendar"],
            "enabled": True,
            "created_at": entry["created_at"],
        }
        assert "id" not in entry
        searched = history.search(ref_id=account_ref)
        assert [item["ref"] for item in searched["items"]] == [account_ref]
