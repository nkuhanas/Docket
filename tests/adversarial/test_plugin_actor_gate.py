import importlib.util
import sys
import uuid
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

from docket.security import (
    issue_projection_approval_token,
    issue_projection_decision_approval_token,
    issue_projection_local_action_token,
    issue_projection_review_navigation_token,
)

PLUGIN_PATH = Path("hermes/plugin/docket_discord/__init__.py")


class Platform(Enum):
    DISCORD = "discord"


@pytest.fixture
def plugin_module():
    spec = importlib.util.spec_from_file_location("docket_discord_plugin", PLUGIN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
def test_non_command_daily_thread_message_is_dropped_before_model(
    plugin_module, monkeypatch
) -> None:
    monkeypatch.setenv("DOCKET_QUEUE_CHANNEL_ID", "queue")
    event = SimpleNamespace(
        text="please explain this card",
        message_id="message",
        source=SimpleNamespace(
            platform="discord",
            user_id="operator",
            guild_id="guild",
            chat_id="thread",
            parent_chat_id="queue",
        ),
    )

    assert plugin_module._pre_gateway_dispatch(event) == {
        "action": "skip",
        "reason": "invalid-docket-control",
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
    assert "state-changing Docket operation" in result["text"]
    assert "Referencing an existing record" in result["text"]


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


def test_plugin_rejects_aliased_channel_lanes(plugin_module, monkeypatch) -> None:
    monkeypatch.setenv("DOCKET_CHAT_CHANNEL_ID", "222222222222222222")
    monkeypatch.setenv("DOCKET_QUEUE_CHANNEL_ID", "222222222222222222")
    monkeypatch.setenv("DOCKET_SYSTEM_CHANNEL_ID", "333333333333333333")

    with pytest.raises(RuntimeError, match="must be distinct"):
        plugin_module._validate_channel_lanes()


def test_failed_item_can_render_one_canonical_ignore_control(plugin_module, monkeypatch) -> None:
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
            self.custom_id = kwargs["custom_id"]

    fake_discord = SimpleNamespace(
        Embed=FakeEmbed,
        ButtonStyle=SimpleNamespace(success=1, danger=2, secondary=3),
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
        ButtonStyle=SimpleNamespace(success=1, danger=2, secondary=3),
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

    assert len(view.items) == 1
    assert view.items[0].custom_id == f"dkt:n:{token}"
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

    monkeypatch.setattr(plugin_module, "_post_button_response", fake_post)
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
