import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def request_plugin(monkeypatch):
    path = Path("hermes/plugin/docket_discord/__init__.py")
    spec = importlib.util.spec_from_file_location("docket_request_bridge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_read_token", lambda: "synthetic-correlation-key")
    monkeypatch.setattr(module, "_operator_preferences", lambda: "")
    return module


def _event():
    return SimpleNamespace(
        text="Synthetic resolved request",
        message_id="000000000000000006",
        media_urls=[],
        raw_message=SimpleNamespace(content="Synthetic reported wording", attachments=[]),
        source=SimpleNamespace(
            platform="discord",
            user_id="000000000000000001",
            guild_id="000000000000000002",
            chat_id="000000000000000003",
        ),
    )


def _store():
    return SimpleNamespace(get_or_create_session=lambda _source: SimpleNamespace(session_id="task"))


@pytest.mark.adversarial
def test_capture_failures_do_not_block_foreground_and_background_cannot_inherit(
    request_plugin,
    monkeypatch,
):
    calls = []

    def internal(path, payload):
        calls.append((path, payload))
        if path.endswith("/requests"):
            return {
                "request_ref": "req_" + "0" * 26,
                "state": "active",
                "completion_token": "a" * 32,
            }
        if path.endswith("/records") or path.endswith("/attachments"):
            raise RuntimeError("Synthetic archive failure")
        return {"ok": True}

    monkeypatch.setattr(request_plugin, "_docket_internal_request", internal)
    monkeypatch.setattr(request_plugin, "_attachment_manifests", lambda _event: [{"file": "test"}])
    rewritten = request_plugin._pre_gateway_dispatch(_event(), session_store=_store())
    assert rewritten["action"] == "rewrite"
    args = {"invocation_binding": "model-forgery", "request_ref": "model-forgery"}
    assert (
        request_plugin._on_pre_tool_call(
            tool_name="mcp__docket__docket_commit_changeset",
            task_id="task",
            session_id="task",
            tool_call_id="commit",
            turn_id="foreground",
            args=args,
        )
        is None
    )
    encoded, _signature = args["invocation_binding"].split(".")
    binding = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    assert binding["format"] == 3 and binding["operation_key"] == "commit"
    assert "request_ref" not in args and "utterance_ref" not in binding
    denied_args = {}
    denied = request_plugin._on_pre_tool_call(
        tool_name="mcp__docket__docket_commit_changeset",
        task_id="subagent",
        session_id="task",
        tool_call_id="copied",
        turn_id="background",
        args=denied_args,
    )
    assert denied["action"] == "block"
    assert "invocation_binding" not in denied_args
    request_plugin._on_post_llm_call(
        task_id="task",
        session_id="task",
        turn_id="foreground",
        assistant_response="Synthetic completed response",
    )
    assert any(path.endswith("/executions/complete") for path, _payload in calls)


def test_missing_action_admission_fails_closed(request_plugin, monkeypatch):
    def unavailable(*_args):
        raise RuntimeError("Synthetic attribution failure")

    monkeypatch.setattr(request_plugin, "_docket_internal_request", unavailable)
    result = request_plugin._pre_gateway_dispatch(_event(), session_store=_store())
    assert result == {"action": "skip", "reason": "docket-request-admission-unavailable"}
    assert not request_plugin._TRACE_CONTEXTS


def test_missing_foreground_task_does_not_admit_a_request(request_plugin):
    result = request_plugin._pre_gateway_dispatch(_event())
    assert result["action"] == "skip"
