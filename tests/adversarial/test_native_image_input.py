import asyncio
import base64
import hashlib
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest
from test_plugin_actor_gate import plugin_module as base_plugin_fixture
from test_plugin_actor_gate import resume_trace_fixture


@pytest.fixture
def plugin_module(monkeypatch):
    return base_plugin_fixture.__wrapped__(monkeypatch)


def _binding(raw=b"image one", index=1):
    return {"source_ref": "src_" + str(index) * 26,
            "content_hash": hashlib.sha256(raw).hexdigest()}


def _parts(*images):
    return [{"type": "text", "text": "private image instructions"}, *[
        {"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(raw).decode(),
        }} for raw in images
    ]]


def _context(plugin, monkeypatch, images=None):
    images = images if images is not None else [_binding()]
    context = {
        "native_image_bindings": images, "native_image_state": "pending",
        "trace_ref": "trace_" + "0" * 26, "utterance_ref": "utt_" + "0" * 26,
        "terminal": False, "turn_id": None, "next_ordinal": 1, "calls": {},
        "guild_id": "111111111111111111", "actor_id": "222222222222222222",
        "source_channel_id": "333333333333333333", "source_message_id": "444444444444444444",
    }
    plugin._TRACE_CONTEXTS["image-turn"] = context
    persisted = []
    scheduled = []
    monkeypatch.setattr(plugin, "_persist_deterministic_response", lambda ctx: (
        persisted.append(ctx["deterministic_response_text"]) or "rsp_" + "1" * 26
    ))
    def schedule(ctx):
        scheduled.append(ctx["response_ref"])
        ctx["deterministic_delivery_scheduled"] = True

    monkeypatch.setattr(plugin, "_schedule_persisted_deterministic_response", schedule)
    monkeypatch.setattr(plugin, "_enqueue_trace_update", lambda *_args, **_kwargs: None)
    return context, persisted, scheduled


def test_native_input_matches_each_ledger_source_without_retaining_bytes(
    plugin_module, monkeypatch,
):
    context, persisted, scheduled = _context(plugin_module, monkeypatch, [
        _binding(), _binding(b"image two", 2),
    ])
    parts = _parts(b"image one", b"image two")
    before = json.dumps(parts)
    plugin_module._verify_native_image_input("image-turn", "", parts)
    assert json.dumps(parts) == before
    assert context["native_image_state"] == "prepared"
    assert persisted == scheduled == []
    assert "image one" not in str(context) and "base64" not in str(context)
    assert "private image instructions" not in str(context)


@pytest.mark.parametrize("problem", [
    "missing", "summary_only", "different_image", "missing_second", "extra", "reordered",
    "remote_url", "bad_base64", "wrong_part", "oversize", "total_size",
])
def test_native_input_loss_never_starts_text_only_interpretation(
    plugin_module, monkeypatch, problem,
):
    context, persisted, scheduled = _context(plugin_module, monkeypatch, [
        _binding(), _binding(b"image two", 2),
    ])
    parts = _parts(b"image one", b"image two")
    if problem == "missing":
        parts = []
    elif problem == "summary_only":
        parts = "An auxiliary model says this is a career fair."
    elif problem == "different_image":
        parts = _parts(b"changed", b"image two")
    elif problem == "missing_second":
        parts.pop()
    elif problem == "extra":
        parts = _parts(b"image one", b"image two", b"unrelated")
    elif problem == "reordered":
        parts = _parts(b"image two", b"image one")
    elif problem == "remote_url":
        parts[1]["image_url"]["url"] = "https://example.invalid/private-image"
    elif problem == "bad_base64":
        parts[1]["image_url"]["url"] = "data:image/png;base64,not-base64"
    elif problem == "wrong_part":
        parts[1]["type"] = "text"
    elif problem == "oversize":
        monkeypatch.setenv("DOCKET_ATTACHMENT_MAX_BYTES", "4")
    elif problem == "total_size":
        monkeypatch.setenv("DOCKET_ATTACHMENT_TOTAL_MAX_BYTES", "12")
    with pytest.raises(RuntimeError, match=r"^docket_native_image_input_unavailable$"):
        plugin_module._verify_native_image_input("image-turn", "", parts)
    assert context["native_image_state"] == "failed"
    assert len(persisted) == len(scheduled) == 1
    assert "No renewed authorization" in persisted[0]
    assert "base64" not in str(context) and "example.invalid" not in str(context)
    assert context["response_persistence_failed"] is False
    # Neither a transport retry nor a generic upstream error creates a second
    # response or restarts interpretation in this failed execution.
    with pytest.raises(RuntimeError, match="docket_native_image_input_unavailable"):
        plugin_module._verify_native_image_input(
            "image-turn", "", _parts(b"image one", b"image two"),
        )
    plugin_module._on_post_llm_call(task_id="image-turn", assistant_response="Runtime error")
    assert len(persisted) == len(scheduled) == 1
    assert context["terminal"] is True and context["turn_finalized"] is True


