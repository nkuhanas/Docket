from pathlib import Path


def test_hermes_readiness_uses_generated_mcp_tool_count() -> None:
    script = Path("scripts/docket").read_text(encoding="utf-8")
    deploy = script.split("\ndeploy() {", 1)[1].split("\n}\n", 1)[0]

    assert "scripts/compose-mcp-smoke.py" in script
    assert "scripts/sync_hermes_docket_config.py" in script
    assert "hermes/config.example.yaml" in script
    assert "expected_tool_count" in script
    assert "registered 20 tool" not in script
    assert '"$ROOT/scripts/prepare-hermes-home.sh"' in deploy
    assert deploy.index('"$ROOT/scripts/prepare-hermes-home.sh"') < deploy.index(
        "compose up -d"
    )


def test_gmail_triage_installer_pins_an_isolated_profile_and_local_delivery() -> None:
    script = Path("scripts/setup-hermes-triage.sh").read_text(encoding="utf-8")
    config = Path("hermes/triage-config.example.yaml").read_text(encoding="utf-8")
    skill = Path(
        "hermes/plugin/docket_discord/skills/docket-triage/SKILL.md"
    ).read_text(encoding="utf-8")
    launcher = Path("hermes/scripts/docket-gmail-triage.sh").read_text(
        encoding="utf-8"
    )

    assert "hermes profile create" in script
    assert "--clone --no-alias" in script
    assert '--script "docket-gmail-triage.sh"' in script
    assert "--no-agent" in script
    assert "--deliver log" in script
    assert "--deliver discord" not in script
    assert "mcp test docket-triage" in script
    assert "hermes -p docket-triage" in launcher
    assert "--skills docket-triage" in launcher
    assert "--oneshot" in launcher
    assert "cli: []" in config
    assert "cron: []" in config
    assert "plugins:\n  enabled: []" in config
    assert "discord:" not in config
    assert "Return only `[SILENT]` after a normal run." in skill
