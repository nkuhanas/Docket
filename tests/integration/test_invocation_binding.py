import asyncio
import base64
import hashlib
import hmac
import importlib.util
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from trace_support import bind_execution, segment_for

from docket.config import get_settings
from docket.database import get_session_factory
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.internal_api.schemas import McpTraceUpdate
from docket.mcp.instrumented import ProvenanceFastMCP, _result_envelope
from docket.models import OperatorUtterance, ToolInvocation
from docket.services.mcp_traces import McpTraceService
from docket.services.trace_correlation import correlated_calls
from docket.tool_contracts import CONTRACT_VERSION, contract_hash


def _evidence(session, message="999999999999999999"):
    settings = get_settings()
    utterance = OperatorUtterance(
        actor_ref=f"discord_user:{settings.operator_discord_user_id}", transport="discord",
        source_message_ref=(
            f"discord_message:{settings.discord_guild_id}:{settings.chat_channel_id}:{message}"
        ),
        conversation_ref=f"discord_conversation:{settings.chat_channel_id}",
        said_at=datetime.now(UTC), verbatim_text="Read my test context.",
        content_hash=hashlib.sha256(b"Read my test context.").hexdigest(),
        request_key=f"test-binding:{message}",
    )
    session.add(utterance)
    session.flush()
    return utterance.ref_id


def _payload(utterance, **updates):
    now = int(time.time())
    with get_session_factory().begin() as session:
        binding = bind_execution(session, session.scalar(select(OperatorUtterance).where(
            OperatorUtterance.ref_id == utterance,
        )))
    return {
        "format": 2, "trace_ref": binding["trace_ref"], "call_id": "transport-call-1",
        "ordinal": 1, "utterance_ref": utterance,
        **{key: binding[key] for key in (
            "execution_index", "execution_completion_token", "gateway_instance_ref",
        )},
        "tool_name": "docket_search_history", "argument_hash": sha256_json({"query": "same"}),
        "contract_version": CONTRACT_VERSION, "contract_hash": contract_hash("interactive"),
        "issued_at": now, "expires_at": now + 900, **updates,
    }


def _sign(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode()).decode().rstrip("=")
    signature = hmac.new(
        get_settings().hermes_to_docket_token().encode(),
        b"docket-mcp-invocation-v2:" + encoded.encode(), hashlib.sha256,
    ).hexdigest()
    return f"{encoded}.{signature}"


def _server(calls):
    server = ProvenanceFastMCP("binding-test", caller_profile="interactive")

    @server.tool(name="docket_search_history")
    def read(query: str):
        calls.append(query)
        return {"ok": True, "items": []}

    return server


def test_authenticated_invocation_binding_retains_ordinal_after_one_hundred(session_factory):
    with session_factory.begin() as session:
        utterance = _evidence(session)
    calls = []
    payload = _payload(utterance, ordinal=150, call_id="late-call")
    result = asyncio.run(_server(calls).call_tool("docket_search_history", {
        "query": "same", "invocation_binding": _sign(payload),
    }))
    assert _result_envelope(result)["ok"] is True
    assert calls == ["same"]
    with session_factory() as session:
        invocation = session.scalar(select(ToolInvocation))
        assert invocation.trace_ordinal == 150
        assert invocation.trace_call_id == "late-call"
        assert invocation.domain_state == "succeeded"


