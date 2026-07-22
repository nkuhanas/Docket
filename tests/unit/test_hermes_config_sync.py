import importlib.util
from pathlib import Path

import pytest

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
mcp_servers:
  docket:
    tools:
      include:
        - docket_old
      prompts: false
custom: keep-me
"""
    template = """mcp_servers:
  docket:
    tools:
      include:
        - docket_one
        - docket_two
      prompts: false
"""

    updated = module.synchronize(active, template)

    assert "default: custom" in updated
    assert "custom: keep-me" in updated
    assert "docket_old" not in updated
    assert "        - docket_one\n        - docket_two\n" in updated


def test_sync_fails_closed_on_unmanaged_or_ambiguous_block() -> None:
    module = _module()
    bad = """    tools:\n      include:\n        - shell\n      prompts: false\n"""
    with pytest.raises(module.HermesConfigSyncError):
        module.synchronize(bad, bad)
