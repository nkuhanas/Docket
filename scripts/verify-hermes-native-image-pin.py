"""Offline check of native image delivery through the pinned Hermes classes.

Mount only this file and the Docket plugin read-only. Run without network,
credentials, production data or a model invocation. This checks image-byte
delivery to the context/Responses conversion boundaries, not OCR accuracy or
provider acceptance. The PNG is a synthetic one-pixel fixture.
"""

import base64
import hashlib
import importlib.util
import inspect
import json
import struct
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from agent import conversation_loop
from agent.codex_responses_adapter import _chat_content_to_responses_parts
from agent.image_routing import build_native_content_parts
from gateway.run import GatewayRunner

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "docket_native_image_pin_verification", root / "hermes/plugin/docket_discord/__init__.py",
)
assert spec is not None and spec.loader is not None
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
source = inspect.getsource(GatewayRunner._prepare_inbound_message_text)
assert "self._decide_image_input_mode" in source
gateway_source = inspect.getsource(GatewayRunner)
assert "build_native_content_parts" in gateway_source and "_run_message" in gateway_source
assert "build_turn_context(" in inspect.getsource(conversation_loop.run_conversation)
plugin._trusted_ingress_context = lambda source: (1, 2, 3, None) if source == "fixture" else None
assert plugin._install_native_image_routing()
gateway = GatewayRunner.__new__(GatewayRunner)
assert gateway._decide_image_input_mode(source="fixture") == "native"
wrapper = GatewayRunner._decide_image_input_mode
assert plugin._install_native_image_routing()
assert GatewayRunner._decide_image_input_mode is wrapper


def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


raw = (
    b"\x89PNG\r\n\x1a\n"
    + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    + png_chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff"))
    + png_chunk(b"IEND", b"")
)
context = {
    "terminal": False, "turn_id": None,
    "native_image_state": "pending",
    "native_image_bindings": [{
        "source_ref": "src_" + "1" * 26, "content_hash": hashlib.sha256(raw).hexdigest(),
    }],
}
plugin._TRACE_CONTEXTS["offline-native-fixture"] = context
plugin._enqueue_trace_update = lambda *_args, **_kwargs: None
persisted = []
plugin._persist_deterministic_response = lambda ctx: (
    persisted.append(ctx["deterministic_response_text"]) or "rsp_" + "1" * 26
)


def schedule_without_discord(ctx):
    ctx["deterministic_delivery_scheduled"] = True


plugin._schedule_persisted_deterministic_response = schedule_without_discord
assert plugin._install_context_timing_hook()


class FixtureStop(Exception):
    pass


def stop_before_side_effects():
    raise FixtureStop


def build_context(content):
    return conversation_loop.build_turn_context(
        SimpleNamespace(session_id="offline-native-fixture"), content, None, None,
        "offline-native-fixture", None, None, restore_or_build_system_prompt=None,
        install_safe_stdio=stop_before_side_effects,
        sanitize_surrogates=None, summarize_user_message_for_log=None, set_session_context=None,
        set_current_write_origin=None, ra=None,
    )


with TemporaryDirectory(prefix="docket-native-pin-") as temporary:
    path = Path(temporary) / "fixture.png"
    path.write_bytes(raw)
    parts, skipped = build_native_content_parts("Read this synthetic image.", [str(path)])
    assert skipped == []
    converted = _chat_content_to_responses_parts(parts, role="user")
    images = [part for part in converted if part["type"] == "input_image"]
    assert len(images) == 1
    assert base64.b64decode(images[0]["image_url"].partition(",")[2], validate=True) == raw
    try:
        build_context(parts)
    except FixtureStop:
        pass
    else:
        raise AssertionError("The real context builder did not reach its first callback")
    assert context["native_image_state"] == "prepared" and not persisted
    missing_path = str(path.with_name("absent.png"))
    missing, skipped = build_native_content_parts("Missing image.", [missing_path])
    assert skipped == [str(path.with_name("absent.png"))]
    try:
        build_context(missing)
    except RuntimeError as exc:
        assert str(exc) == "docket_native_image_input_unavailable"
    else:
        raise AssertionError("Lost image reached interpretation without its source bytes")
    assert context["native_image_state"] == "failed"
    assert persisted == [plugin._NATIVE_IMAGE_FAILURE]

print(json.dumps({
    "status": "passed", "scope": "native image/context/Responses seams; no live provider request",
}))