def test_same_arguments_bind_exactly_and_transport_retry_reaches_replay_service(session_factory):
    with session_factory.begin() as session:
        utterance = _evidence(session)
    calls = []
    server = _server(calls)
    first, second = _payload(utterance), _payload(utterance)
    # No trace callback or response reconciliation has happened. The durable
    # binding exists immediately and two identical requests cannot cross-link.
    for payload in (first, second):
        result = asyncio.run(server.call_tool("docket_search_history", {
            "query": "same", "invocation_binding": _sign(payload),
        }))
        assert _result_envelope(result)["ok"] is True
    with session_factory() as session:
        rows = list(session.scalars(select(ToolInvocation).order_by(ToolInvocation.started_at)))
        assert [row.trace_ref for row in rows] == [first["trace_ref"], second["trace_ref"]]
        assert all(row.utterance_refs == [utterance] for row in rows)
        assert all(row.transport_state == "completed" for row in rows)
        assert all(row.received_argument_hash == first["argument_hash"] for row in rows)
    replay = asyncio.run(server.call_tool("docket_search_history", {
        "query": "same", "invocation_binding": _sign(first),
    }))
    assert _result_envelope(replay)["ok"] is True
    assert calls == ["same", "same", "same"]
    with session_factory() as session:
        rows = list(session.scalars(select(ToolInvocation).where(
            ToolInvocation.trace_execution_id == segment_for(
                session, first["trace_ref"], first["execution_index"],
            ).id
        )))
        assert len(rows) == 2
        assert {row.trace_call_id for row in rows} == {"transport-call-1", None}
        assert {row.trace_ordinal for row in rows} == {1}


@pytest.mark.parametrize("change", [
    {"argument_hash": "0" * 64}, {"tool_name": "docket_commit_changeset"},
    {"contract_hash": "0" * 64}, {"format": True}, {"format": 1}, {"ordinal": True},
    {"issued_at": 1, "expires_at": 2}, {"utterance_ref": new_public_ref("utt")},
])
def test_invalid_signed_scope_is_terminal_without_dispatch(session_factory, change):
    with session_factory.begin() as session:
        utterance = _evidence(session)
    calls = []
    result = asyncio.run(_server(calls).call_tool("docket_search_history", {
        "query": "same", "invocation_binding": _sign(_payload(utterance, **change)),
    }))
    assert _result_envelope(result)["error"]["code"] == "invalid_invocation_binding"
    assert calls == []
    with session_factory() as session:
        row = session.scalar(select(ToolInvocation))
        assert row.transport_state == "completed"
        assert row.domain_state == "rejected"
        assert row.trace_ref is None


def test_binding_is_hidden_and_plugin_signer_matches_service(session_factory):
    with session_factory.begin() as session:
        utterance = _evidence(session)
    spec = importlib.util.spec_from_file_location(
        "binding_plugin", Path("hermes/plugin/docket_discord/__init__.py")
    )
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    payload = _payload(utterance)
    token = plugin._invocation_binding(
        {"trace_ref": payload["trace_ref"], "utterance_ref": utterance,
         **{key: payload[key] for key in (
             "execution_index", "execution_completion_token", "gateway_instance_ref",
         )}},
        {"call_id": payload["call_id"], "ordinal": 1,
         "tool_name": payload["tool_name"], "received_argument_hash": payload["argument_hash"]},
    )
    calls = []
    server = _server(calls)
    tool = asyncio.run(server.list_tools())[0]
    assert tool.inputSchema["properties"]["invocation_binding"]["x-docket-internal"] is True
    result = asyncio.run(server.call_tool("docket_search_history", {
        "query": "same", "invocation_binding": token,
    }))
    assert _result_envelope(result)["ok"] is True
    assert calls == ["same"]
    with session_factory() as session:
        row = session.scalar(select(ToolInvocation))
        assert row.trace_ref == payload["trace_ref"]
        assert token not in repr(row.__dict__)


def test_removed_invocation_format_has_no_decoder(session_factory):
    with session_factory.begin() as session:
        utterance = _evidence(session)
    payload = _payload(utterance, format=1)
    payload.pop("execution_index")
    payload.pop("execution_completion_token")
    encoded = base64.urlsafe_b64encode(json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
    ).encode()).decode().rstrip("=")
    signature = hmac.new(
        get_settings().hermes_to_docket_token().encode(),
        b"docket-mcp-invocation-v1:" + encoded.encode(), hashlib.sha256,
    ).hexdigest()
    calls = []
    result = asyncio.run(_server(calls).call_tool("docket_search_history", {
        "query": "same", "invocation_binding": f"{encoded}.{signature}",
    }))
    assert _result_envelope(result)["error"]["code"] == "invalid_invocation_binding"
    assert calls == []
    with session_factory() as session:
        invocation = session.scalar(select(ToolInvocation))
        assert invocation.domain_state == "rejected" and invocation.trace_execution_id is None


