import os
import re
import subprocess
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
    assert deploy.index('"$ROOT/scripts/prepare-hermes-home.sh"') < deploy.index("compose up -d")
    assert "gmail_triage_setup" in deploy
    assert deploy.index("compose up -d") < deploy.index("gmail_triage_setup")
    assert deploy.index("gmail_triage_setup") < deploy.index("postdeploy")


def test_deploy_drains_execution_but_preserves_queued_durable_work() -> None:
    script = Path("scripts/docket").read_text(encoding="utf-8")
    deploy = script.split("\ndeploy() {", 1)[1].split("\n}\n", 1)[0]
    ingress_deploy = script.split("\ndeploy_ingress() {", 1)[1].split("\n}\n", 1)[0]

    assert "where status = 'running'" in script
    assert "where status = 'delivering'" in script
    assert "reconciliation_required" not in script.split("operational_counts()", 1)[1].split(
        "\n}", 1
    )[0]
    assert "request_database_drain" in deploy
    assert "wait_for_database_drain" in deploy
    assert deploy.index("wait_for_database_drain") < deploy.index("wait_for_hermes_drain")
    assert "compose exec -T --user hermes hermes python" in script
    assert '"$state" == "draining 0"' in script
    assert '"$state" == "running 0"' not in script
    drained = deploy.index('wait_for_operational_idle')
    assert drained < deploy.index('backup=$(backup_database)', drained)
    assert deploy.index('backup=$(backup_database)', drained) < deploy.index(
        "alembic upgrade head", drained
    )
    assert "compose up -d --no-build --force-recreate docket hermes" in deploy
    assert "--force-recreate docket hermes discord-ingress" not in deploy

    for marker in (
        "quiesce-ingress-options",
        "discord-ingress-handoff",
        "--force-recreate discord-ingress",
        "regenerate-ingress-options",
    ):
        assert marker in ingress_deploy
    assert ingress_deploy.index("quiesce-ingress-options") < ingress_deploy.index(
        "discord-ingress-handoff"
    )
    assert ingress_deploy.index("discord-ingress-handoff") < ingress_deploy.index(
        "--force-recreate discord-ingress"
    )
    assert ingress_deploy.index("--force-recreate discord-ingress") < ingress_deploy.rindex(
        "regenerate-ingress-options"
    )


def test_production_reset_is_manifest_bound_and_swaps_only_after_clean_verification() -> None:
    script = Path("scripts/docket").read_text(encoding="utf-8")
    readiness = script.split("\ntracked_context_readiness() (", 1)[1].split("\n)\n", 1)[0]
    reset = script.split("\nproduction_reset() {", 1)[1].split("\n}\n", 1)[0]

    assert "build-manifest" in readiness
    assert "production-reset-authorization.txt" in readiness
    assert "authorization-text" in readiness
    assert '"$execution_flag" == "--execute"' in reset
    assert reset.count("verify-execution") == 2
    first_verify = reset.index("verify-execution")
    drain = reset.index("request_database_drain")
    second_verify = reset.index("verify-execution", first_verify + 1)
    stop_writers = reset.index(
        "compose stop -t 30 discord-ingress hermes docket", second_verify
    )
    materialize = reset.index("materialize-clean", stop_writers)
    clean_authority = reset.index("verify-authority", materialize)
    rename_old = reset.index("ALTER DATABASE docket RENAME TO", clean_authority)
    rename_clean = reset.index("ALTER DATABASE $clean_database RENAME TO docket")
    postdeploy = reset.index("postdeploy")
    completion = reset.index("record-completion")
    drop_old = reset.rindex('drop_cutover_database "$quarantine_database"')

    assert first_verify < drain < second_verify < stop_writers
    assert stop_writers < materialize < clean_authority < rename_old < rename_clean
    assert rename_clean < postdeploy < completion < drop_old
    assert "matching pre-reset image is unavailable" in reset
    assert "production_reset_authorization" not in reset
    assert "old_renamed" in reset
    assert 'docker_engine image tag "$old_image" docket-docket:latest' in reset


