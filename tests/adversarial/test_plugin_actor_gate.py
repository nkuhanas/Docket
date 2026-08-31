import base64
import hashlib
import importlib.util
import sys
import uuid
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

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


def test_operator_capture_uses_raw_discord_content_for_stable_ingress_replay(
    monkeypatch,
) -> None:
    spec = importlib.util.spec_from_file_location("docket_discord_capture_test", PLUGIN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    actor = "111111111111111111"
    guild = "222222222222222222"
    channel = "333333333333333333"
    message = "444444444444444444"
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", actor)
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", channel)
    captured: dict[str, object] = {}

    def fake_request(path, payload, **_kwargs):
        captured["path"] = path
        captured["payload"] = payload
        return {"ok": True, "ref": f"utt_{'0' * 26}", "state": "recorded"}

    monkeypatch.setattr(module, "_docket_internal_request", fake_request)
    raw_text = f"<@{guild}> create the attached class calendars"
    event = SimpleNamespace(
        text="@Yuuka create the attached class calendars",
        message_id=message,
        timestamp=datetime.now(UTC),
        raw_message=SimpleNamespace(content=raw_text, attachments=[]),
        media_urls=[],
        source=SimpleNamespace(
            platform="discord",
            user_id=actor,
            guild_id=guild,
            chat_id=channel,
        ),
    )

    utterance_ref, _reply_binding, _ingress = module._capture_operator_utterance(event)

    assert utterance_ref == f"utt_{'0' * 26}"
    assert captured["path"] == "/internal/v1/discord/operator-utterances"
    assert captured["payload"]["verbatim_text"] == raw_text


@pytest.mark.parametrize(
    ("profile_name", "argv", "expected"),
    [
        ("default", ["hermes", "gateway", "run", "--replace"], True),
        ("default", ["hermes", "gateway", "run"], False),
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
        plugin_module._owns_discord_gateway_lifetime(SimpleNamespace(profile_name=profile_name))
        is expected
    )


@pytest.mark.parametrize("prefix", ["a", "l", "n", "p", "r"])
def test_legacy_component_namespaces_are_not_routable(plugin_module, prefix: str) -> None:
    assert plugin_module._CONTROL_ID.fullmatch(f"dkt:{prefix}:{'A' * 48}") is None


def test_semantic_option_component_namespace_is_routable(plugin_module) -> None:
    assert plugin_module._CONTROL_ID.fullmatch(f"dkt:s:{'A' * 48}") is not None


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
        tool_name="mcp__docket__docket_search_entities",
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
        tool_name="mcp__docket__docket_search_entities",
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
        "tool_name": "docket_search_entities",
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


def test_operator_attachment_manifest_preserves_exact_cached_bytes(
    plugin_module, monkeypatch, tmp_path
) -> None:
    plaintext = b"untrusted schedule image bytes"
    cached = tmp_path / "schedule.png"
    cached.write_bytes(plaintext)
    monkeypatch.setenv("DOCKET_ATTACHMENT_MAX_BYTES", "1024")
    monkeypatch.setenv("DOCKET_ATTACHMENT_TOTAL_MAX_BYTES", "2048")
    attachment = SimpleNamespace(
        id="444444444444444445",
        filename="schedule.png",
        content_type="image/png",
        size=len(plaintext),
    )
    event = SimpleNamespace(
        raw_message=SimpleNamespace(attachments=[attachment]),
        media_urls=[str(cached)],
        timestamp=datetime.now(UTC),
    )

    manifests = plugin_module._attachment_manifests(event)

    assert manifests == [
        {
            "transport_attachment_ref": attachment.id,
            "filename": attachment.filename,
            "media_type": attachment.content_type,
            "byte_size": len(plaintext),
            "received_at": event.timestamp.isoformat(),
            "plaintext_base64": base64.b64encode(plaintext).decode("ascii"),
        }
    ]


def test_attachment_binding_is_trusted_but_content_is_explicitly_untrusted(
    plugin_module, monkeypatch
) -> None:
    actor = "111111111111111111"
    guild = "222222222222222222"
    channel = "333333333333333333"
    message = "444444444444444444"
    source_ref = f"src_{'5' * 26}"
    content_hash = hashlib.sha256(b"schedule").hexdigest()
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", actor)
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", channel)
    monkeypatch.setattr(
        plugin_module,
        "_capture_operator_utterance",
        lambda _event: (
            f"utt_{'0' * 26}",
            None,
            {
                "state": "claimed",
                "attachments": [
                    {
                        "ref": source_ref,
                        "ingest_state": "available",
                        "retention_disposition": "retained_encrypted",
                        "content_hash": content_hash,
                        "source_revision": 1,
                        "untrusted_content": True,
                    }
                ],
            },
        ),
    )
    event = SimpleNamespace(
        text="Import the attached schedule as tracked context.",
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
    assert source_ref in result["text"]
    assert content_hash in result["text"]
    assert '"source_revision": 1' in result["text"]
    assert '"attachment_content_trust": "untrusted_evidence"' in result["text"]
    assert "plaintext_base64" not in result["text"]


def test_pending_attachment_never_reaches_model(plugin_module, monkeypatch) -> None:
    actor = "111111111111111111"
    guild = "222222222222222222"
    channel = "333333333333333333"
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", actor)
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", channel)
    monkeypatch.setattr(
        plugin_module,
        "_capture_operator_utterance",
        lambda _event: (
            f"utt_{'0' * 26}",
            None,
            {"state": "pending", "attachment_state": "pending"},
        ),
    )
    event = SimpleNamespace(
        text="Import the attachment.",
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
        "reason": "docket-attachment-evidence-pending",
    }


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