def test_unsigned_and_foreign_utterance_binding_rejects(session_factory):
    with session_factory.begin() as session:
        utterance = _evidence(session)
    calls = []
    server = _server(calls)
    for token in ("not-signed", _sign(_payload(utterance))[:-1] + "Z"):
        result = asyncio.run(server.call_tool("docket_search_history", {
            "query": "same", "invocation_binding": token,
        }))
        assert _result_envelope(result)["error"]["code"] == "invalid_invocation_binding"
    assert calls == []


def test_running_retry_and_foreign_binding_cannot_erase_durable_commit(session_factory):
    with session_factory.begin() as session:
        utterance = _evidence(session)
        trace_ref = new_public_ref("trace")
        common = {
            "trace_ref": trace_ref, "trace_ordinal": 1,
            "tool_name": "docket_commit_changeset", "caller_profile": "interactive",
            "tool_contract_version": CONTRACT_VERSION, "received_argument_hash": "a" * 64,
            "actor_ref": "discord_user:000000000000000001", "utterance_refs": [utterance],
        }
        original = ToolInvocation(
            **common, trace_call_id="original-call", transport_state="completed",
            domain_state="succeeded", result_disposition="committed",
            completed_at=datetime.now(UTC),
        )
        retry = ToolInvocation(**common, trace_call_id=None)
        foreign = ToolInvocation(
            **{**common, "utterance_refs": [new_public_ref("utt")]},
            trace_call_id=None, transport_state="completed", domain_state="succeeded",
            result_disposition="committed", completed_at=datetime.now(UTC),
        )
        session.add_all([original, retry, foreign])
        session.flush()
        assert correlated_calls([original, retry, foreign])[(None, "original-call")] is original


@pytest.mark.parametrize("mismatch", ["source", "argument_hash"])
def test_late_trace_callback_cannot_change_invocation_binding(session_factory, mismatch):
    with session_factory.begin() as session:
        utterance = _evidence(session)
    payload = _payload(utterance)
    calls = []
    result = asyncio.run(_server(calls).call_tool("docket_search_history", {
        "query": "same", "invocation_binding": _sign(payload),
    }))
    assert _result_envelope(result)["ok"] is True
    settings = get_settings()
    with session_factory() as session:
        started = segment_for(session, payload["trace_ref"], payload["execution_index"]).started_at
    request = McpTraceUpdate.model_validate({
        "request_id": "00000000-0000-0000-0000-000000000001",
        "guild_id": settings.discord_guild_id, "source_channel_id": settings.chat_channel_id,
        "source_message_id": "999999999999999998" if mismatch == "source" else "999999999999999999",
        "actor_id": settings.operator_discord_user_id, "caller_profile": "interactive",
        "tool_contract_version": CONTRACT_VERSION,
        "tool_contract_hash": contract_hash("interactive"),
        "turn_started_at": started.replace(tzinfo=UTC), "updated_at": datetime.now(UTC),
        **{key: payload[key] for key in (
            "execution_index", "execution_completion_token", "gateway_instance_ref",
        )},
        "turn_status": "running",
        "call": {
            "call_id": payload["call_id"], "ordinal": 1, "tool_name": payload["tool_name"],
            "execution_boundary": "mcp_attempted", "transport_state": "running", "elapsed_ms": 0,
            "received_argument_hash": "f" * 64 if mismatch == "argument_hash"
            else payload["argument_hash"],
        },
    })
    with pytest.raises(DocketError) as failure, session_factory.begin() as session:
        McpTraceService(session).update(payload["trace_ref"], request)
    assert failure.value.code == (
        "mcp_trace_binding_mismatch" if mismatch == "source" else "mcp_trace_call_conflict"
    )