def test_production_reset_multiline_shell_commands_preserve_their_arguments() -> None:
    script = Path("scripts/docket").read_text(encoding="utf-8")
    reset = script.split("\nproduction_reset() {", 1)[1].split("\n}\n", 1)[0]
    commands = re.findall(r"docket sh -ec \\\n\s+'(.*?)'", reset, flags=re.DOTALL)

    assert len(commands) == 6
    for command in commands:
        lines = command.splitlines()
        assert len(lines) > 1
        assert all(line.rstrip().endswith("\\") for line in lines[:-1])

    assert "trap - EXIT INT TERM" in reset
    assert "${reset_complete:-0}" in reset


def test_gmail_triage_installer_pins_an_isolated_profile_and_local_delivery() -> None:
    script = Path("scripts/setup-hermes-triage.sh").read_text(encoding="utf-8")
    config = Path("hermes/triage-config.example.yaml").read_text(encoding="utf-8")
    skill = Path("hermes/plugin/docket_discord/skills/docket-triage/SKILL.md").read_text(
        encoding="utf-8"
    )
    launcher = Path("hermes/scripts/docket-gmail-triage.sh").read_text(encoding="utf-8")

    assert "hermes profile create" in script
    assert "--clone --no-alias" in script
    assert '--script "docket-gmail-triage.sh"' in script
    assert "--no-agent" in script
    assert "--deliver local" in script
    assert "--deliver log" not in script
    assert "--deliver discord" not in script
    assert 'JOB_SCHEDULE="every 5m"' in script
    assert "hermes cron edit" in script
    assert script.count("hermes cron list --all") == 2
    assert "hermes cron list)" not in script
    assert "mcp test docket-triage" in script
    assert "discovered_tool_count" in script
    assert "must discover exactly four tools" in script
    for tool in (
        "docket_get_triage_context",
        "docket_submit_triage_analysis",
        "docket_get_attention_case",
        "docket_apply_existing_suppression",
    ):
        assert tool in script
        assert tool in config
    assert "docket_submit_triage_decision" not in script
    assert "docket_submit_triage_decision" not in config
    assert "hermes -p docket-triage" in launcher
    assert "--skills docket-triage" in launcher
    assert "--oneshot" in launcher
    assert "request_dump_*.json" in launcher
    assert "exhausted its model request retries" in launcher
    assert '"$normalized_output" = "[SILENT]"' in launcher
    assert "preferences/TRIAGE.md" in launcher
    assert "head -c 16384" in launcher
    assert "operator-authored triage preferences" in launcher
    assert "tool-contract.md" in launcher
    assert "Docket triage tool contract exceeds 12 KiB" in launcher
    assert "contracts/triage.md" in script
    assert "returned unexpected model output" in launcher
    assert "exactly one source per run" in skill
    assert "caps the profile at one source" in skill
    assert "cli: []" in config
    assert "cron: []" in config
    assert "plugins:\n  enabled: []" in config
    assert "discord:" not in config
    assert "Return only `[SILENT]` after a normal run." in skill

    prepare = Path("scripts/prepare-hermes-home.sh").read_text(encoding="utf-8")
    assert "hermes/preferences/$preference.example.md" in prepare
    assert 'if [ ! -e "$destination" ]' in prepare
    assert '"$PREFERENCES_DIR/AGENT.md"' in prepare
    assert '"$PREFERENCES_DIR/TRIAGE.md"' in prepare


