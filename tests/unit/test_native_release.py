import os
import subprocess
from pathlib import Path

import pytest

from docket.config import get_settings
from docket.ingress_credentials import main


def test_native_startup_does_not_provision_or_open_discord_credentials(monkeypatch):
    monkeypatch.setattr(
        "docket.ingress_credentials.get_settings",
        lambda: get_settings().model_copy(
            update={"discord_adapter_enabled": False},
        ),
    )
    monkeypatch.delenv("DOCKET_INGRESS_DB_PASSWORD_FILE", raising=False)
    monkeypatch.setattr(
        "docket.ingress_credentials.provision_ingress_role",
        lambda *args: pytest.fail("native startup provisioned Discord"),
    )
    main()


def definitions():
    return Path("scripts/docket").read_text().split('\ncase "${1:-}" in', 1)[0]


@pytest.mark.parametrize(
    "enabled,gateway,backup_fails",
    [
        ("true", "true", False),
        ("false", "true", False),
        ("false", "false", False),
        ("false", "true", True),
    ],
)
def test_deployment_mode_preserves_drain_backup_and_selected_adapter(
    tmp_path,
    enabled,
    gateway,
    backup_fails,
):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    prepare = scripts / "prepare-hermes-home.sh"
    prepare.write_text('#!/bin/bash\nprintf "prepare-hermes\\n" >> "$CALL_LOG"\n')
    prepare.chmod(0o700)
    log = tmp_path / "calls"
    harness = scripts / "harness"
    harness.write_text(
        definitions()
        + r"""
log() { printf '%s\n' "$*" >> "$CALL_LOG"; }
env_value() { printf '%s' "$TEST_ADAPTER"; }
predeploy() { log predeploy; }
ensure_ingress_credential() { log ingress-credential; }
ensure_attachment_encryption_key() { log encryption-key; }
git() { printf 'synthetic-revision\n'; }
docker_engine() { log "docker $*"; }
build_image() { log build; }
compose() {
    log "compose $*"
    if [[ "$*" == 'ps -q docket' || "$*" == 'ps -q discord-ingress' ||
          ( "$*" == 'ps -q hermes' && "$TEST_GATEWAY" == true ) ]]; then
        printf 'existing-container\n'
    fi
}
request_database_drain() { log db-drain; printf 'drain_synthetic\n'; }
request_hermes_drain() { log hermes-drain; }
wait_for_database_drain() { log db-wait; }
wait_for_hermes_drain() { log hermes-wait; }
wait_for_operational_idle() { log idle; }
wait_for_discord_ingress() { log ingress-wait; }
backup_database() {
    log backup
    if [[ "$TEST_BACKUP_FAILS" == true ]]; then return 1; fi
    printf 'synthetic-backup\n'
}
python3() { log quarantine; }
database_continuity() {
    log "continuity $*"
    if [[ "$*" == quiesce-ingress-options ]]; then
        printf 'legacy projection listener unavailable\n' >&2
        return 1
    fi
}
wait_for_hermes() { log hermes-ready; }
gmail_triage_setup() { log triage-setup; }
postdeploy() { log postdeploy; }
clear_hermes_drain() { log hermes-clear; }
deploy
"""
    )
    result = subprocess.run(
        ["bash", str(harness)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CALL_LOG": str(log),
            "TEST_ADAPTER": enabled,
            "TEST_GATEWAY": gateway,
            "TEST_BACKUP_FAILS": str(backup_fails).lower(),
        },
    )
    calls = log.read_text().splitlines()
    if backup_fails:
        assert result.returncode != 0
        assert any(call.startswith("continuity abort") for call in calls)
        assert "compose stop -t 30 discord-ingress hermes searxng" not in calls
        assert not any("--force-recreate" in call for call in calls)
        return
    assert result.returncode == 0, result.stderr
    assert calls.index("db-drain") < calls.index("db-wait") < calls.index("backup")
    assert calls.index("backup") < calls.index("postdeploy")
    release = next(i for i, call in enumerate(calls) if call.startswith("continuity release"))
    assert calls.index("postdeploy") < release
    if enabled == "false":
        stop = calls.index("compose stop -t 30 discord-ingress hermes searxng")
        start = calls.index("compose up -d --no-build --force-recreate docket")
        assert calls.index("backup") < stop < start < calls.index("postdeploy")
        assert "continuity quiesce-ingress-options" not in calls
        assert "prepare-hermes" not in calls and "triage-setup" not in calls
        assert not any(call.endswith("--force-recreate docket hermes") for call in calls)
    else:
        assert "prepare-hermes" in calls and "triage-setup" in calls
        assert "compose up -d --no-build --force-recreate docket hermes" in calls
        assert "compose stop -t 30 discord-ingress hermes searxng" not in calls


@pytest.mark.parametrize("value,expected", [("", "yes"), ("true", "yes"), ("'false'", "no")])
def test_legacy_default_and_explicit_native_boolean(tmp_path, value, expected):
    script = tmp_path / "mode"
    script.write_text(
        definitions()
        + """
env_value() { printf '%s' "$TEST_ADAPTER"; }
if discord_adapter_enabled; then printf yes; else printf no; fi
"""
    )
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "TEST_ADAPTER": value,
        },
    )
    assert result.returncode == 0 and result.stdout == expected


def test_missing_shared_key_cannot_replace_retained_encrypted_callbacks(tmp_path):
    script = tmp_path / "missing-key"
    script.write_text(
        definitions()
        + r"""
env_value() { printf '%s' "$TEST_KEYS"; }
compose() {
    case "$*" in
        'ps -q postgres') printf container ;;
        *encrypted_attachment_blobs*) printf f ;;
        *mcp_event_subscriptions*'octet_length'*) printf 1 ;;
        *mcp_event_subscriptions*) printf t ;;
    esac
}
ensure_attachment_encryption_key
"""
    )
    keys = tmp_path / "keys"
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "TEST_KEYS": str(keys),
        },
    )
    assert result.returncode != 0 and "callbacks exist" in result.stderr
    assert not keys.exists()


@pytest.mark.parametrize("table_exists,expected", [("t", "1 5"), ("f", "1 3")])
def test_drain_counts_native_deliveries_and_accepts_pre_migration_database(
    tmp_path,
    table_exists,
    expected,
):
    script = tmp_path / "counts"
    script.write_text(
        definitions()
        + r"""
compose() {
    case "$*" in
        *to_regclass*) printf '%s' "$TEST_TABLE_EXISTS" ;;
        *mcp_event_deliveries*) printf 2 ;;
        *) printf '1 3' ;;
    esac
}
operational_counts
"""
    )
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "TEST_TABLE_EXISTS": table_exists,
        },
    )
    assert result.returncode == 0 and result.stdout.strip() == expected
