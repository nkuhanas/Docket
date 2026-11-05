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


@pytest.mark.parametrize("failure", ["none", "drain", "backup", "migration", "health"])
def test_native_deploy_preserves_backup_barrier_and_tested_artifact(tmp_path, failure):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    log = tmp_path / "calls"
    harness = scripts / "harness"
    harness.write_text(
        definitions()
        + r"""
log() { printf '%s\n' "$*" >> "$CALL_LOG"; }
predeploy() { log validated-image; export DOCKET_IMAGE=sha256:tested; }
ensure_attachment_encryption_key() { log encryption-key; }
git() { printf 'synthetic-revision\n'; }
docker_engine() { log "docker $*"; }
compose() {
    log "compose $* image=$DOCKET_IMAGE"
    if [[ "$*" == 'ps -q docket' ]]; then printf existing; fi
    if [[ "$*" == *'alembic upgrade'* && "$TEST_FAILURE" == migration ]]; then return 1; fi
}
request_database_drain() { log db-drain; printf 'drain_synthetic\n'; }
wait_for_database_drain() { log db-wait; [[ "$TEST_FAILURE" != drain ]]; }
wait_for_operational_idle() { log idle; }
backup_database() {
    log backup
    [[ "$TEST_FAILURE" != backup ]] || return 1
    printf 'synthetic-backup\n'
}
retire_gateway_containers() { log retire-gateway; }
database_continuity() { log "continuity $*"; }
postdeploy() { log health; [[ "$TEST_FAILURE" != health ]]; }
deploy
"""
    )
    result = subprocess.run(
        ["bash", str(harness)],
        capture_output=True,
        text=True,
        env={**os.environ, "CALL_LOG": str(log), "TEST_FAILURE": failure},
    )
    calls = log.read_text().splitlines()
    assert calls.index("validated-image") < calls.index("db-drain") < calls.index("db-wait")
    recreates = [c for c in calls if "--force-recreate" in c]
    if failure != "none":
        assert result.returncode != 0
        assert "continuity abort drain_synthetic" in calls
        assert "continuity release drain_synthetic" not in calls
        if failure in {"drain", "backup", "migration"}:
            assert not recreates
        return
    assert result.returncode == 0, result.stderr
    assert calls.index("db-wait") < calls.index("idle") < calls.index("backup")
    assert calls.index("backup") < calls.index("retire-gateway") < calls.index("health")
    assert calls.index("health") < calls.index("continuity release drain_synthetic")
    assert recreates == ["compose up -d --no-build --force-recreate docket image=sha256:tested"]
    assert not any(c == "build" or "--build " in c for c in calls)


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