def test_gmail_triage_launcher_suppresses_success_and_fails_closed(tmp_path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_hermes = bin_dir / "hermes"
    fake_hermes.write_text(
        """#!/bin/sh
set -eu
case ${HERMES_FAKE_MODE:-silent} in
  silent) printf '[SILENT]\\n' ;;
  unexpected) printf 'source-derived output must not escape\\n' ;;
  dump)
    mkdir -p "$HERMES_HOME/profiles/docket-triage/sessions"
    : > "$HERMES_HOME/profiles/docket-triage/sessions/request_dump_test.json"
    printf '[SILENT]\\n'
    ;;
esac
""",
        encoding="utf-8",
    )
    fake_hermes.chmod(0o700)
    launcher = Path("hermes/scripts/docket-gmail-triage.sh").resolve()
    base_env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HERMES_HOME": str(tmp_path / "home"),
    }
    contract_path = tmp_path / "home" / "profiles" / "docket-triage" / "tool-contract.md"
    contract_path.parent.mkdir(parents=True)
    contract_path.write_text(
        Path("hermes/plugin/docket_discord/contracts/triage.md").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )

    silent = subprocess.run(
        ["sh", str(launcher)],
        check=False,
        capture_output=True,
        text=True,
        env=base_env,
    )
    assert silent.returncode == 0
    assert silent.stdout == ""
    assert silent.stderr == ""

    unexpected = subprocess.run(
        ["sh", str(launcher)],
        check=False,
        capture_output=True,
        text=True,
        env={**base_env, "HERMES_FAKE_MODE": "unexpected"},
    )
    assert unexpected.returncode == 1
    assert unexpected.stdout == ""
    assert unexpected.stderr == "Docket Gmail triage returned unexpected model output.\n"

    dumped = subprocess.run(
        ["sh", str(launcher)],
        check=False,
        capture_output=True,
        text=True,
        env={**base_env, "HERMES_FAKE_MODE": "dump"},
    )
    assert dumped.returncode == 1
    assert dumped.stdout == ""
    assert dumped.stderr == "Docket Gmail triage exhausted its model request retries.\n"


def test_operator_script_separates_gmail_scan_from_semantic_triage() -> None:
    script = Path("scripts/docket").read_text(encoding="utf-8")
    gmail_cli = Path("src/docket/gmail_cli.py").read_text(encoding="utf-8")

    assert "gmail-status) gmail status" in script
    assert "gmail-scan) gmail scan" in script
    assert "gmail-triage-status) gmail_triage_control status" in script
    assert "gmail-triage-pause) gmail_triage_control pause" in script
    assert "gmail-triage-run) gmail_triage_control run" in script
    assert "gmail-triage-resume) gmail_triage_control resume" in script
    assert "gmail-propose-archive)" in script
    assert 'gmail propose-archive "${2:-}" "${3:-}" "${4:-}"' in script
    assert "gmail-reconcile-operation)" in script
    assert 'gmail reconcile-operation "${2:-}" "${3:-}"' in script
    assert "expected exactly one '$job_name' cron job" in script
    assert "hermes cron list --all" in script
    assert "one-shot triage requires the recurring job to be paused" in script
    assert "/opt/data/scripts/docket-gmail-triage.sh" in script
    assert "GmailIngestionService" in gmail_cli
    scan_function = gmail_cli.split("def _scan", 1)[1].split("def _propose_archive", 1)[0]
    assert "TriageService" not in scan_function
    assert "read_message(" not in gmail_cli
    assert "DOCKET_GMAIL_TRIAGE_SOURCE_ALLOWLIST='[]'" in script


def test_google_oauth_wrapper_auto_selects_remote_mode_without_a_browser() -> None:
    script = Path("scripts/setup-google-oauth.sh").read_text(encoding="utf-8")

    assert "DISPLAY" in script
    assert "WAYLAND_DISPLAY" in script
    assert "BROWSER" in script
    assert 'set -- --remote "$@"' in script
    assert "No SSH tunnel is required" in script


def test_hermes_oauth_recovery_keeps_main_and_triage_sessions_independent() -> None:
    script = Path("scripts/setup-hermes-oauth.sh").read_text(encoding="utf-8")
    operator = Path("scripts/docket").read_text(encoding="utf-8")

    assert "auth logout openai-codex" in script
    assert "auth add openai-codex" in script
    assert "--type oauth" in script
    assert "--no-browser" in script
    assert "auth reset openai-codex" in script
    assert "-p docket-triage" in script
    assert "profiles/docket-triage/auth.json" in script
    assert "Restored the prior" in script
    assert "compose restart hermes" in script
    assert "setup-hermes-auth) hermes_oauth_setup" in operator
