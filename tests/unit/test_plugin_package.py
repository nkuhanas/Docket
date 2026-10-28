import json
import re
from pathlib import Path
from xml.etree import ElementTree

import yaml

from docket.tool_contracts import render_contract

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "plugin" / "docket"


def test_private_plugin_candidate_is_self_contained_and_has_no_credentials():
    manifest = json.loads((PACKAGE / "plugin.json").read_text())
    assert manifest["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert manifest["name"] == PACKAGE.name
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    interface = manifest["extensions"]["com.openai"]["interface"]
    assert len(interface["shortDescription"]) <= 30
    for key in ("logo", "composerIcon"):
        target = (PACKAGE / interface[key]).resolve()
        assert target.is_relative_to(PACKAGE.resolve())
        assert ElementTree.parse(target).getroot().attrib["viewBox"] == "0 0 96 96"
    mcp = json.loads((PACKAGE / "mcp.json").read_text())
    assert mcp["mcpServers"] == {
        "docket": {"type": "streamable-http", "url": "https://docket.you/mcp/"},
    }
    for item in PACKAGE.rglob("*"):
        assert not item.is_symlink()
        assert item.name not in (".env", "secrets", "node_modules", ".venv")


def test_skill_and_server_contract_share_the_native_boundary():
    text = (PACKAGE / "skills" / "docket" / "SKILL.md").read_text()
    metadata = yaml.safe_load(text.split("---", 2)[1])
    assert metadata["name"] == "docket"
    assert metadata["description"]
    assert "read-only" in text
    assert (PACKAGE / "contracts" / "plugin.md").read_text() == render_contract("plugin")
