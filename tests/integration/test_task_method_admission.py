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
