import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

from docket.internal_api.schemas import McpTraceCheckpoint


@pytest.fixture
def timing_plugin(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "docket_timing_plugin", "hermes/plugin/docket_discord/__init__.py",
    )
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    base = datetime.now(UTC)
    clock = {"elapsed": 0, "wall_offset": 0}

    class Clock:
        @staticmethod
        def now(_tz):
            return base + timedelta(seconds=clock["elapsed"] + clock["wall_offset"])

    monkeypatch.setattr(plugin, "datetime", Clock)
    monkeypatch.setattr(plugin, "time", SimpleNamespace(monotonic=lambda: clock["elapsed"]))
    sent = []
    monkeypatch.setattr(plugin, "_enqueue_trace_update", lambda _ctx, **kw: sent.append(kw))
    context = dict(
        trace_ref="trace_" + "0" * 26, utterance_ref="utt_" + "0" * 26,
        guild_id="111111111111111111", actor_id="222222222222222222",
        source_channel_id="333333333333333333", source_message_id="444444444444444444",
        tool_contract_version=plugin._TOOL_CONTRACT_VERSION,
        tool_contract_hash=plugin._TOOL_CONTRACT_HASH, caller_profile="interactive",
        turn_started_at=base.isoformat(), calls={}, next_ordinal=1,
        turn_id=None, started=False, terminal=False,
    )
    plugin._TRACE_CONTEXTS["timing-test"] = context
    return plugin, context, clock, sent


def _binding(**kwargs):
    return dict(task_id="timing-test", turn_id="turn-1", api_request_id="api-1", **kwargs)


def test_real_api_hook_keys_distinguish_retry_and_duplicate_callbacks(timing_plugin):
    plugin, context, clock, sent = timing_plugin
    first = _binding(started_at=42.1)
    plugin._on_pre_api_request(**first, request={"messages": "secret prompt"})
    clock["elapsed"] = 2
    plugin._on_api_request_finished(**first, error={"message": "secret error"})
    # Exact duplicate notifications (including late starts) cannot add another interval.
    plugin._on_pre_api_request(**first)
    plugin._on_api_request_finished(**first)
    clock["elapsed"] = 5
    retry = _binding(started_at=47.1)
    plugin._on_pre_api_request(**retry)
    clock["elapsed"] = 8
    plugin._on_api_request_finished(**retry, response="secret result")
    assert len(sent) == 2 and context["started"] is True
    assert {item["timing"]["phase"] for item in sent} == {"model_request"}
    assert len({item["timing"]["span_id"] for item in sent}) == 2
    assert "secret" not in str(context) + str(sent)
    assert sum((datetime.fromisoformat(row["timing"]["ended_at"])
                - datetime.fromisoformat(row["timing"]["started_at"])).total_seconds()
               for row in sent) == 5  # Retry backoff is NOT model time.


def test_missing_and_cross_turn_hooks_cannot_infer_model_time(timing_plugin):
    plugin, context, clock, sent = timing_plugin
    plugin._on_api_request_finished(**_binding(started_at=1))
    plugin._on_pre_api_request(**_binding(started_at=2))
    clock["elapsed"] = 100
    plugin._on_api_request_finished(**{**_binding(started_at=2), "turn_id": "different"})
    plugin._on_api_request_finished(**_binding(started_at=3))
    assert sent == [] and "timings" not in context
    context["terminal"] = True
    plugin._on_api_request_finished(**_binding(started_at=2))
    assert sent == []


def test_clock_jump_is_not_reported_as_measured_work(timing_plugin):
    plugin, context, clock, sent = timing_plugin
    plugin._on_pre_api_request(**_binding(started_at=1))
    clock["elapsed"] = 2
    clock["wall_offset"] = 90
    plugin._on_api_request_finished(**_binding(started_at=1))
    assert sent == [] and "timings" not in context


def test_timing_checkpoint_paginates_without_calls_and_reuses_lost_ack_body(
    timing_plugin, monkeypatch,
):
    plugin, context, clock, _sent = timing_plugin
    for index in range(53):
        plugin._on_pre_api_request(**_binding(started_at=index))
        clock["elapsed"] += 1
        plugin._on_api_request_finished(**_binding(started_at=index))
    pages = []

    def send(_path, body, **_kwargs):
        pages.append(json.loads(json.dumps(body)))
        McpTraceCheckpoint.model_validate(body)
        if len(pages) == 1:
            raise OSError("Lost acknowledgment")
        return {"trace_ref": context["trace_ref"], "disposition": "updated"}

    monkeypatch.setattr(plugin, "_docket_internal_request", send)
    assert plugin._checkpoint_trace(context, turn_status="completed") is True
    assert pages[0] == pages[1]
    assert [len(page["timings"]) for page in pages[1:]] == [25, 25, 3]
    assert [page["turn_status"] for page in pages[1:]] == ["running", "running", "completed"]
    assert all(not page["calls"] and len(json.dumps(page).encode()) <= 16_384 for page in pages)