def test_input_failure_does_not_execute_when_error_response_capture_fails(
    plugin_module, monkeypatch,
):
    context, _persisted, scheduled = _context(plugin_module, monkeypatch)

    def unavailable(_context):
        raise OSError("private transport diagnostic")

    monkeypatch.setattr(plugin_module, "_persist_deterministic_response", unavailable)
    with pytest.raises(RuntimeError, match="docket_native_image_input_unavailable"):
        plugin_module._verify_native_image_input("image-turn", "", [])
    assert context["native_image_state"] == "failed"
    assert context["response_persistence_failed"] is True
    assert scheduled == []


def test_failed_input_capture_can_recover_at_final_callback_without_model_response(
    plugin_module, monkeypatch,
):
    context, persisted, scheduled = _context(plugin_module, monkeypatch)
    original = plugin_module._persist_deterministic_response
    monkeypatch.setattr(plugin_module, "_persist_deterministic_response",
                        lambda _ctx: (_ for _ in ()).throw(OSError("unavailable")))
    with pytest.raises(RuntimeError, match="docket_native_image_input_unavailable"):
        plugin_module._verify_native_image_input("image-turn", "", [])
    monkeypatch.setattr(plugin_module, "_persist_deterministic_response", original)
    plugin_module._on_post_llm_call(task_id="image-turn", assistant_response="generic error")
    assert persisted == [plugin_module._NATIVE_IMAGE_FAILURE]
    assert len(scheduled) == 1 and context["turn_finalized"] is True
    assert context["response_persistence_failed"] is False


def test_new_execution_claim_does_not_rewrite_previous_native_failure(plugin_module, monkeypatch):
    context, persisted, scheduled = _context(plugin_module, monkeypatch)
    context["execution_completion_token"] = "original-claim"
    with pytest.raises(RuntimeError, match="docket_native_image_input_unavailable"):
        plugin_module._verify_native_image_input("image-turn", "", [])
    assert context["native_image_state"] == "failed"
    resumed = resume_trace_fixture(plugin_module, monkeypatch, context, "image-turn")
    # Ingress supplies fresh source bindings; no failed/prepared state is copied.
    resumed.update(
        native_image_bindings=context["native_image_bindings"], native_image_state="pending"
    )
    assert not resumed.get("deterministic_response_text")
    assert not resumed.get("deterministic_delivery_scheduled")
    assert not resumed.get("response_ref")
    plugin_module._verify_native_image_input("image-turn", "", _parts(b"image one"))
    assert resumed["native_image_state"] == "prepared"
    assert context["native_image_state"] == "failed"
    assert len(persisted) == len(scheduled) == 1  # Previous immutable response remains.


def test_delayed_failure_projection_keeps_original_execution_binding(plugin_module, monkeypatch):
    schedule = plugin_module._schedule_persisted_deterministic_response
    context, _persisted, _scheduled = _context(plugin_module, monkeypatch)
    context.update(
        response_ref="rsp_" + "1" * 26, deterministic_response_text="Original durable response",
        execution_completion_token="original-claim",
    )
    queued = []
    delivered = []

    async def deliver(snapshot):
        delivered.append(dict(snapshot))

    monkeypatch.setattr(plugin_module, "_deliver_persisted_deterministic_response", deliver)
    monkeypatch.setattr(plugin_module, "_discord_runtime", lambda: ("loop", None, None))
    monkeypatch.setattr(plugin_module.asyncio, "run_coroutine_threadsafe",
                        lambda coroutine, _loop: queued.append(coroutine))
    schedule(context)
    context.update(response_ref=None, deterministic_response_text=None,
                   execution_completion_token="new-claim")
    asyncio.run(queued[0])
    assert delivered[0]["response_ref"] == "rsp_" + "1" * 26
    assert delivered[0]["deterministic_response_text"] == "Original durable response"
    assert delivered[0]["execution_completion_token"] == "original-claim"


@pytest.mark.parametrize("state", ["pending", "failed"])
@pytest.mark.parametrize("tool", ["stage_changes", "commit_changeset", "resolve_conflict"])
def test_native_input_guard_also_blocks_mutation_dispatch(
    plugin_module, monkeypatch, state, tool,
):
    context, _persisted, _scheduled = _context(plugin_module, monkeypatch)
    context["native_image_state"] = state
    monkeypatch.setattr(plugin_module, "_validate_authority_arguments_locally",
                        lambda *_args: pytest.fail("must not reach schema or admission"))
    result = plugin_module._on_pre_tool_call(
        tool_name=f"mcp__docket__docket_{tool}", args={}, task_id="image-turn",
        turn_id="native-turn", tool_call_id="native-tool",
    )
    assert result["action"] == "block"
    assert result["message"] == plugin_module._NATIVE_IMAGE_FAILURE
    assert context["calls"]["native-tool"]["execution_boundary"] == "local_rejection"


