import importlib.util
import sys
import uuid
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from docket.security import (
    issue_projection_approval_token,
    issue_projection_decision_approval_token,
    issue_projection_local_action_token,
    issue_projection_proposal_control_token,
    issue_projection_review_navigation_token,
)

PLUGIN_PATH = Path("hermes/plugin/docket_discord/__init__.py")


class Platform(Enum):
    DISCORD = "discord"


@pytest.fixture
def plugin_module(monkeypatch):
    spec = importlib.util.spec_from_file_location("docket_discord_plugin", PLUGIN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        "_capture_operator_utterance",
        lambda _event: f"utt_{'0' * 26}",
    )

    def fake_internal_request(path, _payload, **_kwargs):
        if path == "/internal/v1/discord/agent-responses":
            return {"ok": True, "ref": f"rsp_{'1' * 26}", "state": "pending"}
        return {"ok": True}

    monkeypatch.setattr(module, "_docket_internal_request", fake_internal_request)
    return module


def test_repeated_plugin_loads_share_one_process_registration_key() -> None:
    def load(name: str) -> ModuleType:
        spec = importlib.util.spec_from_file_location(name, PLUGIN_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    first = load("docket_discord_plugin_first")
    second = load("docket_discord_plugin_second")

    assert first._GATEWAY_REGISTRATION_KEY == second._GATEWAY_REGISTRATION_KEY


@pytest.mark.parametrize(
    ("profile_name", "argv", "expected"),
    [
        ("default", ["hermes", "gateway", "run", "--replace"], True),
        ("docket-triage", ["hermes", "gateway", "run", "--replace"], False),
        ("default", ["hermes", "cron", "install"], False),
        ("default", ["hermes", "profiles", "reconcile"], False),
    ],
)
def test_only_default_gateway_runtime_owns_gateway_lifetime(
    plugin_module,
    monkeypatch,
    profile_name: str,
    argv: list[str],
    expected: bool,
) -> None:
    monkeypatch.setattr(plugin_module.sys, "argv", argv)

    assert (
        plugin_module._owns_discord_gateway_lifetime(
            SimpleNamespace(profile_name=profile_name)
        )
        is expected
    )


def test_plugin_accepts_version_bound_schedule_decision_token(plugin_module) -> None:
    approval_id = uuid.uuid4()
    projection_id = uuid.uuid4()
    token = issue_projection_decision_approval_token(
        approval_id,
        projection_id,
        7,
        datetime.now(UTC) + timedelta(minutes=15),
        b"test-signing-key",
    )

    assert plugin_module._CONTROL_ID.fullmatch(f"dkt:a:{token}")
    assert plugin_module._decode_control(token) == (approval_id, projection_id)


@pytest.mark.adversarial
def test_unauthorized_actor_is_dropped_before_model(plugin_module, monkeypatch) -> None:
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", "operator")
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", "guild")
    monkeypatch.setenv("DOCKET_QUEUE_CHANNEL_ID", "queue")
    delivered = False

    def fake_post(**_kwargs) -> None:
        nonlocal delivered
        delivered = True

    monkeypatch.setattr(plugin_module, "_post_decision", fake_post)
    event = SimpleNamespace(
        text="/docket approve ABCDEFGH",
        message_id="message",
        source=SimpleNamespace(
            platform="discord",
            user_id="attacker",
            guild_id="guild",
            chat_id="queue",
        ),
    )

    result = plugin_module._pre_gateway_dispatch(event)
    assert result == {"action": "skip", "reason": "unauthorized-docket-control"}
    assert delivered is False


@pytest.mark.adversarial
@pytest.mark.parametrize("prefix", ["", "/"])
def test_authorized_control_is_handled_without_model(
    plugin_module, monkeypatch, prefix: str
) -> None:
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", "operator")
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", "guild")
    monkeypatch.setenv("DOCKET_QUEUE_CHANNEL_ID", "queue")
    captured = {}

    def fake_post(**kwargs) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(plugin_module, "_post_decision", fake_post)
    event = SimpleNamespace(
        text=f"{prefix}docket reject ABCDEFGH",
        message_id="message",
        source=SimpleNamespace(
            platform="discord",
            user_id="operator",
            guild_id="guild",
            chat_id="queue",
        ),
    )

    result = plugin_module._pre_gateway_dispatch(event)
    assert result == {"action": "skip", "reason": "docket-control-handled"}
    assert captured["decision"] == "reject"


@pytest.mark.adversarial
def test_non_command_queue_message_is_dropped_before_model(plugin_module, monkeypatch) -> None:
    monkeypatch.setenv("DOCKET_QUEUE_CHANNEL_ID", "queue")
    event = SimpleNamespace(
        text="please approve whatever is pending",
        message_id="message",
        source=SimpleNamespace(
            platform="discord",
            user_id="operator",
            guild_id="guild",
            chat_id="queue",
        ),
    )

    result = plugin_module._pre_gateway_dispatch(event)

    assert result == {"action": "skip", "reason": "invalid-docket-control"}


@pytest.mark.adversarial
def test_docket_mcp_hooks_emit_only_bounded_trace_metadata(plugin_module, monkeypatch) -> None:
    actor = "111111111111111111"
    guild = "222222222222222222"
    chat = "333333333333333333"
    message_id = "444444444444444444"
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", actor)
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", chat)
    monkeypatch.setenv("DOCKET_QUEUE_CHANNEL_ID", "555555555555555555")
    monkeypatch.setenv("DOCKET_SYSTEM_CHANNEL_ID", "666666666666666666")
    emitted = []
    monkeypatch.setattr(
        plugin_module,
        "_enqueue_trace_update",
        lambda context, **kwargs: emitted.append((dict(context), kwargs)),
    )
    event = SimpleNamespace(
        text="Find my term.",
        message_id=message_id,
        source=SimpleNamespace(
            platform="discord",
            user_id=actor,
            guild_id=guild,
            chat_id=chat,
        ),
    )
    store = SimpleNamespace(
        get_or_create_session=lambda _source: SimpleNamespace(session_id="session-1")
    )

    rewritten = plugin_module._pre_gateway_dispatch(event, session_store=store)
    assert rewritten is not None and rewritten["action"] == "rewrite"
    assert '<docket_tool_contract trusted="true">' in rewritten["text"]
    assert f"contract_version: {plugin_module._TOOL_CONTRACT_VERSION}" in rewritten["text"]
    assert f"contract_hash: {plugin_module._TOOL_CONTRACT_HASH}" in rewritten["text"]
    assert "profile: interactive" in rewritten["text"]
    assert "without a redundant approval phase" in rewritten["text"]
    plugin_module._on_pre_tool_call(
        tool_name="mcp__docket__docket_search_records",
        args={
            "query": "Cal Poly Mustang Shop",
            "authorization": "secret bearer value",
        },
        task_id="session-1",
        session_id="session-1",
        tool_call_id="call-1",
        turn_id="turn-1",
    )
    plugin_module._on_post_tool_call(
        tool_name="mcp__docket__docket_search_records",
        result='{"ok":true,"records":[{"title":"secret result body"}]}',
        task_id="session-1",
        session_id="session-1",
        tool_call_id="call-1",
        turn_id="turn-1",
        duration_ms=42,
        status="succeeded",
    )
    plugin_module._on_post_llm_call(
        task_id="session-1",
        session_id="session-1",
        turn_id="turn-1",
        assistant_response="secret model response",
    )

    assert len(emitted) == 3
    assert emitted[0][0]["tool_contract_version"] == plugin_module._TOOL_CONTRACT_VERSION
    assert emitted[0][0]["tool_contract_hash"] == plugin_module._TOOL_CONTRACT_HASH
    assert emitted[0][0]["caller_profile"] == "interactive"
    running = emitted[0][1]["call"]
    terminal = emitted[1][1]["call"]
    assert running == {
        "call_id": "call-1",
        "ordinal": 1,
        "tool_name": "docket_search_records",
        "transport_state": "running",
        "elapsed_ms": 0,
        "disposition": None,
        "transport_error_code": None,
        "argument_preview": '{"fields":["authorization","query"]}',
        "received_argument_hash": (
            "8c35d71f49d364f15cbf026ee0a05ed5011e35fc2b0d0897fd48b801a47f13da"
        ),
    }
    assert terminal["transport_state"] == "completed"
    assert terminal["elapsed_ms"] == 42
    assert emitted[2][1] == {"turn_status": "completed"}
    assert "Cal Poly Mustang Shop" not in str(emitted)
    assert "secret bearer value" not in str(emitted)
    assert "secret result body" not in str(emitted)
    assert "secret model response" not in str(emitted)

    plugin_module._on_pre_tool_call(
        tool_name="terminal",
        task_id="session-1",
        tool_call_id="call-2",
        turn_id="turn-1",
    )
    assert len(emitted) == 3


@pytest.mark.adversarial
def test_changeset_argument_preview_is_semantic_and_never_raw(plugin_module) -> None:
    preview = plugin_module._argument_preview(
        "docket_commit_changeset",
        {
            "utterance_ref": "utt_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "resolved_intent": {"secret": "I already applied for this one"},
            "content": {
                "resolution_changes": [
                    {
                        "object_type": "attention_case_resolution",
                        "object_ref": "case_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                        "case_revision_ref": "caserev_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                        "case_outcome": "resolved",
                        "item_dispositions": [
                            {
                                "case_item_ref": "citem_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                                "disposition": "resolved",
                            }
                        ],
                    }
                ]
            },
        },
    )
    assert "case_01ARZ3NDEKTSV4RRFFQ69G5FAV" in preview
    assert "caserev_01ARZ3NDEKTSV4RRFFQ69G5FAV" in preview
    assert '"case_outcome":"resolved"' in preview
    assert '"explicit_item_dispositions":1' in preview
    assert "I already applied" not in preview
    assert "item_01ARZ3NDEKTSV4RRFFQ69G5FAV" not in preview


@pytest.mark.adversarial
def test_terminal_trace_preserves_needs_clarification_disposition(plugin_module) -> None:
    call = {
        "ordinal": 1,
        "tool_name": "docket_commit_changeset",
        "transport_state": "running",
        "elapsed_ms": 0,
        "disposition": None,
        "transport_error_code": None,
        "argument_preview": '{"effects":["case resolution"]}',
    }

    terminal = plugin_module._terminal_trace_call(
        call,
        result={"ok": True, "disposition": "needs_clarification"},
        duration_ms=17,
        status="completed",
        error_type=None,
    )

    assert terminal["transport_state"] == "completed"
    assert terminal["disposition"] == "needs_clarification"
    assert terminal["transport_error_code"] is None


@pytest.mark.adversarial
def test_empty_model_turn_is_reported_as_no_response(plugin_module, monkeypatch) -> None:
    actor = "111111111111111111"
    guild = "222222222222222222"
    chat = "333333333333333333"
    message_id = "444444444444444444"
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", actor)
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", chat)
    monkeypatch.setenv("DOCKET_QUEUE_CHANNEL_ID", "555555555555555555")
    monkeypatch.setenv("DOCKET_SYSTEM_CHANNEL_ID", "666666666666666666")
    requests: list[tuple[str, dict[str, object]]] = []

    def fake_request(path, payload, **_kwargs):
        requests.append((path, dict(payload)))
        return {"ok": True}

    monkeypatch.setattr(plugin_module, "_docket_internal_request", fake_request)
    monkeypatch.setattr(plugin_module, "_enqueue_trace_update", lambda *_args, **_kwargs: None)
    event = SimpleNamespace(
        text="Do the bounded thing.",
        message_id=message_id,
        source=SimpleNamespace(
            platform="discord",
            user_id=actor,
            guild_id=guild,
            chat_id=chat,
        ),
    )
    store = SimpleNamespace(
        get_or_create_session=lambda _source: SimpleNamespace(session_id="session-empty")
    )
    plugin_module._pre_gateway_dispatch(event, session_store=store)
    plugin_module._on_post_llm_call(
        task_id="session-empty",
        session_id="session-empty",
        turn_id="turn-empty",
        assistant_response="",
    )

    assert requests[-1][0] == "/internal/v1/discord/agent-turns/no-response"
    assert requests[-1][1]["utterance_ref"].startswith("utt_")
    assert requests[-1][1]["source_message_id"] == message_id


@pytest.mark.adversarial
def test_empty_signoff_turn_persists_deterministic_response(plugin_module, monkeypatch) -> None:
    actor = "111111111111111111"
    guild = "222222222222222222"
    chat = "333333333333333333"
    message_id = "444444444444444444"
    decision_ref = f"dec_{'3' * 26}"
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", actor)
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", chat)
    monkeypatch.setenv("DOCKET_QUEUE_CHANNEL_ID", "555555555555555555")
    monkeypatch.setenv("DOCKET_SYSTEM_CHANNEL_ID", "666666666666666666")
    requests: list[tuple[str, dict[str, object]]] = []

    def fake_request(path, payload, **_kwargs):
        requests.append((path, dict(payload)))
        if path == "/internal/v1/discord/agent-responses":
            return {"ok": True, "ref": f"rsp_{'4' * 26}", "state": "pending"}
        return {"ok": True}

    monkeypatch.setattr(plugin_module, "_docket_internal_request", fake_request)
    monkeypatch.setattr(
        plugin_module,
        "_record_final_signoff_if_explicit",
        lambda _event, _ref: {
            "ok": True,
            "ref": decision_ref,
            "authorized_scope": "tracked_context_test_scope",
            "production_reset_authority": False,
        },
    )
    monkeypatch.setattr(plugin_module, "_enqueue_trace_update", lambda *_args, **_kwargs: None)
    event = SimpleNamespace(
        text=plugin_module._FINAL_ARCHITECTURE_SIGNOFF_TEXT,
        message_id=message_id,
        source=SimpleNamespace(
            platform="discord",
            user_id=actor,
            guild_id=guild,
            chat_id=chat,
        ),
    )
    store = SimpleNamespace(
        get_or_create_session=lambda _source: SimpleNamespace(
            session_id="session-signoff",
            session_key="session-signoff",
        )
    )

    plugin_module._pre_gateway_dispatch(event, session_store=store)
    plugin_module._on_post_llm_call(
        task_id="session-signoff",
        session_id="session-signoff",
        turn_id="turn-signoff",
        assistant_response="",
    )

    assert requests[-1][0] == "/internal/v1/discord/agent-responses"
    assert requests[-1][1]["model_identifier"] == "docket-deterministic-signoff-v1"
    assert decision_ref in str(requests[-1][1]["verbatim_text"])
    assert not any(path.endswith("/no-response") for path, _payload in requests)


@pytest.mark.adversarial
def test_exact_signoff_persists_and_schedules_confirmation_without_model_turn(
    plugin_module, monkeypatch
) -> None:
    actor = "111111111111111111"
    guild = "222222222222222222"
    chat = "333333333333333333"
    message_id = "444444444444444445"
    decision_ref = f"dec_{'5' * 26}"
    response_ref = f"rsp_{'6' * 26}"
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", actor)
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", chat)
    monkeypatch.setenv("DOCKET_QUEUE_CHANNEL_ID", "555555555555555555")
    monkeypatch.setenv("DOCKET_SYSTEM_CHANNEL_ID", "666666666666666666")
    requests: list[tuple[str, dict[str, object]]] = []
    scheduled: list[dict[str, object]] = []

    def fake_request(path, payload, **_kwargs):
        requests.append((path, dict(payload)))
        if path == "/internal/v1/discord/agent-responses":
            return {"ok": True, "ref": response_ref, "state": "pending"}
        return {"ok": True}

    monkeypatch.setattr(plugin_module, "_docket_internal_request", fake_request)
    monkeypatch.setattr(
        plugin_module,
        "_record_final_signoff_if_explicit",
        lambda _event, _ref: {
            "ok": True,
            "ref": decision_ref,
            "authorized_scope": "tracked_context_test_scope",
            "production_reset_authority": False,
        },
    )
    monkeypatch.setattr(
        plugin_module,
        "_schedule_persisted_deterministic_response",
        lambda context: scheduled.append(dict(context)),
    )
    event = SimpleNamespace(
        text=plugin_module._FINAL_ARCHITECTURE_SIGNOFF_TEXT,
        message_id=message_id,
        source=SimpleNamespace(
            platform="discord",
            user_id=actor,
            guild_id=guild,
            chat_id=chat,
        ),
    )
    store = SimpleNamespace(
        get_or_create_session=lambda _source: SimpleNamespace(
            session_id="session-direct-signoff",
            session_key="session-direct-signoff",
        )
    )

    result = plugin_module._pre_gateway_dispatch(event, session_store=store)

    assert result == {"action": "skip", "reason": "docket-signoff-handled"}
    assert requests[-1][0] == "/internal/v1/discord/agent-responses"
    assert requests[-1][1]["model_identifier"] == "docket-deterministic-signoff-v1"
    assert scheduled[0]["response_ref"] == response_ref
    assert decision_ref in str(scheduled[0]["deterministic_response_text"])
    assert "tracked_context_test_scope" in str(scheduled[0]["deterministic_response_text"])
    assert "does not authorize production deployment" in str(
        scheduled[0]["deterministic_response_text"]
    )


@pytest.mark.asyncio
@pytest.mark.adversarial
async def test_processing_completion_delivers_persisted_signoff_fallback(
    plugin_module, monkeypatch
) -> None:
    deliveries: list[tuple[dict[str, object], bool]] = []

    async def fake_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    class FakeAdapter:
        async def on_processing_complete(self, _event, _outcome) -> None:
            return None

        async def send(self, **kwargs):
            self.sent = kwargs
            return SimpleNamespace(success=True)

    adapter = FakeAdapter()
    context = {
        "response_ref": f"rsp_{'4' * 26}",
        "deterministic_response_text": "Signed and recorded.",
        "guild_id": "222222222222222222",
        "source_channel_id": "333333333333333333",
        "source_message_id": "444444444444444444",
        "actor_id": "111111111111111111",
    }
    monkeypatch.setattr(
        plugin_module,
        "_trace_context_for_event",
        lambda _event, _adapter: context,
    )
    monkeypatch.setattr(
        plugin_module,
        "_post_agent_response_delivery",
        lambda payload, *, delivered: deliveries.append((payload, delivered)),
    )
    monkeypatch.setattr(plugin_module.asyncio, "to_thread", fake_to_thread)
    plugin_module._install_processing_outcome_listener(adapter)

    await adapter.on_processing_complete(SimpleNamespace(), "success")

    assert adapter.sent == {
        "chat_id": "333333333333333333",
        "content": "Signed and recorded.",
        "reply_to": "444444444444444444",
        "metadata": {"notify": True},
    }
    assert len(deliveries) == 1
    assert deliveries[0][0]["response_ref"] == context["response_ref"]
    assert deliveries[0][1] is True


@pytest.mark.asyncio
async def test_mcp_trace_projection_creates_then_edits_one_system_message(
    plugin_module, monkeypatch
) -> None:
    guild_id = "222222222222222222"
    channel_id = "333333333333333333"
    trace_ref = plugin_module._new_trace_ref()
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild_id)
    monkeypatch.setenv("DOCKET_SYSTEM_CHANNEL_ID", channel_id)

    class FakeEmbed:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.fields = []
            self.footer = None

        def add_field(self, **kwargs) -> None:
            self.fields.append(kwargs)

        def set_footer(self, **kwargs) -> None:
            self.footer = SimpleNamespace(text=kwargs["text"])

    class FakeMessage:
        def __init__(self, embed) -> None:
            self.id = 555555555555555555
            self.author = SimpleNamespace(id=999999999999999999)
            self.embeds = [embed]
            self.edit_count = 0

        async def edit(self, *, embed, **_kwargs):
            self.embeds = [embed]
            self.edit_count += 1
            return self

    class FakeTextChannel:
        def __init__(self) -> None:
            self.guild = SimpleNamespace(id=int(guild_id))
            self.messages = []

        async def history(self, **_kwargs):
            for message in self.messages:
                yield message

        async def send(self, *, embed, **_kwargs):
            message = FakeMessage(embed)
            self.messages.append(message)
            return message

    channel = FakeTextChannel()
    client = SimpleNamespace(
        user=SimpleNamespace(id=999999999999999999),
        fetch_channel=lambda _channel_id: None,
    )

    async def fetch_channel(_channel_id):
        return channel

    client.fetch_channel = fetch_channel
    fake_discord = SimpleNamespace(
        Embed=FakeEmbed,
        TextChannel=FakeTextChannel,
        NotFound=type("NotFound", (Exception,), {}),
        AllowedMentions=SimpleNamespace(none=lambda: None),
        utils=SimpleNamespace(
            escape_mentions=lambda value: value,
            escape_markdown=lambda value: value,
        ),
    )
    monkeypatch.setitem(sys.modules, "discord", fake_discord)
    monkeypatch.setattr(
        plugin_module,
        "_discord_runtime",
        lambda: (None, None, client),
    )
    render = {
        "title": "Docket tool activity",
        "summary": "Trusted docket-chat request\n1 Docket call",
        "status": "Completed",
        "calls": [
            {
                "ordinal": 1,
                "tool_name": "docket_search_history",
                "transport_state": "completed",
                "domain_state": "succeeded",
                "elapsed_ms": 42,
                "outcome": "succeeded",
                "tool_call_ref": "call_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "transport_error_code": "none",
                "argument_preview": '{"fields":["query"]}',
            }
        ],
        "overflow_count": 0,
        "updated_at": "<t:1784940000:F> · <t:1784940000:R>",
    }
    digest = plugin_module.hashlib.sha256(
        plugin_module.json.dumps(
            render,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    payload = {
        "request_id": str(uuid.uuid4()),
        "trace_ref": trace_ref,
        "guild_id": guild_id,
        "channel_id": channel_id,
        "render": render,
        "render_sha256": digest,
    }

    first = await plugin_module._put_mcp_trace(trace_ref, payload)
    second = await plugin_module._put_mcp_trace(
        trace_ref,
        {**payload, "request_id": str(uuid.uuid4())},
    )

    assert first["created"] is True
    assert second["created"] is False
    assert len(channel.messages) == 1
    assert channel.messages[0].edit_count == 1
    assert channel.messages[0].embeds[0].fields[1]["name"] == ("1. docket_search_history")
    value = channel.messages[0].embeds[0].fields[1]["value"]
    assert value.startswith("Outcome: Succeeded")
    assert "Transport: Completed" in value
    assert "Domain: Succeeded" in value


@pytest.mark.adversarial
def test_authorized_daily_thread_message_reaches_model_with_thread_provenance(
    plugin_module, monkeypatch
) -> None:
    actor = "111111111111111111"
    guild = "222222222222222222"
    chat = "333333333333333333"
    queue = "444444444444444444"
    thread = "555555555555555555"
    message = "666666666666666666"
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", actor)
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", chat)
    monkeypatch.setenv("DOCKET_QUEUE_CHANNEL_ID", queue)
    event = SimpleNamespace(
        text="please explain this card",
        message_id=message,
        source=SimpleNamespace(
            platform="discord",
            user_id=actor,
            guild_id=guild,
            chat_id=thread,
            parent_chat_id=queue,
        ),
    )
    store = SimpleNamespace(
        get_or_create_session=lambda _source: SimpleNamespace(session_id="thread-session")
    )

    rewritten = plugin_module._pre_gateway_dispatch(event, session_store=store)

    assert rewritten is not None and rewritten["action"] == "rewrite"
    assert f'"channel_id": "{thread}"' in rewritten["text"]
    assert f'"parent_channel_id": "{queue}"' in rewritten["text"]
    assert f'"request_key": "discord:{guild}:{thread}:{message}:0"' in rewritten["text"]
    trace_context = plugin_module._TRACE_CONTEXTS["thread-session"]
    assert trace_context["source_channel_id"] == thread
    assert trace_context["source_message_id"] == message


@pytest.mark.adversarial
def test_unauthorized_daily_thread_message_is_dropped_before_model(
    plugin_module, monkeypatch
) -> None:
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", "111111111111111111")
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", "222222222222222222")
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", "333333333333333333")
    monkeypatch.setenv("DOCKET_QUEUE_CHANNEL_ID", "444444444444444444")
    event = SimpleNamespace(
        text="please explain this card",
        message_id="666666666666666666",
        source=SimpleNamespace(
            platform="discord",
            user_id="999999999999999999",
            guild_id="222222222222222222",
            chat_id="555555555555555555",
            parent_chat_id="444444444444444444",
        ),
    )

    assert plugin_module._pre_gateway_dispatch(event) == {
        "action": "skip",
        "reason": "unauthorized-docket-thread",
    }


@pytest.mark.adversarial
def test_system_surface_is_output_only(plugin_module, monkeypatch) -> None:
    monkeypatch.setenv("DOCKET_SYSTEM_CHANNEL_ID", "system")
    event = SimpleNamespace(
        text="@Hermes explain this alert",
        message_id="message",
        source=SimpleNamespace(
            platform="discord",
            user_id="operator",
            guild_id="guild",
            chat_id="system",
        ),
    )

    assert plugin_module._pre_gateway_dispatch(event) == {
        "action": "skip",
        "reason": "docket-system-output-only",
    }


@pytest.mark.adversarial
@pytest.mark.parametrize("command", ["/sethome", "/hermes sethome", "/cron list"])
def test_generic_delivery_commands_are_blocked_in_chat(
    plugin_module, monkeypatch, command: str
) -> None:
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", "chat")
    monkeypatch.setenv("DOCKET_QUEUE_CHANNEL_ID", "queue")
    event = SimpleNamespace(
        text=command,
        message_id="message",
        source=SimpleNamespace(
            platform="discord",
            user_id="operator",
            guild_id="guild",
            chat_id="chat",
        ),
    )

    assert plugin_module._pre_gateway_dispatch(event) == {
        "action": "skip",
        "reason": "docket-generic-delivery-disabled",
    }


@pytest.mark.adversarial
@pytest.mark.parametrize(
    ("guild_id", "channel_id"),
    [("", "queue"), ("other-guild", "queue"), ("guild", "other-channel")],
)
def test_control_is_rejected_outside_trusted_context(
    plugin_module, monkeypatch, guild_id: str, channel_id: str
) -> None:
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", "operator")
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", "guild")
    monkeypatch.setenv("DOCKET_QUEUE_CHANNEL_ID", "queue")
    delivered = False

    def fake_post(**_kwargs) -> None:
        nonlocal delivered
        delivered = True

    monkeypatch.setattr(plugin_module, "_post_decision", fake_post)
    event = SimpleNamespace(
        text="/docket approve ABCDEFGH",
        message_id="message",
        source=SimpleNamespace(
            platform="discord",
            user_id="operator",
            guild_id=guild_id,
            chat_id=channel_id,
        ),
    )

    result = plugin_module._pre_gateway_dispatch(event)
    assert result == {"action": "skip", "reason": "unauthorized-docket-control"}
    assert delivered is False


def test_authorized_chat_receives_verified_source_context(plugin_module, monkeypatch) -> None:
    actor = "111111111111111111"
    guild = "222222222222222222"
    channel = "333333333333333333"
    message = "444444444444444444"
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", actor)
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", channel)
    event = SimpleNamespace(
        text="Store my Fall 2026 term",
        message_id=message,
        source=SimpleNamespace(
            platform="discord",
            user_id=actor,
            guild_id=guild,
            chat_id=channel,
        ),
    )

    result = plugin_module._pre_gateway_dispatch(event)

    assert result is not None and result["action"] == "rewrite"
    assert result["text"].startswith(event.text)
    assert f'"request_key": "discord:{guild}:{channel}:{message}:0"' in result["text"]
    assert f'"actor_id": "{actor}"' in result["text"]
    assert "Reads do not consume an intent index" in result["text"]
    assert "one ChangeSet" in result["text"]
    assert "current authenticated OperatorUtterance supplies authority" in result["text"]
    assert "do not split one request across legacy mutations" in result["text"]


def test_projection_reply_binding_is_injected_as_trusted_context(
    plugin_module, monkeypatch
) -> None:
    actor = "111111111111111111"
    guild = "222222222222222222"
    channel = "333333333333333333"
    message = "444444444444444444"
    case_ref = f"case_{'2' * 26}"
    revision_ref = f"case_{'3' * 26}"
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", actor)
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", channel)
    monkeypatch.setattr(
        plugin_module,
        "_capture_operator_utterance",
        lambda _event: (
            f"utt_{'0' * 26}",
            {
                "kind": "attention_case",
                "primary_ref": case_ref,
                "primary_revision_ref": revision_ref,
                "case_refs": [case_ref],
                "case_revision_refs": [revision_ref],
                "brief_ref": None,
                "trusted_context_refs": [f"ctx_{'4' * 26}"],
            },
        ),
    )
    event = SimpleNamespace(
        text="Register the context but skip the meeting.",
        message_id=message,
        source=SimpleNamespace(
            platform="discord",
            user_id=actor,
            guild_id=guild,
            chat_id=channel,
        ),
    )

    result = plugin_module._pre_gateway_dispatch(event)

    assert result is not None and result["action"] == "rewrite"
    assert '"reply_binding"' in result["text"]
    assert case_ref in result["text"]
    assert revision_ref in result["text"]


@pytest.mark.adversarial
def test_trusted_ingress_fails_closed_when_utterance_cannot_persist(
    plugin_module, monkeypatch
) -> None:
    actor = "111111111111111111"
    guild = "222222222222222222"
    channel = "333333333333333333"
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", actor)
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", channel)

    def fail_capture(_event):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(plugin_module, "_capture_operator_utterance", fail_capture)
    event = SimpleNamespace(
        text="This must not reach interpretation.",
        message_id="444444444444444444",
        source=SimpleNamespace(
            platform="discord",
            user_id=actor,
            guild_id=guild,
            chat_id=channel,
        ),
    )

    assert plugin_module._pre_gateway_dispatch(event) == {
        "action": "skip",
        "reason": "docket-utterance-persistence-failed",
    }


@pytest.mark.adversarial
def test_exact_final_signoff_is_recorded_before_model_dispatch(plugin_module, monkeypatch) -> None:
    actor = "111111111111111111"
    guild = "222222222222222222"
    channel = "333333333333333333"
    message = "444444444444444444"
    utterance_ref = f"utt_{'2' * 26}"
    captured = []
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", actor)
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", channel)
    monkeypatch.setattr(
        plugin_module,
        "_capture_operator_utterance",
        lambda _event: utterance_ref,
    )

    def record_signoff(event, ref):
        captured.append((event.text, ref))
        return {
            "ok": True,
            "ref": f"dec_{'3' * 26}",
            "authorized_scope": None,
            "production_reset_authority": False,
        }

    monkeypatch.setattr(
        plugin_module,
        "_record_final_signoff_if_explicit",
        record_signoff,
    )
    event = SimpleNamespace(
        text=plugin_module._FINAL_ARCHITECTURE_SIGNOFF_TEXT,
        message_id=message,
        source=SimpleNamespace(
            platform="discord",
            user_id=actor,
            guild_id=guild,
            chat_id=channel,
        ),
    )

    result = plugin_module._pre_gateway_dispatch(event)

    assert result is not None and result["action"] == "rewrite"
    assert captured == [(plugin_module._FINAL_ARCHITECTURE_SIGNOFF_TEXT, utterance_ref)]
    assert '"decision_ref": "dec_33333333333333333333333333"' in result["text"]
    assert '"ok": true' in result["text"]
    assert "already persisted this exact specification sign-off" in result["text"]


@pytest.mark.adversarial
def test_rejected_signoff_reaches_model_with_safe_failure_context(
    plugin_module, monkeypatch
) -> None:
    actor = "111111111111111111"
    guild = "222222222222222222"
    channel = "333333333333333333"
    message = "444444444444444444"
    utterance_ref = f"utt_{'2' * 26}"
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", actor)
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", channel)
    monkeypatch.setattr(
        plugin_module,
        "_capture_operator_utterance",
        lambda _event: utterance_ref,
    )

    def reject_signoff(_event, _ref):
        raise plugin_module.PluginAPIError(
            "specification_signoff_artifact_mismatch",
            "Specification sign-off does not identify an eligible frozen artifact.",
        )

    monkeypatch.setattr(
        plugin_module,
        "_record_final_signoff_if_explicit",
        reject_signoff,
    )
    event = SimpleNamespace(
        text=(
            "I accept ONT-DELTA-2026-08-28-UNKNOWN frozen at SHA-256 "
            f"{'9' * 64} and authorize implementation of that amendment."
        ),
        message_id=message,
        source=SimpleNamespace(
            platform="discord",
            user_id=actor,
            guild_id=guild,
            chat_id=channel,
        ),
    )

    result = plugin_module._pre_gateway_dispatch(event)

    assert result is not None and result["action"] == "rewrite"
    assert '"ok": false' in result["text"]
    assert '"error_code": "specification_signoff_artifact_mismatch"' in result["text"]
    assert "It did not create implementation authority" in result["text"]


@pytest.mark.adversarial
def test_unknown_signoff_outcome_remains_fail_closed(plugin_module, monkeypatch) -> None:
    actor = "111111111111111111"
    guild = "222222222222222222"
    channel = "333333333333333333"
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", actor)
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", channel)
    monkeypatch.setattr(
        plugin_module,
        "_capture_operator_utterance",
        lambda _event: f"utt_{'2' * 26}",
    )

    def lose_signoff_outcome(_event, _ref):
        raise plugin_module.PluginAPIError(
            "invalid_decision_ref",
            "Docket did not return a typed Decision reference",
            502,
        )

    monkeypatch.setattr(
        plugin_module,
        "_record_final_signoff_if_explicit",
        lose_signoff_outcome,
    )
    event = SimpleNamespace(
        text=plugin_module._FINAL_ARCHITECTURE_SIGNOFF_TEXT,
        message_id="444444444444444444",
        source=SimpleNamespace(
            platform="discord",
            user_id=actor,
            guild_id=guild,
            chat_id=channel,
        ),
    )

    assert plugin_module._pre_gateway_dispatch(event) == {
        "action": "skip",
        "reason": "docket-signoff-persistence-failed",
    }


@pytest.mark.adversarial
def test_amendment_signoff_forwards_exact_binding_only(plugin_module, monkeypatch) -> None:
    document_ref = "ONT-DELTA-2026-08-28-CASE-RESOLUTION"
    frozen_hash = "058788ec6728565b51bbce3e80d51146c52fec0c0364f7599e3877f97d964a05"
    exact_text = (
        f"I accept {document_ref} frozen at SHA-256 {frozen_hash} and authorize "
        "implementation of that amendment."
    )
    requests = []

    def capture(path, payload, **_kwargs):
        requests.append((path, payload))
        return {"ok": True, "ref": f"dec_{'3' * 26}"}

    monkeypatch.setattr(plugin_module, "_docket_internal_request", capture)

    signoff_result = plugin_module._record_final_signoff_if_explicit(
        SimpleNamespace(text=exact_text),
        f"utt_{'2' * 26}",
    )

    assert signoff_result == {"ok": True, "ref": f"dec_{'3' * 26}"}
    assert requests[0][0] == "/internal/v1/discord/specification-signoffs"
    assert requests[0][1]["document_ref"] == document_ref
    assert requests[0][1]["frozen_artifact_hash"] == frozen_hash
    assert (
        plugin_module._record_final_signoff_if_explicit(
            SimpleNamespace(text=f"{exact_text} please"),
            f"utt_{'4' * 26}",
        )
        is None
    )


@pytest.mark.adversarial
def test_production_reset_authority_forwards_every_exact_binding(
    plugin_module, monkeypatch
) -> None:
    document_ref = "ONT-DELTA-2026-08-29-TRACKED-CONTEXT"
    frozen_hash = "830c33c9d78485a6a6a8f872b6dfad996869f8a7eaea9a5f7d39d52e9357cf48"
    manifest_hash = "a" * 64
    backup_ref = "tracked-context-pre-reset-20260830.dump"
    backup_hash = "b" * 64
    revision = "c" * 40
    exact_text = (
        f"I authorize execution of the production reset for `{document_ref}` frozen at "
        f"SHA-256 `{frozen_hash}`, bound to reset manifest SHA-256 `{manifest_hash}`, "
        f"verified backup artifact `{backup_ref}` at SHA-256 `{backup_hash}`, and "
        f"deployment revision `{revision}`."
    )
    requests = []

    def capture(path, payload, **_kwargs):
        requests.append((path, payload))
        return {"ok": True, "ref": f"dec_{'3' * 26}"}

    monkeypatch.setattr(plugin_module, "_docket_internal_request", capture)

    result = plugin_module._record_production_reset_authorization_if_explicit(
        SimpleNamespace(text=exact_text),
        f"utt_{'2' * 26}",
    )

    assert result == {"ok": True, "ref": f"dec_{'3' * 26}"}
    assert requests == [
        (
            "/internal/v1/discord/production-reset-authorizations",
            {
                "request_id": requests[0][1]["request_id"],
                "utterance_ref": f"utt_{'2' * 26}",
                "document_ref": document_ref,
                "frozen_artifact_hash": frozen_hash,
                "reset_manifest_sha256": manifest_hash,
                "verified_backup_ref": backup_ref,
                "verified_backup_sha256": backup_hash,
                "deployment_revision": revision,
            },
        )
    ]
    assert (
        plugin_module._record_production_reset_authorization_if_explicit(
            SimpleNamespace(text=f"{exact_text} now"),
            f"utt_{'4' * 26}",
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.adversarial
async def test_agent_response_persistence_failure_blocks_discord_delivery(
    plugin_module, monkeypatch
) -> None:
    class FakeSendResult:
        def __init__(self, success, **kwargs) -> None:
            self.success = success
            self.error = kwargs.get("error")
            self.retryable = kwargs.get("retryable", False)

    gateway_module = ModuleType("gateway")
    platforms_module = ModuleType("gateway.platforms")
    base_module = ModuleType("gateway.platforms.base")
    base_module.SendResult = FakeSendResult
    monkeypatch.setitem(sys.modules, "gateway", gateway_module)
    monkeypatch.setitem(sys.modules, "gateway.platforms", platforms_module)
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", base_module)

    delivered = []

    class FakeAdapter:
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            delivered.append((chat_id, content, reply_to, metadata))
            return FakeSendResult(True)

    adapter = FakeAdapter()
    adapter._docket_provenance_contexts = {
        ("222222222222222222", "333333333333333333", "444444444444444444"): {
            "terminal": True,
            "response_persistence_failed": True,
        }
    }
    plugin_module._install_provenance_delivery_guard(adapter)

    blocked = await adapter.send(
        chat_id="333333333333333333",
        content="unlogged response",
        reply_to="444444444444444444",
    )
    assert blocked.success is False
    assert blocked.error == "docket_agent_response_not_persisted"
    assert delivered == []

    adapter._docket_provenance_contexts.clear()
    allowed = await adapter.send(
        chat_id="333333333333333333",
        content="logged response",
        reply_to="444444444444444444",
    )
    assert allowed.success is True
    assert delivered == [
        (
            "333333333333333333",
            "logged response",
            "444444444444444444",
            None,
        )
    ]


@pytest.mark.asyncio
@pytest.mark.adversarial
async def test_generic_home_channel_prompt_is_suppressed_only_during_docket_turn(
    plugin_module, monkeypatch
) -> None:
    class FakeSendResult:
        def __init__(self, success, **kwargs) -> None:
            self.success = success
            self.error = kwargs.get("error")

    gateway_module = ModuleType("gateway")
    platforms_module = ModuleType("gateway.platforms")
    base_module = ModuleType("gateway.platforms.base")
    base_module.SendResult = FakeSendResult
    monkeypatch.setitem(sys.modules, "gateway", gateway_module)
    monkeypatch.setitem(sys.modules, "gateway.platforms", platforms_module)
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", base_module)

    delivered = []

    class FakeAdapter:
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            delivered.append((chat_id, content, reply_to, metadata))
            return FakeSendResult(True)

    adapter = FakeAdapter()
    adapter._docket_provenance_contexts = {
        ("222222222222222222", "333333333333333333", "444444444444444444"): {
            "terminal": False,
        }
    }
    plugin_module._install_provenance_delivery_guard(adapter)
    prompt = (
        "📬 No home channel is set for Discord. A home channel is where Hermes "
        "delivers cron job results and cross-platform messages."
    )

    suppressed = await adapter.send("333333333333333333", prompt)
    assert suppressed.success is True
    assert delivered == []

    ordinary = await adapter.send("333333333333333333", "ordinary response")
    assert ordinary.success is True
    assert delivered == [("333333333333333333", "ordinary response", None, None)]

    outside = await adapter.send("555555555555555555", prompt)
    assert outside.success is True
    assert delivered[-1] == ("555555555555555555", prompt, None, None)

    adapter._docket_provenance_contexts[
        ("222222222222222222", "333333333333333333", "444444444444444444")
    ]["terminal"] = True
    after_turn = await adapter.send("333333333333333333", prompt)
    assert after_turn.success is True
    assert delivered[-1] == ("333333333333333333", prompt, None, None)


def test_authorized_chat_receives_bounded_operator_preferences(
    plugin_module, monkeypatch, tmp_path
) -> None:
    actor = "111111111111111111"
    guild = "222222222222222222"
    channel = "333333333333333333"
    message = "444444444444444444"
    preferences = tmp_path / "preferences"
    preferences.mkdir()
    (preferences / "AGENT.md").write_text("# Agent\n- Be concise.\n", encoding="utf-8")
    (preferences / "TRIAGE.md").write_text(
        "# Triage\n- Do not propose football games.\n", encoding="utf-8"
    )
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", actor)
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", channel)
    monkeypatch.setenv("DOCKET_PREFERENCES_DIR", str(preferences))
    event = SimpleNamespace(
        text="I don't want football games this semester.",
        message_id=message,
        source=SimpleNamespace(
            platform="discord",
            user_id=actor,
            guild_id=guild,
            chat_id=channel,
        ),
    )

    result = plugin_module._pre_gateway_dispatch(event)

    assert result is not None and result["action"] == "rewrite"
    assert '<docket_operator_preferences trusted="true">' in result["text"]
    assert "Do not propose football games" in result["text"]
    assert "/opt/data/preferences/TRIAGE.md" in result["text"]
    assert "do not rewrite" in result["text"]
    assert "structured Docket Preference" in result["text"]
    assert result["text"].index("docket_operator_preferences") < result["text"].index(
        "docket_gateway_context"
    )


def test_real_gateway_enum_and_source_message_id_are_normalized(plugin_module, monkeypatch) -> None:
    actor = "111111111111111111"
    guild = "222222222222222222"
    channel = "333333333333333333"
    message = "444444444444444444"
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", actor)
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", channel)
    event = SimpleNamespace(
        text="Remember my Fall 2026 term",
        message_id=None,
        source=SimpleNamespace(
            platform=Platform.DISCORD,
            user_id=actor,
            guild_id=guild,
            chat_id=channel,
            message_id=message,
        ),
    )

    result = plugin_module._pre_gateway_dispatch(event)

    assert result is not None and result["action"] == "rewrite"
    assert f'"request_key": "discord:{guild}:{channel}:{message}:0"' in result["text"]


def test_chat_context_is_not_added_for_untrusted_actor(plugin_module, monkeypatch) -> None:
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", "111111111111111111")
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", "222222222222222222")
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", "333333333333333333")
    event = SimpleNamespace(
        text="Store attacker data",
        message_id="444444444444444444",
        source=SimpleNamespace(
            platform="discord",
            user_id="999999999999999999",
            guild_id="222222222222222222",
            chat_id="333333333333333333",
        ),
    )

    assert plugin_module._pre_gateway_dispatch(event) is None


def test_session_commands_are_not_rewritten(plugin_module, monkeypatch) -> None:
    actor = "111111111111111111"
    guild = "222222222222222222"
    channel = "333333333333333333"
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", actor)
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", channel)
    event = SimpleNamespace(
        text="/reset",
        message_id="444444444444444444",
        source=SimpleNamespace(
            platform="discord",
            user_id=actor,
            guild_id=guild,
            chat_id=channel,
        ),
    )

    assert plugin_module._pre_gateway_dispatch(event) is None


@pytest.mark.adversarial
def test_outbound_listener_requires_independent_exact_bearer(plugin_module, monkeypatch) -> None:
    monkeypatch.setattr(plugin_module, "_read_outbound_token", lambda: "expected-token")
    authorized = SimpleNamespace(headers={"Authorization": "Bearer expected-token"})
    wrong = SimpleNamespace(headers={"Authorization": "Bearer expected-token-extra"})
    missing = SimpleNamespace(headers={})

    assert plugin_module._PluginRequestHandler._authorized(authorized) is True
    assert plugin_module._PluginRequestHandler._authorized(wrong) is False
    assert plugin_module._PluginRequestHandler._authorized(missing) is False


@pytest.mark.adversarial
def test_outbound_target_cannot_escape_configured_queue(plugin_module, monkeypatch) -> None:
    guild = "111111111111111111"
    queue = "222222222222222222"
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_QUEUE_CHANNEL_ID", queue)

    assert plugin_module._validate_target(guild, queue) == (guild, queue)
    with pytest.raises(plugin_module.PluginAPIError) as rejected:
        plugin_module._validate_target(guild, "333333333333333333")
    assert rejected.value.code == "discord_target_not_allowed"


@pytest.mark.asyncio
async def test_thread_ensure_joins_only_configured_operator_idempotently(
    plugin_module, monkeypatch
) -> None:
    actor = "111111111111111111"
    guild = "222222222222222222"
    queue_id = "333333333333333333"
    bot_id = 444444444444444444
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", actor)
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_QUEUE_CHANNEL_ID", queue_id)

    class FakeHTTPException(Exception):
        pass

    class FakeObject:
        def __init__(self, *, id: int) -> None:
            self.id = id

    class FakeThread:
        id = 555555555555555555
        name = "2026-07-24 — Friday"
        parent_id = int(queue_id)
        owner_id = bot_id
        archived = False
        auto_archive_duration = 10080

        def __init__(self) -> None:
            self.add_attempts = 0
            self.members: set[int] = set()

        async def add_user(self, user) -> None:
            self.add_attempts += 1
            self.members.add(user.id)

    thread = FakeThread()

    class FakeQueue:
        id = int(queue_id)

        def __init__(self) -> None:
            self.threads = [thread]

        async def archived_threads(self, **_kwargs):
            if False:
                yield None

    async def fake_fetch_queue(_client, requested_guild: str, requested_queue: str):
        assert requested_guild == guild
        assert requested_queue == queue_id
        return FakeQueue()

    fake_discord = SimpleNamespace(
        HTTPException=FakeHTTPException,
        Object=FakeObject,
    )
    monkeypatch.setitem(sys.modules, "discord", fake_discord)
    monkeypatch.setattr(plugin_module, "_fetch_queue", fake_fetch_queue)
    monkeypatch.setattr(
        plugin_module,
        "_discord_runtime",
        lambda: (None, None, SimpleNamespace(user=SimpleNamespace(id=bot_id))),
    )
    payload = {
        "request_id": str(uuid.uuid4()),
        "daily_thread_id": str(uuid.uuid4()),
        "known_thread_id": None,
        "guild_id": guild,
        "channel_id": queue_id,
        "operator_user_id": actor,
        "local_date": "2026-07-24",
        "name": "2026-07-24 — Friday",
        "thread_type": "public_thread",
        "auto_archive_minutes": 10080,
    }

    first = await plugin_module._ensure_thread(payload)
    second = await plugin_module._ensure_thread({**payload, "request_id": str(uuid.uuid4())})

    assert first["operator_user_id"] == actor
    assert first["operator_joined"] is True
    assert second["operator_joined"] is True
    assert thread.add_attempts == 2
    assert thread.members == {int(actor)}

    with pytest.raises(plugin_module.PluginAPIError) as rejected:
        plugin_module._validate_operator_target("666666666666666666")
    assert rejected.value.code == "discord_operator_not_allowed"


@pytest.mark.adversarial
def test_plugin_decodes_only_projection_bound_v2_control(plugin_module) -> None:
    approval_id = uuid.uuid4()
    projection_id = uuid.uuid4()
    token = issue_projection_approval_token(
        approval_id,
        projection_id,
        datetime.now(UTC) + timedelta(minutes=15),
        b"test-signing-key",
    )

    assert plugin_module._decode_control(token) == (approval_id, projection_id)
    with pytest.raises(plugin_module.PluginAPIError):
        plugin_module._decode_control("not-a-projection-token")


def test_plugin_decodes_only_projection_bound_local_control(plugin_module) -> None:
    revision_id = uuid.uuid4()
    projection_id = uuid.uuid4()
    token = issue_projection_local_action_token(
        revision_id,
        projection_id,
        1,
        datetime.now(UTC) + timedelta(days=1),
        b"test-signing-key",
    )

    assert plugin_module._decode_local_control(token) == (revision_id, projection_id)
    with pytest.raises(plugin_module.PluginAPIError):
        plugin_module._decode_local_control("not-a-local-token")


@pytest.mark.adversarial
def test_system_alert_target_is_separately_allowlisted(plugin_module, monkeypatch) -> None:
    guild = "111111111111111111"
    system = "222222222222222222"
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_SYSTEM_CHANNEL_ID", system)

    assert plugin_module._validate_system_target(guild, system) == (guild, system)
    with pytest.raises(plugin_module.PluginAPIError) as rejected:
        plugin_module._validate_system_target(guild, "333333333333333333")
    assert rejected.value.code == "discord_target_not_allowed"


def test_reminder_fields_localize_instants_but_preserve_all_day_dates(
    plugin_module,
) -> None:
    timed = plugin_module._calendar_reminder_fields(
        {
            "summary": "Timed event",
            "start": "<t:1785439800:F> · <t:1785439800:R>",
            "end": "<t:1785440700:F>",
            "timezone": "America/Los_Angeles",
            "is_all_day": False,
        }
    )
    assert timed == [
        ("Title", "Timed event", False),
        ("Starts", "<t:1785439800:F> · <t:1785439800:R>", False),
        ("Ends", "<t:1785440700:F>", False),
    ]

    all_day = plugin_module._calendar_reminder_fields(
        {
            "summary": "All-day event",
            "start": "2026-07-30",
            "end": "2026-07-31",
            "timezone": "America/Los_Angeles",
            "is_all_day": True,
        }
    )
    assert all_day == [
        ("Title", "All-day event", False),
        ("Start date", "2026-07-30", True),
        ("End date (exclusive)", "2026-07-31", True),
        ("Calendar timezone", "America/Los_Angeles", False),
    ]


def test_plugin_rejects_aliased_channel_lanes(plugin_module, monkeypatch) -> None:
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", "222222222222222222")
    monkeypatch.setenv("DOCKET_QUEUE_CHANNEL_ID", "222222222222222222")
    monkeypatch.setenv("DOCKET_SYSTEM_CHANNEL_ID", "333333333333333333")

    with pytest.raises(RuntimeError, match="must be distinct"):
        plugin_module._validate_channel_lanes()


def test_plugin_can_render_one_canonical_local_control(plugin_module, monkeypatch) -> None:
    class FakeEmbed:
        def __init__(self, **kwargs) -> None:
            self.footer = None
            self.description = kwargs.get("description")

        def add_field(self, **_kwargs) -> None:
            return None

        def set_footer(self, **kwargs) -> None:
            self.footer = kwargs["text"]

    class FakeView:
        def __init__(self, **_kwargs) -> None:
            self.items = []

        def add_item(self, item) -> None:
            self.items.append(item)

    class FakeButton:
        def __init__(self, **kwargs) -> None:
            self.custom_id = kwargs["custom_id"]

    fake_discord = SimpleNamespace(
        Embed=FakeEmbed,
        ButtonStyle=SimpleNamespace(success=1, danger=2, secondary=3, primary=4),
        ui=SimpleNamespace(View=FakeView, Button=FakeButton),
        utils=SimpleNamespace(
            escape_mentions=lambda value: value,
            escape_markdown=lambda value: value,
        ),
    )
    monkeypatch.setitem(sys.modules, "discord", fake_discord)
    action_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    projection_id = uuid.uuid4()
    token = issue_projection_local_action_token(
        revision_id,
        projection_id,
        2,
        datetime.now(UTC) + timedelta(days=1),
        b"test-signing-key",
    )
    _embed, view = plugin_module._render_embed(
        projection_id,
        {
            "embed": {
                "title": "Failed item",
                "description": "Only Ignore is a valid local transition.",
                "fields": [],
                "color": 1,
            },
            "controls": [
                {
                    "kind": "local_action",
                    "action_type": "ignore_queue_item",
                    "label": "Ignore",
                    "action_id": str(action_id),
                    "action_revision_id": str(revision_id),
                    "token": token,
                }
            ],
            "projection_version": 1,
            "render_sha256": "a" * 64,
            "component_sha256": "b" * 64,
        },
    )

    assert len(view.items) == 1
    assert view.items[0].custom_id == f"dkt:l:{token}"
    assert "ref " in _embed.footer
    assert "render:" not in _embed.footer
    assert "components:" not in _embed.footer

    _snooze_embed, snooze_view = plugin_module._render_embed(
        projection_id,
        {
            "embed": {
                "title": "Decision required",
                "description": "Reply with context and a decision.",
                "fields": [],
                "color": 1,
            },
            "controls": [
                {
                    "kind": "local_action",
                    "action_type": "snooze_queue_item",
                    "label": "Snooze until tomorrow",
                    "action_id": str(action_id),
                    "action_revision_id": str(revision_id),
                    "token": token,
                }
            ],
            "projection_version": 1,
            "render_sha256": "e" * 64,
            "component_sha256": "f" * 64,
        },
    )
    assert len(snooze_view.items) == 1
    assert snooze_view.items[0].custom_id == f"dkt:l:{token}"

    terminal_embed, terminal_view = plugin_module._render_embed(
        projection_id,
        {
            "embed": {
                "title": "Event updated",
                "description": None,
                "fields": [
                    {
                        "name": "Title",
                        "value": "Check my email",
                        "inline": False,
                    }
                ],
                "color": 1,
            },
            "controls": [],
            "projection_version": 2,
            "render_sha256": "c" * 64,
            "component_sha256": "d" * 64,
        },
    )
    assert terminal_embed.description is None
    assert terminal_view is None


def test_proposal_snooze_joins_decisions_as_a_primary_button(plugin_module, monkeypatch) -> None:
    class FakeEmbed:
        def __init__(self, **_kwargs) -> None:
            self.footer = None

        def add_field(self, **_kwargs) -> None:
            return None

        def set_footer(self, **kwargs) -> None:
            self.footer = kwargs["text"]

    class FakeView:
        def __init__(self, **_kwargs) -> None:
            self.items = []

        def add_item(self, item) -> None:
            self.items.append(item)

    class FakeButton:
        def __init__(self, **kwargs) -> None:
            self.label = kwargs["label"]
            self.style = kwargs["style"]
            self.custom_id = kwargs["custom_id"]
            self.row = kwargs["row"]

    button_styles = SimpleNamespace(success=1, danger=2, secondary=3, primary=4)
    fake_discord = SimpleNamespace(
        Embed=FakeEmbed,
        ButtonStyle=button_styles,
        ui=SimpleNamespace(View=FakeView, Button=FakeButton),
        utils=SimpleNamespace(
            escape_mentions=lambda value: value,
            escape_markdown=lambda value: value,
        ),
    )
    monkeypatch.setitem(sys.modules, "discord", fake_discord)
    approval_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    projection_id = uuid.uuid4()
    expires_at = datetime.now(UTC) + timedelta(days=1)
    approval_token = issue_projection_approval_token(
        approval_id,
        projection_id,
        expires_at,
        b"test-signing-key",
    )
    edit_token = issue_projection_proposal_control_token(
        revision_id,
        projection_id,
        "edit",
        expires_at,
        b"test-signing-key",
    )
    snooze_token = issue_projection_proposal_control_token(
        revision_id,
        projection_id,
        "snooze",
        expires_at,
        b"test-signing-key",
    )

    _embed, view = plugin_module._render_embed(
        projection_id,
        {
            "embed": {
                "title": "Review new event",
                "description": None,
                "fields": [],
                "color": 1,
            },
            "controls": [
                {
                    "kind": "approval",
                    "decision": "approve",
                    "label": "Approve",
                    "approval_id": str(approval_id),
                    "token": approval_token,
                },
                {
                    "kind": "approval",
                    "decision": "reject",
                    "label": "Reject",
                    "approval_id": str(approval_id),
                    "token": approval_token,
                },
                {
                    "kind": "proposal_action",
                    "transition": "proposal_edit",
                    "label": "Edit details",
                    "row": 3,
                    "action_revision_id": str(revision_id),
                    "token": edit_token,
                },
                {
                    "kind": "proposal_action",
                    "transition": "proposal_snooze",
                    "label": "Snooze until tomorrow",
                    "row": 0,
                    "action_revision_id": str(revision_id),
                    "token": snooze_token,
                },
            ],
            "projection_version": 1,
            "render_sha256": "a" * 64,
            "component_sha256": "b" * 64,
        },
    )

    assert [(item.label, item.style, item.row) for item in view.items] == [
        ("Approve", button_styles.success, 0),
        ("Reject", button_styles.danger, 0),
        ("Edit details", button_styles.secondary, 3),
        ("Snooze until tomorrow", button_styles.primary, 0),
    ]


def test_plugin_accepts_only_bound_persistent_review_navigation(plugin_module, monkeypatch) -> None:
    class FakeEmbed:
        def __init__(self, **_kwargs) -> None:
            self.footer = None

        def add_field(self, **_kwargs) -> None:
            return None

        def set_footer(self, **kwargs) -> None:
            self.footer = SimpleNamespace(text=kwargs["text"])

    class FakeView:
        def __init__(self, **_kwargs) -> None:
            self.items = []

        def add_item(self, item) -> None:
            self.items.append(item)

    class FakeButton:
        def __init__(self, **kwargs) -> None:
            self.custom_id = kwargs["custom_id"]

    fake_discord = SimpleNamespace(
        Embed=FakeEmbed,
        ButtonStyle=SimpleNamespace(success=1, danger=2, secondary=3, primary=4),
        ui=SimpleNamespace(
            View=FakeView,
            Button=FakeButton,
        ),
        utils=SimpleNamespace(
            escape_mentions=lambda value: value,
            escape_markdown=lambda value: value,
        ),
    )
    monkeypatch.setitem(sys.modules, "discord", fake_discord)
    revision_id = uuid.uuid4()
    projection_id = uuid.uuid4()
    expires_at = datetime.now(UTC) + timedelta(days=1)
    actor = "111111111111111111"
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", actor)
    token = issue_projection_review_navigation_token(
        action_revision_id=revision_id,
        projection_id=projection_id,
        projection_version=3,
        source_view="summary",
        source_page=None,
        target_view="schedule_review",
        target_page=1,
        actor_id=actor,
        expires_at=expires_at,
        signing_key=b"test-signing-key",
    )
    _embed, view = plugin_module._render_embed(
        projection_id,
        {
            "embed": {
                "title": "Apply schedule",
                "description": "Review one immutable aggregate.",
                "fields": [],
                "color": 1,
            },
            "controls": [
                {
                    "kind": "review_navigation",
                    "transition": "proposal_review_navigate",
                    "label": "Begin review",
                    "row": 1,
                    "action_revision_id": str(revision_id),
                    "source_view": "summary",
                    "source_page": None,
                    "target_view": "schedule_review",
                    "target_page": 1,
                    "token": token,
                },
            ],
            "projection_version": 3,
            "render_sha256": "a" * 64,
            "component_sha256": "b" * 64,
        },
    )

    assert {item.custom_id for item in view.items} == {f"dkt:n:{token}"}
    approval_id = uuid.uuid4()
    reject_token = issue_projection_approval_token(
        approval_id,
        projection_id,
        expires_at,
        b"test-signing-key",
    )
    rebuild_token = issue_projection_proposal_control_token(
        revision_id,
        projection_id,
        "refresh",
        expires_at,
        b"test-signing-key",
    )
    _embed, stale_view = plugin_module._render_embed(
        projection_id,
        {
            "embed": {
                "title": "Calendar state changed",
                "description": "Rebuild before approval.",
                "fields": [],
                "color": 1,
            },
            "controls": [
                {
                    "kind": "approval",
                    "decision": "reject",
                    "label": "Reject",
                    "approval_id": str(approval_id),
                    "token": reject_token,
                },
                {
                    "kind": "proposal_action",
                    "transition": "proposal_refresh",
                    "label": "Rebuild preview",
                    "row": 3,
                    "action_revision_id": str(revision_id),
                    "token": rebuild_token,
                },
            ],
            "projection_version": 4,
            "render_sha256": "a" * 64,
            "component_sha256": "b" * 64,
        },
    )
    assert {item.custom_id for item in stale_view.items} == {
        f"dkt:r:{reject_token}",
        f"dkt:p:{rebuild_token}",
    }
    brief_approval_token = issue_projection_approval_token(
        approval_id,
        projection_id,
        expires_at,
        b"test-signing-key",
    )
    brief_edit_token = issue_projection_proposal_control_token(
        revision_id,
        projection_id,
        "edit",
        expires_at,
        b"test-signing-key",
    )
    brief_snooze_token = issue_projection_proposal_control_token(
        revision_id,
        projection_id,
        "snooze",
        expires_at,
        b"test-signing-key",
    )
    brief_next_token = issue_projection_review_navigation_token(
        action_revision_id=revision_id,
        projection_id=projection_id,
        projection_version=5,
        source_view="brief_review",
        source_page=1,
        target_view="brief_review",
        target_page=2,
        actor_id=actor,
        expires_at=expires_at,
        signing_key=b"test-signing-key",
    )
    _embed, brief_view = plugin_module._render_embed(
        projection_id,
        {
            "embed": {
                "title": "Review new event",
                "description": None,
                "fields": [],
                "color": 1,
            },
            "controls": [
                {
                    "kind": "approval",
                    "decision": decision,
                    "label": decision.title(),
                    "approval_id": str(approval_id),
                    "token": brief_approval_token,
                }
                for decision in ("approve", "reject")
            ]
            + [
                {
                    "kind": "proposal_action",
                    "transition": "proposal_edit",
                    "label": "Edit details",
                    "row": 3,
                    "action_revision_id": str(revision_id),
                    "token": brief_edit_token,
                },
                {
                    "kind": "proposal_action",
                    "transition": "proposal_snooze",
                    "label": "Snooze until tomorrow",
                    "row": 0,
                    "action_revision_id": str(revision_id),
                    "token": brief_snooze_token,
                },
                {
                    "kind": "review_navigation",
                    "transition": "proposal_review_navigate",
                    "label": "Next",
                    "row": 4,
                    "action_revision_id": str(revision_id),
                    "source_view": "brief_review",
                    "source_page": 1,
                    "target_view": "brief_review",
                    "target_page": 2,
                    "token": brief_next_token,
                },
            ],
            "projection_version": 5,
            "render_sha256": "a" * 64,
            "component_sha256": "b" * 64,
        },
    )
    assert {item.custom_id for item in brief_view.items} == {
        f"dkt:a:{brief_approval_token}",
        f"dkt:r:{brief_approval_token}",
        f"dkt:p:{brief_edit_token}",
        f"dkt:p:{brief_snooze_token}",
        f"dkt:n:{brief_next_token}",
    }
    edit_token = issue_projection_proposal_control_token(
        revision_id,
        projection_id,
        "edit",
        expires_at,
        b"test-signing-key",
    )
    with pytest.raises(plugin_module.PluginAPIError, match="Approval pair"):
        plugin_module._render_embed(
            projection_id,
            {
                "embed": {
                    "title": "Forged stale controls",
                    "description": None,
                    "fields": [],
                    "color": 1,
                },
                "controls": [
                    {
                        "kind": "approval",
                        "decision": "reject",
                        "label": "Reject",
                        "approval_id": str(approval_id),
                        "token": reject_token,
                    },
                    {
                        "kind": "proposal_action",
                        "transition": "proposal_edit",
                        "label": "Edit details",
                        "row": 3,
                        "action_revision_id": str(revision_id),
                        "token": edit_token,
                    },
                ],
                "projection_version": 4,
                "render_sha256": "a" * 64,
                "component_sha256": "b" * 64,
            },
        )
    with pytest.raises(plugin_module.PluginAPIError, match="binding does not match"):
        plugin_module._render_embed(
            projection_id,
            {
                "embed": {
                    "title": "Apply schedule",
                    "description": "Review one immutable aggregate.",
                    "fields": [],
                    "color": 1,
                },
                "controls": [
                    {
                        "kind": "review_navigation",
                        "transition": "proposal_review_navigate",
                        "label": "Begin review",
                        "row": 1,
                        "action_revision_id": str(revision_id),
                        "source_view": "summary",
                        "source_page": None,
                        "target_view": "schedule_review",
                        "target_page": 2,
                        "token": token,
                    }
                ],
                "projection_version": 3,
                "render_sha256": "a" * 64,
                "component_sha256": "b" * 64,
            },
        )


@pytest.mark.asyncio
async def test_schedule_review_navigation_requests_persistent_message_update(
    plugin_module, monkeypatch
) -> None:
    actor = "111111111111111111"
    guild = "222222222222222222"
    queue = "333333333333333333"
    thread_id = "444444444444444444"
    message_id = "555555555555555555"
    revision_id = uuid.uuid4()
    projection_id = uuid.uuid4()
    token = issue_projection_review_navigation_token(
        action_revision_id=revision_id,
        projection_id=projection_id,
        projection_version=4,
        source_view="summary",
        source_page=None,
        target_view="schedule_review",
        target_page=1,
        actor_id=actor,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        signing_key=b"test-signing-key",
    )
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", actor)
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_QUEUE_CHANNEL_ID", queue)

    class FakeThread:
        parent_id = int(queue)

    class FakeResponse:
        def __init__(self) -> None:
            self.done = False

        async def defer(self, **kwargs) -> None:
            assert kwargs == {}
            self.done = True

        def is_done(self) -> bool:
            return self.done

    class FakeFollowup:
        def __init__(self) -> None:
            self.sent: list[tuple[str, bool]] = []

        async def send(self, content: str, *, ephemeral: bool) -> None:
            self.sent.append((content, ephemeral))

    fake_discord = SimpleNamespace(Thread=FakeThread)
    monkeypatch.setitem(sys.modules, "discord", fake_discord)
    captured: dict[str, object] = {}

    def fake_post(payload, *, local_action=False):
        captured.update(payload)
        assert local_action is True
        return {"ok": True}

    async def fake_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(plugin_module, "_post_button_response", fake_post)
    monkeypatch.setattr(plugin_module.asyncio, "to_thread", fake_to_thread)
    response = FakeResponse()
    followup = FakeFollowup()
    interaction = SimpleNamespace(
        id=666666666666666666,
        data={"custom_id": f"dkt:n:{token}"},
        user=SimpleNamespace(id=int(actor)),
        guild_id=int(guild),
        channel_id=int(thread_id),
        channel=FakeThread(),
        message=SimpleNamespace(id=int(message_id)),
        response=response,
        followup=followup,
    )

    await plugin_module._on_docket_interaction(interaction)

    assert captured["transition"] == "proposal_review_navigate"
    assert captured["source_view"] == "summary"
    assert captured["source_page"] is None
    assert captured["target_view"] == "schedule_review"
    assert captured["target_page"] == 1
    assert captured["action_revision_id"] == str(revision_id)
    assert captured["projection_id"] == str(projection_id)
    assert followup.sent == []


@pytest.mark.asyncio
async def test_accepted_duplicate_approval_disables_controls_and_requests_card_repair(
    plugin_module, monkeypatch
) -> None:
    actor = "111111111111111111"
    guild = "222222222222222222"
    queue = "333333333333333333"
    thread_id = "444444444444444444"
    message_id = "555555555555555555"
    approval_id = uuid.uuid4()
    projection_id = uuid.uuid4()
    token = issue_projection_approval_token(
        approval_id,
        projection_id,
        datetime.now(UTC) + timedelta(days=1),
        b"test-signing-key",
    )
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", actor)
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_QUEUE_CHANNEL_ID", queue)

    class FakeThread:
        parent_id = int(queue)

    class FakeHTTPException(Exception):
        pass

    class FakeResponse:
        def __init__(self) -> None:
            self.done = False

        async def defer(self, **kwargs) -> None:
            assert kwargs == {"ephemeral": True, "thinking": True}
            self.done = True

        def is_done(self) -> bool:
            return self.done

    class FakeFollowup:
        def __init__(self) -> None:
            self.sent: list[tuple[str, bool]] = []

        async def send(self, content: str, *, ephemeral: bool) -> None:
            self.sent.append((content, ephemeral))

    class FakeMessage:
        id = int(message_id)

        def __init__(self) -> None:
            self.edits: list[object] = []

        async def edit(self, *, view) -> None:
            self.edits.append(view)

    fake_discord = SimpleNamespace(Thread=FakeThread, HTTPException=FakeHTTPException)
    monkeypatch.setitem(sys.modules, "discord", fake_discord)
    captured: dict[str, object] = {}

    def fake_post(payload, *, local_action=False):
        captured.update(payload)
        assert local_action is False
        return {
            "ok": True,
            "decision": "approve",
            "approval_status": "consumed",
            "already_recorded": True,
            "operation_id": str(uuid.uuid4()),
        }

    async def fake_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(plugin_module, "_post_button_response", fake_post)
    monkeypatch.setattr(plugin_module.asyncio, "to_thread", fake_to_thread)
    response = FakeResponse()
    followup = FakeFollowup()
    message = FakeMessage()
    interaction = SimpleNamespace(
        id=666666666666666666,
        data={"custom_id": f"dkt:a:{token}"},
        user=SimpleNamespace(id=int(actor)),
        guild_id=int(guild),
        channel_id=int(thread_id),
        channel=FakeThread(),
        message=message,
        response=response,
        followup=followup,
    )

    await plugin_module._on_docket_interaction(interaction)

    assert captured["approval_id"] == str(approval_id)
    assert captured["projection_id"] == str(projection_id)
    assert captured["decision"] == "approve"
    assert message.edits == [None]
    assert followup.sent == [("Already approved — refreshing this card", True)]
