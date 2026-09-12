import importlib.util
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from docket.config import get_settings
from docket.domain.public_refs import new_public_ref
from docket.internal_api.schemas import McpTraceCheckpoint
from docket.models import ConversationalToolTrace, OperatorUtterance, ToolInvocation
from docket.services.mcp_traces import McpTraceService


@pytest.fixture
def plugin(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "docket_checkpoint_plugin", "hermes/plugin/docket_discord/__init__.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_enqueue_trace_update", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_validate_authority_arguments_locally", lambda *_args: None)
    return module


def _context(plugin, factory=None):
    settings = get_settings()
    ref = new_public_ref("utt")
    message = "777777777777777778"
    if factory:
        with factory.begin() as session:
            session.add(OperatorUtterance(
                ref_id=ref, actor_ref=f"discord_user:{settings.operator_discord_user_id}",
                transport="discord", source_message_ref=(
                    f"discord_message:{settings.discord_guild_id}:{settings.chat_channel_id}:"
                    f"{message}"
                ), conversation_ref=f"discord_conversation:{settings.chat_channel_id}",
                said_at=datetime.now(UTC), verbatim_text="Test retained request.",
                content_hash="a" * 64, request_key="checkpoint-plugin",
            ))
    context = dict(
        trace_ref=new_public_ref("trace"), utterance_ref=ref,
        guild_id=settings.discord_guild_id, actor_id=settings.operator_discord_user_id,
        source_channel_id=settings.chat_channel_id, source_message_id=message,
        tool_contract_version=plugin._TOOL_CONTRACT_VERSION,
        tool_contract_hash=plugin._TOOL_CONTRACT_HASH, caller_profile="interactive",
        turn_started_at=datetime.now(UTC).isoformat(), gateway_instance_ref=None,
        calls={}, next_ordinal=1, started=False, terminal=False, turn_id=None,
    )
    plugin._TRACE_CONTEXTS["checkpoint-test"] = context
    return context


def _call(ordinal):
    return dict(
        call_id=f"call-{ordinal}", ordinal=ordinal, tool_name="docket_stage_changes",
        execution_boundary="local_rejection", transport_state="completed", elapsed_ms=3,
        disposition="rejected_validation", transport_error_code=None,
        received_argument_hash="b" * 64, argument_preview='{"fields":["patch"]}',
    )


def _checkpoint_backend(factory, sent):
    def send(path, payload, **kwargs):
        sent.append((path, json.loads(json.dumps(payload)), kwargs))
        if path.endswith("/checkpoint"):
            with factory.begin() as session:
                return McpTraceService(session).checkpoint(
                    path.split("/")[-2], McpTraceCheckpoint.model_validate(payload),
                )
        if path.endswith("/agent-responses"):
            return {"ok": True, "ref": new_public_ref("rsp")}
        raise AssertionError(path)
    return send


def test_local_rejection_is_durable_without_queue_or_post_hook(
    plugin, monkeypatch, session_factory,
):
    context = _context(plugin, session_factory)
    sent = []
    monkeypatch.setattr(
        plugin, "_docket_internal_request", _checkpoint_backend(session_factory, sent),
    )
    monkeypatch.setattr(
        plugin, "_validate_authority_arguments_locally", lambda *_args: "Bad schema",
    )
    args = {"patch": {"secret": "private submitted text"}}
    result = plugin._on_pre_tool_call(
        tool_name="mcp__docket__docket_stage_changes", args=args, task_id="checkpoint-test",
        tool_call_id="call-1", turn_id="turn-1",
    )
    assert result == {"action": "block", "message": "Bad schema"}
    assert len(sent) == 1 and sent[0][2] == {"method": "PUT", "timeout": 5}
    assert "private submitted text" not in str(sent)
    assert "invocation_binding" not in args
    with session_factory() as session:
        trace = session.scalar(select(ConversationalToolTrace))
        assert trace.calls[0]["transport_state"] == "completed"
        assert trace.calls[0]["disposition"] == "rejected_validation"
        assert session.scalar(select(func.count(ToolInvocation.id))) == 0
    assert context["trace_checkpoint_pending"] is False


def test_happy_tools_stay_async_then_final_checkpoint_precedes_response_persistence(
    plugin, monkeypatch, session_factory,
):
    context = _context(plugin, session_factory)
    sent = []
    monkeypatch.setattr(
        plugin, "_docket_internal_request", _checkpoint_backend(session_factory, sent),
    )
    keywords = dict(tool_name="mcp__docket__docket_search_history", task_id="checkpoint-test",
                    tool_call_id="call-1", turn_id="turn-1")
    assert plugin._on_pre_tool_call(**keywords, args={"query": "private search"}) is None
    plugin._on_post_tool_call(**keywords, result={"ok": True}, duration_ms=12)
    assert sent == []
    plugin._on_post_llm_call(task_id="checkpoint-test", turn_id="turn-1",
                             assistant_response="Bounded final reply.")
    assert sent[0][0].endswith("/checkpoint")
    assert sent[1][0].endswith("/agent-responses")
    assert "private search" not in str(sent[0])
    assert context["turn_finalized"] is True
    with session_factory() as session:
        trace = session.scalar(select(ConversationalToolTrace))
        assert trace.status == "completed"
        assert trace.calls[0]["reported_disposition"] == "succeeded"
        assert trace.calls[0]["disposition"] is None  # No call_ in this isolated hook fixture.


def test_failed_checkpoint_blocks_subsequent_dispatch_until_durable_recovery(
    plugin, monkeypatch, session_factory,
):
    context = _context(plugin, session_factory)
    failed = []

    def unavailable(*_args, **_kwargs):
        failed.append(True)
        raise OSError("Docket unavailable")

    monkeypatch.setattr(plugin, "_docket_internal_request", unavailable)
    context["calls"]["call-1"] = _call(1)
    context["next_ordinal"] = 2
    assert plugin._checkpoint_trace(context) is False
    assert len(failed) == 3
    args = {"query": "test", "invocation_binding": "untrusted token"}
    keywords = dict(tool_name="mcp__docket__docket_search_history", task_id="checkpoint-test",
                    tool_call_id="call-2", turn_id="turn-1", args=args)
    result = plugin._on_pre_tool_call(**keywords)
    assert result["action"] == "block" and "no new Docket call" in result["message"]
    assert "invocation_binding" not in args and len(failed) == 6
    assert context["next_ordinal"] == 2
    sent = []
    monkeypatch.setattr(
        plugin, "_docket_internal_request", _checkpoint_backend(session_factory, sent),
    )
    assert plugin._on_pre_tool_call(**keywords) is None
    assert context["trace_checkpoint_pending"] is False
    assert "invocation_binding" in args
    with session_factory() as session:
        trace = session.scalar(select(ConversationalToolTrace))
        assert trace.last_ordinal == 1 and trace.calls[0]["execution_boundary"] == "local_rejection"


def test_lost_checkpoint_acknowledgement_reuses_exact_page_after_commit(
    plugin, monkeypatch, session_factory,
):
    context = _context(plugin, session_factory)
    context["calls"]["call-1"] = _call(1)
    sent = []
    backend = _checkpoint_backend(session_factory, sent)

    def lose_response(path, payload, **kwargs):
        result = backend(path, payload, **kwargs)
        if len(sent) == 1:
            raise OSError("Response lost after commit")
        return result

    monkeypatch.setattr(plugin, "_docket_internal_request", lose_response)
    assert plugin._checkpoint_trace(context, turn_status="completed") is True
    assert len(sent) == 2 and sent[0] == sent[1]
    with session_factory() as session:
        trace = session.scalar(select(ConversationalToolTrace))
        assert trace.version == 1 and len(trace.calls) == 1


def test_checkpoint_uses_bounded_pages_and_can_resume_partial_delivery(plugin, monkeypatch):
    context = _context(plugin)
    for ordinal in range(1, 104):
        call = {**_call(ordinal), "argument_preview": '"' + "界" * 250 + '"'}
        context["calls"][call["call_id"]] = call
    sent = []
    failing = True

    def send(path, payload, **kwargs):
        sent.append(payload)
        if failing and payload["calls"][0]["ordinal"] > 1:
            raise OSError("Later page unavailable")
        assert len(json.dumps(payload, separators=(",", ":")).encode()) <= 16_384
        assert len(payload["calls"]) <= 25
        McpTraceCheckpoint.model_validate(payload)
        return {"ok": True, "trace_ref": context["trace_ref"], "disposition": "updated"}

    monkeypatch.setattr(plugin, "_docket_internal_request", send)
    assert plugin._checkpoint_trace(context, turn_status="completed") is False
    assert all(page["turn_status"] == "running" for page in sent)
    failing = False
    sent.clear()
    assert plugin._checkpoint_trace(context, turn_status="completed") is True
    assert len(sent) > 5  # Wire-byte cap, not only the entry-count cap, determines pages.
    assert [call["ordinal"] for page in sent for call in page["calls"]] == list(range(1, 104))
    assert sent[-1]["turn_status"] == "completed"
    assert all(page["turn_status"] == "running" for page in sent[:-1])


def test_failed_completion_telemetry_does_not_suppress_final_domain_response(plugin, monkeypatch):
    context = _context(plugin)
    context["calls"]["call-1"] = _call(1)
    context["started"] = True
    responses = []

    def send(path, payload, **kwargs):
        if path.endswith("/checkpoint"):
            raise OSError("Trace capture unavailable")
        assert path.endswith("/agent-responses")
        responses.append(payload)
        return {"ok": True, "ref": new_public_ref("rsp")}

    monkeypatch.setattr(plugin, "_docket_internal_request", send)
    plugin._on_post_llm_call(
        task_id="checkpoint-test", assistant_response="Canonical commit recorded.",
    )
    assert context["turn_finalized"] is True
    assert context["trace_checkpoint_pending"] is True
    assert len(responses) == 1
