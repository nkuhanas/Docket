import importlib.util
from pathlib import Path

import pytest
import yaml

from docket.tool_contracts import contract_tool_names

SCRIPT = Path("scripts/sync_hermes_docket_config.py")


def _module():
    spec = importlib.util.spec_from_file_location("sync_hermes_docket_config", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sync_updates_only_managed_docket_tool_block() -> None:
    module = _module()
    active = """model:
  default: custom
display:
  skin: custom
  tool_progress: all
  interim_assistant_messages: true
  show_commentary: true
  background_process_notifications: all
platform_toolsets:
  cli:
    - cronjob
  discord:
    - cronjob
    - terminal
mcp_servers:
  docket:
    tools:
      include:
        - docket_old
      prompts: false
custom: keep-me
session_reset:
  mode: none
compression:
  threshold: 0.85
tools:
  unrelated: keep-tool-setting
auxiliary:
  vision:
    provider: custom
"""
    template = """mcp_servers:
  docket:
    tools:
      include:
        - docket_one
        - docket_two
      prompts: false
display:
  tool_progress: log
  interim_assistant_messages: false
  show_commentary: false
  background_process_notifications: off
platform_toolsets:
  cli:
    - cronjob
  discord:
    - terminal
session_reset:
  mode: both
  idle_minutes: 120
compression:
  threshold: 0.50
tools:
  tool_search:
    enabled: "on"
auxiliary:
  compression:
    provider: codex
    model: gpt-5.6-luna
"""

    updated = module.synchronize(active, template)

    assert "default: custom" in updated
    assert "custom: keep-me" in updated
    assert "skin: custom" in updated
    assert "tool_progress: log" in updated
    assert "interim_assistant_messages: false" in updated
    assert "show_commentary: false" in updated
    assert "background_process_notifications: off" in updated
    assert "  cli:\n    - cronjob\n" in updated
    assert "  discord:\n    - terminal\n" in updated
    assert "docket_old" not in updated
    assert "        - docket_one\n        - docket_two\n" in updated
    assert "session_reset:\n  mode: both\n  idle_minutes: 120\n" in updated
    assert "compression:\n  threshold: 0.50\n" in updated
    assert "unrelated: keep-tool-setting" in updated
    assert 'tool_search:\n    enabled: "on"\n' in updated
    assert "vision:\n    provider: custom\n" in updated
    assert "compression:\n    provider: codex\n    model: gpt-5.6-luna\n" in updated


def test_sync_adds_missing_performance_sections_without_replacing_parent_settings() -> None:
    module = _module()
    active = """display:
  tool_progress: all
  interim_assistant_messages: true
  show_commentary: true
  background_process_notifications: all
platform_toolsets:
  discord:
    - terminal
mcp_servers:
  docket:
    tools:
      include:
        - docket_old
      prompts: false
tools:
  unrelated: preserve
auxiliary:
  vision:
    provider: auto
"""
    template = Path("hermes/config.example.yaml").read_text(encoding="utf-8")

    updated = module.synchronize(active, template)
    parsed = yaml.safe_load(updated)

    assert parsed["session_reset"]["mode"] == "both"
    assert parsed["compression"]["codex_gpt55_autoraise"] is False
    assert parsed["tools"]["unrelated"] == "preserve"
    assert parsed["tools"]["tool_search"]["enabled"] == "on"
    assert parsed["auxiliary"]["vision"]["provider"] == "auto"
    assert parsed["auxiliary"]["compression"]["model"] == "gpt-5.6-luna"


def test_sync_fails_closed_on_unmanaged_or_ambiguous_block() -> None:
    module = _module()
    bad = """    tools:\n      include:\n        - shell\n      prompts: false\n"""
    with pytest.raises(module.HermesConfigSyncError):
        module.synchronize(bad, bad)


def test_docket_discord_profile_has_no_mutation_escape_capabilities() -> None:
    module = _module()
    template = Path("hermes/config.example.yaml").read_text(encoding="utf-8")
    start, end = module._nested_section(template, "platform_toolsets", "discord")
    toolset = template[start:end]

    for forbidden in (
        "browser",
        "code_execution",
        "computer_use",
        "delegation",
        "file",
        "terminal",
        "web",
    ):
        assert f"    - {forbidden}\n" not in toolset
    assert template.count("        - docket_commit_changeset\n") == 1
    assert template.count("        - docket_resolve_conflict\n") == 1


def test_example_profiles_allow_exact_clean_contract_tools() -> None:
    interactive = yaml.safe_load(
        Path("hermes/config.example.yaml").read_text(encoding="utf-8")
    )
    triage = yaml.safe_load(
        Path("hermes/triage-config.example.yaml").read_text(encoding="utf-8")
    )

    assert set(interactive["mcp_servers"]["docket"]["tools"]["include"]) == set(
        contract_tool_names("interactive")
    )
    assert set(triage["mcp_servers"]["docket-triage"]["tools"]["include"]) == set(
        contract_tool_names("triage")
    )
    assert interactive["session_reset"] == {
        "mode": "both",
        "at_hour": 4,
        "idle_minutes": 120,
        "notify": False,
    }
    assert interactive["compression"]["codex_gpt55_autoraise"] is False
    assert interactive["tools"]["tool_search"]["enabled"] == "on"
    assert interactive["auxiliary"]["compression"] == {
        "provider": "codex",
        "model": "gpt-5.6-luna",
        "timeout": 120,
        "reasoning_effort": "low",
    }
