"""Method admission follows retained real evidence and the current credential grant."""

import json
from datetime import UTC, datetime, timedelta

from docket.config import get_settings
from docket.models import ProviderAccount, TasksMethodValidation
from docket.providers.google.oauth import TASKS_SCOPE
from docket.providers.google.tasks import credential_fingerprint
from docket.services.tasks_method_admission import ADAPTER_VERSION, TasksMethodAdmission


def setup_admission(session, tmp_path):
    path = tmp_path / "synthetic-user.json"
    path.write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
                "client_id": "synthetic-client",
                "client_secret": "synthetic-secret",
                "token_uri": "https://oauth2.googleapis.com/token",
                "scopes": [TASKS_SCOPE],
            }
        )
    )
    settings = get_settings().model_copy(
        update={
            "tasks_enabled": True,
            "google_oauth_token_file": path,
            "google_account_external_id": "synthetic-tasks",
        }
    )
    account = ProviderAccount(
        provider="google",
        external_account_id="synthetic-tasks",
        capabilities=["google_tasks"],
    )
    session.add(account)
    session.flush()
    return TasksMethodAdmission(session, settings), account


def evidence(service, account, method, *, passed=True, capabilities=None, later=0):
    service.session.add(
        TasksMethodValidation(
            account_id=account.id,
            method=method,
            adapter_version=ADAPTER_VERSION,
            credential_fingerprint=credential_fingerprint(service.settings.google_oauth_token_file),
            passed=passed,
            evidence_sha256="0" * 64,
            capabilities=capabilities or {},
            verified_at=datetime.now(UTC) + timedelta(seconds=later),
        )
    )
    service.session.flush()


def test_no_evidence_or_header_alone_does_not_admit_method(session, tmp_path):
    service, account = setup_admission(session, tmp_path)
    assert not service.gate(account).methods
    evidence(service, account, "patch", capabilities={"header_sent": True})
    assert not service.gate(account).methods
    evidence(service, account, "patch", capabilities={"conditional_write": True}, later=1)
    assert service.gate(account).methods == {"patch"}


def test_new_negative_result_revokes_older_success(session, tmp_path):
    service, account = setup_admission(session, tmp_path)
    evidence(service, account, "delete", capabilities={"conditional_write": True})
    assert service.gate(account).methods == {"delete"}
    evidence(service, account, "delete", passed=False, later=1)
    assert not service.gate(account).methods


def test_grant_rotation_invalidates_admission_but_access_refresh_does_not(session, tmp_path):
    service, account = setup_admission(session, tmp_path)
    evidence(service, account, "patch", capabilities={"conditional_write": True})
    path = service.settings.google_oauth_token_file
    original = json.loads(path.read_text())
    path.write_text(json.dumps({**original, "token": "refreshed-synthetic-access"}))
    assert service.gate(account).methods == {"patch"}
    path.write_text(json.dumps({**original, "refresh_token": "new-synthetic-grant"}))
    assert not service.gate(account).methods


def test_account_disable_and_feature_switch_revoke_admission(session, tmp_path):
    service, account = setup_admission(session, tmp_path)
    evidence(service, account, "patch", capabilities={"conditional_write": True})
    account.enabled = False
    assert not service.gate(account).methods
    account.enabled = True
    service.settings.tasks_enabled = False
    assert not service.gate(account).methods


def test_move_requires_stable_identity_and_insert_requires_uncertainty_proof(session, tmp_path):
    service, account = setup_admission(session, tmp_path)
    evidence(service, account, "move", capabilities={"conditional_write": True})
    evidence(service, account, "insert", capabilities={"conditional_write": True})
    assert not service.gate(account).methods
    evidence(
        service,
        account,
        "move",
        capabilities={
            "conditional_write": True,
            "stable_move_identity": True,
        },
        later=1,
    )
    evidence(service, account, "insert", capabilities={"uncertain_insert_recovery": True}, later=1)
    assert service.gate(account).methods == {"insert", "move"}


def test_local_validation_cannot_record_success_for_a_rotated_grant(
    session_factory,
    tmp_path,
    monkeypatch,
    capsys,
):
    from sqlalchemy import func, select

    from docket import tasks_cli
    from docket.providers.google.fake_tasks import FakeTasksProvider
    from docket.services.tasks_validation import validate_tasks_methods

    with session_factory.begin() as session:
        service, _account = setup_admission(session, tmp_path)
        settings = service.settings
    targets = tmp_path / "validation-targets.json"
    targets.write_text(json.dumps({"source_list": "source", "destination_list": "destination"}))
    targets.chmod(0o600)
    provider = FakeTasksProvider()
    provider.seed_list("source", "Docket validation")
    provider.seed_list("destination", "Docket validation move target")
    monkeypatch.setattr(tasks_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks_cli, "configure_database", lambda _url: None)
    monkeypatch.setattr(tasks_cli, "get_session_factory", lambda: session_factory)
    monkeypatch.setattr(tasks_cli, "GoogleTasksProvider", lambda *_args, **_kwargs: provider)

    def rotate_after_probe(adapter, source, destination):
        report = validate_tasks_methods(adapter, source, destination)
        original = json.loads(settings.google_oauth_token_file.read_text())
        settings.google_oauth_token_file.write_text(
            json.dumps(
                {
                    **original,
                    "refresh_token": "rotated-synthetic-grant",
                }
            )
        )
        return report

    monkeypatch.setattr(tasks_cli, "validate_tasks_methods", rotate_after_probe)
    assert tasks_cli.main(["validate", "--targets-file", str(targets)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "tasks_validation_grant_changed"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(TasksMethodValidation)) == 0
