from pathlib import Path


def test_hermes_readiness_uses_generated_mcp_tool_count() -> None:
    script = Path("scripts/docket").read_text(encoding="utf-8")

    assert "scripts/compose-mcp-smoke.py" in script
    assert "expected_tool_count" in script
    assert "registered 20 tool" not in script
