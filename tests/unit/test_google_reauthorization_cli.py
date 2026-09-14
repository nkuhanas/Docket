import hashlib
import io
import json
import subprocess
from unittest.mock import Mock

import pytest

from docket import calendar_recovery_cli, google_oauth_cli
from docket.config import get_settings
from docket.providers.google.oauth import (
    GoogleOAuthSetupError,
    credential_fingerprint,
    resolve_scopes,
)


@pytest.mark.parametrize("remote", [False, True])
def test_successful_setup_recovers_only_after_the_credential_is_saved(
    tmp_path, monkeypatch, remote,
) -> None:
    token = tmp_path / "google_oauth_token.json"
    calls = []

    def setup(**kwargs):
        assert kwargs["manual_callback"] is remote
        token.write_text("new-test-credential")
        calls.append("saved")
        return resolve_scopes(["calendar"])

    def recover(path):
        assert path == token and path.read_text() == "new-test-credential"
        calls.append("recovered")
        return 0

    monkeypatch.setattr(google_oauth_cli, "perform_setup", setup)
    monkeypatch.setattr(google_oauth_cli, "_recover_deliveries", recover)
    arguments = ["setup", "--credentials-dir", str(tmp_path), "--force"]
    assert google_oauth_cli.main(arguments + (["--remote"] if remote else [])) == 0
    assert calls == ["saved", "recovered"]


@pytest.mark.parametrize("error", [GoogleOAuthSetupError("Consent denied"), OSError("secret")])
def test_failed_consent_or_credential_write_never_recovers(monkeypatch, capsys, error) -> None:
    monkeypatch.setattr(google_oauth_cli, "perform_setup", Mock(side_effect=error))
    recover = Mock()
    monkeypatch.setattr(google_oauth_cli, "_recover_deliveries", recover)
    assert google_oauth_cli.main(["setup"]) == 1
    recover.assert_not_called()
    assert "secret" not in capsys.readouterr().err


@pytest.mark.parametrize("arguments", [["--credentials-only"], ["--scope-profile", "gmail-read"]])
def test_offline_or_non_calendar_setup_does_not_retry(monkeypatch, arguments) -> None:
    monkeypatch.setattr(google_oauth_cli, "perform_setup", Mock(return_value=resolve_scopes(
        ["calendar"] if arguments == ["--credentials-only"] else ["gmail-read"],
    )))
    recover = Mock()
    monkeypatch.setattr(google_oauth_cli, "_recover_deliveries", recover)
    assert google_oauth_cli.main(["setup", *arguments]) == 0
    recover.assert_not_called()


def test_read_only_status_does_not_retry(monkeypatch) -> None:
    monkeypatch.setattr(google_oauth_cli, "validate_client_file", Mock())
    monkeypatch.setattr(
        google_oauth_cli, "authorized_user_file_status", Mock(return_value="configured"),
    )
    recover = Mock()
    monkeypatch.setattr(google_oauth_cli, "_recover_deliveries", recover)
    assert google_oauth_cli.main(["status"]) == 0
    recover.assert_not_called()


def test_recovery_resume_needs_no_new_consent(tmp_path, monkeypatch) -> None:
    setup = Mock()
    recover = Mock(return_value=0)
    monkeypatch.setattr(google_oauth_cli, "perform_setup", setup)
    monkeypatch.setattr(google_oauth_cli, "_recover_deliveries", recover)
    assert google_oauth_cli.main([
        "recover-deliveries", "--credentials-dir", str(tmp_path),
    ]) == 0
    setup.assert_not_called()
    recover.assert_called_once_with(tmp_path / "google_oauth_token.json")


def test_host_recovery_binds_runtime_credential_without_printing_it(
    tmp_path, monkeypatch, capsys,
) -> None:
    token = tmp_path / "test-token.json"
    token.write_text("test-secret")
    fingerprint = hashlib.sha256(b"test-secret").hexdigest()
    run = Mock(return_value=subprocess.CompletedProcess(
        [], 0, json.dumps({"ok": True, "recovery": {"requeued": 2, "delivery_state": "queued"}}),
        "unused-private-stderr",
    ))
    monkeypatch.setattr(google_oauth_cli.subprocess, "run", run)
    assert google_oauth_cli._recover_deliveries(token) == 0
    assert run.call_args.kwargs["input"] == fingerprint + "\n"
    assert "requeue-after-reauth" in run.call_args.args[0]
    assert fingerprint not in " ".join(run.call_args.args[0])
    assert run.call_args.kwargs["timeout"] == 60
    output = capsys.readouterr().out
    assert '"requeued": 2' in output and "not confirmation" in output
    assert "test-secret" not in output and fingerprint not in output
    assert "private-stderr" not in output


