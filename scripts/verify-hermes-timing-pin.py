"""Offline, payload-free timing-seam verification inside the pinned Hermes image.

Mount only the plugin directory and this script read-only beneath /workspace;
no network, .env or production state is needed. This exercises the actual builder and hook registry,
not a live model request, and is not latency benchmark evidence.
"""

import importlib.util
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import model_tools
import run_agent
from agent import conversation_loop
from hermes_cli.plugins import VALID_HOOKS

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "docket_timing_pin_verification", root / "hermes/plugin/docket_discord/__init__.py",
)
assert spec is not None and spec.loader is not None
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
assert {"pre_api_request", "post_api_request", "api_request_error"} <= VALID_HOOKS
source = inspect.getsource(conversation_loop.run_conversation)
for hook in ("pre_api_request", "post_api_request"):
    assert f'"{hook}",' in source
assert "api_request_id=api_request_id" in source and "started_at=api_start_time" in source

sent = []
plugin._enqueue_trace_update = lambda _context, **values: sent.append(values)
plugin._TRACE_CONTEXTS["offline-fixture"] = {"terminal": False, "turn_id": None}
assert plugin._install_context_timing_hook()


class FixtureStop(Exception):
    pass


def stop_before_side_effects():
    raise FixtureStop


try:
    # The real builder's first callback halts before any provider, memory or disk work.
    conversation_loop.build_turn_context(
        SimpleNamespace(session_id="offline-fixture"), None, None, None, "offline-fixture",
        None, None, restore_or_build_system_prompt=None,
        install_safe_stdio=stop_before_side_effects,
        sanitize_surrogates=None, summarize_user_message_for_log=None, set_session_context=None,
        set_current_write_origin=None, ra=None,
    )
except FixtureStop:
    pass
else:
    raise AssertionError("The pinned context builder did not invoke its first callback")
assert len(sent) == 1 and sent[0]["timing"]["phase"] == "context_schema"
binding = dict(task_id="offline-fixture", turn_id="offline-turn", api_request_id="offline-api",
               started_at=1.0)
plugin._on_pre_api_request(**binding)
plugin._on_api_request_finished(**binding)
assert len(sent) == 2 and sent[1]["timing"]["phase"] == "model_request"
assert all(set(row["timing"]) == {"span_id", "phase", "started_at", "ended_at"} for row in sent)
assert plugin._SCHEMA_DISCLOSURE.install_hermes_progressive_schema_patch()
assert plugin._install_schema_timing_hook()
assert model_tools.handle_function_call is run_agent.handle_function_call
# Avoid registry discovery work; the real dispatcher and Docket bridge still
# process this exact fixture, including the early return that bypasses hooks.
model_tools.get_tool_definitions = lambda **_kwargs: [{
    "type": "function", "function": {
        "name": "mcp__docket__docket_commit_changeset",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}]
described = run_agent.handle_function_call(
    "tool_describe", {"name": "mcp__docket__docket_commit_changeset"},
    task_id="offline-fixture", turn_id="offline-turn", tool_call_id="offline-schema",
)
assert json.loads(described)["parameters"]["properties"] == {}
assert len(sent) == 3 and sent[-1]["timing"]["phase"] == "context_schema"
print(json.dumps({"status": "passed", "scope": "pinned timing seams; no live provider request"}))