def test_binding_uses_ledger_metadata_and_rejects_invented_sources(plugin_module):
    attachment = {"ref": "src_" + "1" * 26, "content_hash": "a" * 64,
                  "ingest_state": "available", "media_type": "image/png"}
    bindings = plugin_module._native_image_bindings([attachment, {"media_type": "application/pdf"}])
    assert bindings == [
        {"source_ref": attachment["ref"], "content_hash": attachment["content_hash"]},
    ]
    for update in ({"ref": "evt_" + "1" * 26}, {"content_hash": "made up"},
                   {"ingest_state": "pending"}):
        with pytest.raises(RuntimeError):
            plugin_module._native_image_bindings([{**attachment, **update}])
    with pytest.raises(RuntimeError):
        plugin_module._native_image_bindings([attachment, attachment])
    for media_type in (None, "application/octet-stream", ""):
        assert plugin_module._native_image_bindings([
            {**attachment, "media_type": media_type, "filename": "SCHEDULE.PNG"},
        ]) == [{"source_ref": attachment["ref"], "content_hash": attachment["content_hash"]}]
    assert plugin_module._native_image_bindings([
        {**attachment, "media_type": "application/pdf", "filename": "not-an-image.png"},
    ]) == []


def test_native_routing_is_scoped_and_refreshes_without_double_wrapping(plugin_module, monkeypatch):
    calls = []

    class GatewayRunner:
        def _decide_image_input_mode(self, *, source=None, session_key=None, user_config=None,
                                     provider=None, model=None):
            calls.append((source, session_key, user_config, provider, model))
            return "original routing"

    module = ModuleType("gateway.run")
    module.GatewayRunner = GatewayRunner
    monkeypatch.setitem(sys.modules, "gateway.run", module)
    monkeypatch.setattr(plugin_module, "_trusted_ingress_context",
                        lambda source: (1, 2, 3, None) if source == "docket" else None)
    assert plugin_module._install_native_image_routing()
    wrapper = GatewayRunner._decide_image_input_mode
    assert plugin_module._install_native_image_routing()
    assert GatewayRunner._decide_image_input_mode is wrapper
    gateway = GatewayRunner()
    assert gateway._decide_image_input_mode(source="docket") == "native"
    assert calls == []  # No metadata lookup or auxiliary-model pre-analysis.
    assert gateway._decide_image_input_mode(source="unrelated", provider="custom", model="x") == (
        "original routing"
    )
    assert calls == [("unrelated", None, None, "custom", "x")]
    monkeypatch.setattr(plugin_module, "_trusted_ingress_context", lambda _source: None)
    plugin_module._install_native_image_routing()
    assert gateway._decide_image_input_mode(source="docket") == "original routing"


def test_ingress_binds_images_in_ledger_order_before_context_construction(
    plugin_module, monkeypatch,
):
    actor, guild, channel, message = (str(index) * 18 for index in (1, 2, 3, 4))
    monkeypatch.setenv("DOCKET_OPERATOR_DISCORD_USER_ID", actor)
    monkeypatch.setenv("DOCKET_DISCORD_GUILD_ID", guild)
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", channel)
    images = [_binding(), _binding(b"image two", 2)]
    attachments = [{"ref": row["source_ref"], "content_hash": row["content_hash"],
                    "media_type": "image/png", "ingest_state": "available"} for row in images]
    attachments.insert(1, {"media_type": "application/pdf", "ingest_state": "available"})
    monkeypatch.setattr(plugin_module, "_capture_operator_utterance", lambda _event: (
        "utt_" + "1" * 26, None, {
            "state": "claimed", "execution_completion_token": "admitted-image-claim",
            "attachments": attachments,
        },
    ))
    event = SimpleNamespace(
        text="Read the images.", message_id=message,
        source=SimpleNamespace(platform="discord", user_id=actor, guild_id=guild, chat_id=channel),
    )
    store = SimpleNamespace(get_or_create_session=lambda _source: SimpleNamespace(
        session_id="native-input", session_key="native-input",
    ))
    result = plugin_module._pre_gateway_dispatch(event, session_store=store)
    assert result["action"] == "rewrite"
    context = plugin_module._TRACE_CONTEXTS["native-input"]
    assert context["native_image_bindings"] == images
    assert context["native_image_state"] == "pending"
    plugin_module._verify_native_image_input("native-input", "", _parts(b"image one", b"image two"))
    assert context["native_image_state"] == "prepared"


