from pathlib import Path


def test_hermes_readiness_uses_generated_mcp_tool_count() -> None:
    script = Path("scripts/docket").read_text(encoding="utf-8")
    deploy = script.split("\ndeploy() {", 1)[1].split("\n}\n", 1)[0]

    assert "scripts/compose-mcp-smoke.py" in script
    assert "expected_tool_count" in script
    assert "registered 20 tool" not in script
    assert '"$ROOT/scripts/prepare-hermes-home.sh"' in deploy
    assert deploy.index('"$ROOT/scripts/prepare-hermes-home.sh"') < deploy.index(
        "compose up -d"
    )