def test_schema_tool_and_local_validation_measure_only_their_own_work(timing_plugin, monkeypatch):
    plugin, context, clock, sent = timing_plugin
    kwargs = dict(task_id="timing-test", turn_id="turn-1", tool_call_id="describe-1")
    dispatcher = _install_schema_fixture(plugin, monkeypatch, clock)
    assert dispatcher("tool_describe", {
        "name": "mcp__docket__docket_stage_changes", "private": "not retained",
    }, **kwargs) == "unchanged result"
    assert sent[0]["timing"]["phase"] == "context_schema"

    def validate(*_args):
        clock["elapsed"] += 2
        return "Bad schema"

    monkeypatch.setattr(plugin, "_validate_authority_arguments_locally", validate)
    monkeypatch.setattr(plugin, "_checkpoint_trace", lambda *_a, **_k: True)
    result = plugin._on_pre_tool_call(tool_name="mcp__docket__docket_commit_changeset",
                                     args={"private": "not retained"}, **{
                                         **kwargs, "tool_call_id": "commit-1",
                                     })
    assert result == {"action": "block", "message": "Bad schema"}
    timing = [row["timing"] for row in sent if "timing" in row]
    assert [row["phase"] for row in timing] == ["context_schema", "local_validation"]
    assert (datetime.fromisoformat(timing[-1]["ended_at"])
            - datetime.fromisoformat(timing[-1]["started_at"])).total_seconds() == 2
    assert "not retained" not in str(context) + str(sent)


def _install_schema_fixture(plugin, monkeypatch, clock):
    def handle_function_call(function_name, function_args, task_id=None, tool_call_id=None,
                             session_id=None, turn_id=None):
        clock["elapsed"] += 1
        return "unchanged result"

    module = ModuleType("model_tools")
    module.handle_function_call = handle_function_call
    runtime = ModuleType("run_agent")
    runtime.handle_function_call = handle_function_call
    monkeypatch.setitem(sys.modules, "model_tools", module)
    monkeypatch.setitem(sys.modules, "run_agent", runtime)
    assert plugin._install_schema_timing_hook()
    assert module.handle_function_call is runtime.handle_function_call
    assert plugin._install_schema_timing_hook()
    assert module.handle_function_call is runtime.handle_function_call
    return module.handle_function_call


def test_other_schema_tools_are_not_mislabeled_docket_preparation(timing_plugin, monkeypatch):
    plugin, _context, clock, sent = timing_plugin
    dispatcher = _install_schema_fixture(plugin, monkeypatch, clock)
    assert dispatcher("tool_describe", {"name": "other_tool"}, "timing-test") == "unchanged result"
    assert dispatcher("tool_search", {"name": "docket_test"}, "timing-test") == "unchanged result"
    assert sent == []


def test_context_wrapper_preserves_arguments_results_errors_and_discovery_binding(
    timing_plugin, monkeypatch,
):
    plugin, context, clock, sent = timing_plugin
    received = []

    def build_turn_context(agent, user_message, system_message, conversation_history, task_id):
        received.append((agent, user_message, system_message, conversation_history, task_id))
        clock["elapsed"] += 3
        if user_message == "raise":
            raise ValueError("Original failure")
        return "unchanged result"

    module = ModuleType("agent")
    module.conversation_loop = SimpleNamespace(build_turn_context=build_turn_context)
    monkeypatch.setitem(sys.modules, "agent", module)
    assert plugin._install_context_timing_hook() is True
    wrapper = module.conversation_loop.build_turn_context
    assert plugin._install_context_timing_hook() is True
    assert module.conversation_loop.build_turn_context is wrapper
    agent = SimpleNamespace(session_id="timing-test")
    assert wrapper(agent, "private", None, [], "timing-test") == "unchanged result"
    assert received == [(agent, "private", None, [], "timing-test")]
    with pytest.raises(ValueError, match="Original failure"):
        wrapper(agent=agent, user_message="raise", system_message=None,
                conversation_history=[], task_id="timing-test")
    assert len(sent) == 2 and "private" not in str(context)
    assert {row["timing"]["phase"] for row in sent} == {"context_schema"}
    # A new discovery uses the latest observer, not a stale module's context map.
    with monkeypatch.context() as patch:
        patch.setattr(plugin, "_TRACE_CONTEXTS", {})
        plugin._install_context_timing_hook()
        assert wrapper(agent, "after discovery", None, [], "timing-test") == "unchanged result"
    assert len(sent) == 2


def test_context_wrapper_refuses_changed_pinned_signature(timing_plugin, monkeypatch):
    plugin, _context, _clock, _sent = timing_plugin
    module = ModuleType("agent")
    module.conversation_loop = SimpleNamespace(build_turn_context=lambda changed: changed)
    monkeypatch.setitem(sys.modules, "agent", module)
    with pytest.raises(RuntimeError, match="seam changed"):
        plugin._install_context_timing_hook()


def test_registered_hooks_match_plugin_manifest(timing_plugin, monkeypatch):
    plugin, _context, _clock, _sent = timing_plugin
    for name in (
        "_validate_channel_lanes", "_start_trace_delivery_worker", "_start_projection_server",
    ):
        monkeypatch.setattr(plugin, name, lambda: None)
    monkeypatch.setattr(plugin, "_install_context_timing_hook", lambda: True)
    monkeypatch.setattr(plugin, "_install_schema_timing_hook", lambda: True)
    monkeypatch.setattr(plugin, "_owns_discord_gateway_lifetime", lambda _ctx: False)
    monkeypatch.setattr(plugin._SCHEMA_DISCLOSURE, "install_hermes_progressive_schema_patch",
                        lambda: True)
    hooks = {}
    ctx = SimpleNamespace(register_hook=lambda name, fn: hooks.update({name: fn}),
                          register_skill=lambda *_args: None)
    plugin.register(ctx)
    manifest = yaml.safe_load(Path("hermes/plugin/docket_discord/plugin.yaml").read_text())
    assert set(hooks) == set(manifest["provides_hooks"])
    assert {"pre_api_request", "post_api_request", "api_request_error"} <= hooks.keys()
    assert hooks["api_request_error"] is hooks["post_api_request"]