def test_real_context_boundary_checks_images_before_calling_original(plugin_module, monkeypatch):
    context, persisted, _scheduled = _context(plugin_module, monkeypatch)
    calls = []

    def build_turn_context(agent, user_message, system_message, conversation_history, task_id):
        calls.append(user_message)
        return "original result"

    module = ModuleType("agent")
    module.conversation_loop = SimpleNamespace(build_turn_context=build_turn_context)
    monkeypatch.setitem(sys.modules, "agent", module)
    plugin_module._install_context_timing_hook()
    wrapper = module.conversation_loop.build_turn_context
    agent = SimpleNamespace(session_id="image-turn")
    with pytest.raises(RuntimeError, match="docket_native_image_input_unavailable"):
        wrapper(agent, "lost image, only text", None, [], "image-turn")
    assert calls == [] and len(persisted) == 1
    resumed = resume_trace_fixture(plugin_module, monkeypatch, context, "image-turn")
    resumed.update(
        native_image_bindings=context["native_image_bindings"], native_image_state="pending"
    )
    assert wrapper(agent, _parts(b"image one"), None, [], "image-turn") == "original result"
    assert resumed["native_image_state"] == "prepared"
    assert context["native_image_state"] == "failed"
    # Unrelated sessions neither inherit the source nor get blocked by this request.
    assert wrapper(SimpleNamespace(session_id="other"), "plain", None, [], "other") == (
        "original result"
    )


def test_only_persisted_native_failure_is_projected_once(plugin_module, monkeypatch):
    context, persisted, _scheduled = _context(plugin_module, monkeypatch)
    with pytest.raises(RuntimeError, match="docket_native_image_input_unavailable"):
        plugin_module._verify_native_image_input("image-turn", "", [])
    module = ModuleType("gateway.platforms.base")
    module.SendResult = SimpleNamespace
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", module)
    sent = []

    async def send(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(success=True)

    adapter = SimpleNamespace(send=send, _docket_provenance_contexts={(
        context["guild_id"], context["source_channel_id"], context["source_message_id"],
    ): context})
    plugin_module._install_provenance_delivery_guard(adapter)
    args = {"chat_id": context["source_channel_id"], "reply_to": context["source_message_id"]}
    assert asyncio.run(adapter.send(**args, content="Generic Hermes error")).success is False
    blocked = asyncio.run(adapter.send(**args, content=plugin_module._NATIVE_IMAGE_FAILURE))
    assert blocked.success is False
    assert sent == []
    monkeypatch.setattr(plugin_module, "_discord_runtime", lambda: (None, adapter, None))
    monkeypatch.setattr(plugin_module, "_post_agent_response_delivery", lambda *_a, **_k: None)
    monkeypatch.setattr(plugin_module, "_complete_interactive_ingress", lambda *_a, **_k: None)
    asyncio.run(plugin_module._deliver_persisted_deterministic_response(context))
    assert len(sent) == len(persisted) == 1
    assert sent[0]["content"] == plugin_module._NATIVE_IMAGE_FAILURE
    assert context["delivery_recorded"] is True
    plugin_module._on_post_llm_call(task_id="image-turn", assistant_response="generic error")
    assert asyncio.run(adapter.send(**args, content="generic error")).success is False
    assert len(sent) == len(persisted) == 1
    key = (context["guild_id"], context["source_channel_id"], "new")
    adapter._docket_provenance_contexts[key] = {"terminal": False}
    sent_new = asyncio.run(adapter.send(chat_id=context["source_channel_id"], content="new reply"))
    assert sent_new.success


@pytest.mark.parametrize("missing", [
    "_install_context_timing_hook", "_install_native_image_routing",
])
def test_interactive_registration_requires_native_input_boundary(
    plugin_module, monkeypatch, missing,
):
    monkeypatch.setattr(plugin_module, "_validate_channel_lanes", lambda: None)
    monkeypatch.setattr(plugin_module._SCHEMA_DISCLOSURE, "install_hermes_progressive_schema_patch",
                        lambda: True)
    for name in ("_install_context_timing_hook", "_install_native_image_routing",
                 "_install_schema_timing_hook"):
        monkeypatch.setattr(plugin_module, name, lambda: True)
    monkeypatch.setattr(plugin_module, missing, lambda: False)
    monkeypatch.setattr(plugin_module, "_owns_discord_gateway_lifetime", lambda _ctx: True)
    monkeypatch.setattr(plugin_module, "_start_gateway_lifetime",
                        lambda: pytest.fail("must fail before admitting execution"))
    with pytest.raises(RuntimeError, match="native-image boundary is unavailable"):
        plugin_module.register(SimpleNamespace())