@pytest.mark.parametrize("outcome", [
    subprocess.CompletedProcess([], 1, '{"ok": false}', "private-database-url"),
    subprocess.CompletedProcess([], 0, "private-malformed-output", ""),
    subprocess.CompletedProcess([], 0, '{"ok": true, "recovery": null}', ""),
    subprocess.CompletedProcess([], 0, "x" * 16385, ""),
    subprocess.TimeoutExpired("private-command", 60, output="private-response"),
])
def test_recovery_failure_is_partial_setup_success_and_safe_to_resume(
    tmp_path, monkeypatch, capsys, outcome,
) -> None:
    token = tmp_path / "test-token.json"
    token.write_text("retained-new-credential")
    run = (
        Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=outcome)
    )
    monkeypatch.setattr(google_oauth_cli.subprocess, "run", run)
    assert google_oauth_cli._recover_deliveries(token) == 2
    assert token.read_text() == "retained-new-credential"
    output = capsys.readouterr()
    assert "remains saved" in output.err and "recover-deliveries" in output.err
    assert "Some deliveries may already be queued" in output.err
    assert "private-" not in output.out + output.err


def test_credential_fingerprint_rejects_missing_symlink_and_unbounded_files(tmp_path) -> None:
    target = tmp_path / "test-token.json"
    for data in (b"", b"x" * 65537):
        target.write_bytes(data)
        with pytest.raises(GoogleOAuthSetupError):
            credential_fingerprint(target)
    target.write_bytes(b"bounded")
    alias = tmp_path / "alias"
    alias.symlink_to(target)
    for path in (alias, tmp_path / "missing"):
        with pytest.raises(GoogleOAuthSetupError):
            credential_fingerprint(path)
    assert credential_fingerprint(target) == hashlib.sha256(b"bounded").hexdigest()


@pytest.mark.parametrize("case", ["matched", "mismatch", "disabled", "unavailable"])
def test_runtime_recovery_checks_matching_credential_and_real_write_gate(
    tmp_path, monkeypatch, capsys, case,
) -> None:
    token = tmp_path / "test-token.json"
    token.write_text("runtime-credential")
    settings = get_settings().model_copy(update={
        "google_oauth_token_file": token,
        "external_writes_enabled": case != "disabled",
    })
    monkeypatch.setattr(calendar_recovery_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(calendar_recovery_cli, "configure_database", Mock())
    runner = Mock()
    runner.requeue_after_reauthorization.return_value.projection.return_value = {"requeued": 1}
    if case == "unavailable":
        runner.requeue_after_reauthorization.side_effect = RuntimeError("private-db-url")
    monkeypatch.setattr(calendar_recovery_cli, "OperationRunner", Mock(return_value=runner))
    monkeypatch.setattr(calendar_recovery_cli, "get_session_factory", Mock())
    monkeypatch.setattr(calendar_recovery_cli, "build_calendar_write_provider", Mock())
    fingerprint = "a" * 64 if case == "mismatch" else credential_fingerprint(token)
    monkeypatch.setattr(calendar_recovery_cli.sys, "stdin", io.StringIO(fingerprint + "\n"))
    result = calendar_recovery_cli.main([
        "requeue-after-reauth", "--execute", "--credential-sha256-stdin",
    ])
    output = capsys.readouterr().out
    body = json.loads(output)
    assert result == (0 if case == "matched" else 1)
    assert "private-db-url" not in output and fingerprint not in output
    if case in {"matched", "unavailable"}:
        runner.requeue_after_reauthorization.assert_called_once_with(
            external_account_id=settings.google_account_external_id,
            credential_ref=str(token),
        )
    else:
        runner.requeue_after_reauthorization.assert_not_called()
        assert body["error"]["code"] == (
            "calendar_recovery_credential_mismatch" if case == "mismatch"
            else "calendar_recovery_writes_disabled"
        )
