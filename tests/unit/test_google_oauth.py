import json
import stat
from pathlib import Path
from typing import Any

import pytest

from docket.providers.google.oauth import (
    CALENDAR_EVENTS_SCOPE,
    DEFAULT_SCOPE_PROFILES,
    DOCS_SCOPE,
    GMAIL_MODIFY_SCOPE,
    SHEETS_SCOPE,
    GoogleOAuthSetupError,
    authorized_user_file_status,
    perform_setup,
    resolve_scopes,
    validate_client_file,
)


def _write_client(path: Path, *, client_type: str = "installed") -> None:
    path.write_text(
        json.dumps(
            {
                client_type: {
                    "client_id": "dummy-client.apps.googleusercontent.com",
                    "client_secret": "dummy-client-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
        ),
        encoding="utf-8",
    )


class FakeCredentials:
    refresh_token = "refresh-secret"

    def to_json(self, strip: list[str] | None = None) -> str:
        assert strip == ["token"]
        return json.dumps(
            {
                "token": "short-lived-access-token",
                "refresh_token": self.refresh_token,
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "dummy-client.apps.googleusercontent.com",
                "client_secret": "dummy-client-secret",
            }
        )


class FakeFlow:
    def __init__(self, calls: dict[str, Any], credentials: FakeCredentials | None = None) -> None:
        self.calls = calls
        self.credentials = credentials or FakeCredentials()

    def run_local_server(self, **kwargs: Any) -> FakeCredentials:
        self.calls.update(kwargs)
        return self.credentials


def test_setup_generates_refresh_token_file_atomically(tmp_path) -> None:
    client_file = tmp_path / "client.json"
    token_file = tmp_path / "credentials" / "token.json"
    _write_client(client_file)
    calls: dict[str, Any] = {}

    scopes = perform_setup(
        client_file=client_file,
        token_file=token_file,
        profiles=["calendar"],
        open_browser=False,
        port=0,
        timeout_seconds=60,
        force=False,
        flow_factory=lambda _client, _scopes: FakeFlow(calls),
    )

    document = json.loads(token_file.read_text(encoding="utf-8"))
    assert scopes == (CALENDAR_EVENTS_SCOPE,)
    assert document["refresh_token"] == "refresh-secret"
    assert document["scopes"] == [CALENDAR_EVENTS_SCOPE]
    assert "token" not in document
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_file.parent.stat().st_mode) == 0o700
    assert authorized_user_file_status(token_file) == "configured"
    assert calls["access_type"] == "offline"
    assert calls["include_granted_scopes"] == "false"
    assert calls["prompt"] == "consent"
    assert calls["open_browser"] is False


def test_setup_refuses_to_replace_existing_token_without_force(tmp_path) -> None:
    client_file = tmp_path / "client.json"
    token_file = tmp_path / "token.json"
    _write_client(client_file)
    token_file.write_text("existing", encoding="utf-8")

    with pytest.raises(GoogleOAuthSetupError, match="already exists"):
        perform_setup(
            client_file=client_file,
            token_file=token_file,
            profiles=["calendar"],
            open_browser=False,
            port=0,
            timeout_seconds=60,
            force=False,
            flow_factory=lambda _client, _scopes: FakeFlow({}),
        )
    assert token_file.read_text(encoding="utf-8") == "existing"


def test_setup_replaces_recognized_dummy_token_without_force(tmp_path) -> None:
    client_file = tmp_path / "client.json"
    token_file = tmp_path / "token.json"
    _write_client(client_file)
    token_file.write_text(
        json.dumps(
            {
                "refresh_token": "dummy-refresh-token",
                "token_uri": "https://oauth2.invalid/token",
            }
        ),
        encoding="utf-8",
    )

    perform_setup(
        client_file=client_file,
        token_file=token_file,
        profiles=["calendar"],
        open_browser=False,
        port=0,
        timeout_seconds=60,
        force=False,
        flow_factory=lambda _client, _scopes: FakeFlow({}),
    )

    assert authorized_user_file_status(token_file) == "configured"


def test_setup_requires_refresh_token_and_leaves_no_file(tmp_path) -> None:
    client_file = tmp_path / "client.json"
    token_file = tmp_path / "token.json"
    _write_client(client_file)
    credentials = FakeCredentials()
    credentials.refresh_token = ""

    with pytest.raises(GoogleOAuthSetupError, match="did not return a refresh token"):
        perform_setup(
            client_file=client_file,
            token_file=token_file,
            profiles=["calendar"],
            open_browser=False,
            port=0,
            timeout_seconds=60,
            force=False,
            flow_factory=lambda _client, _scopes: FakeFlow({}, credentials),
        )
    assert not token_file.exists()


def test_setup_refuses_checked_in_smoke_directory(tmp_path) -> None:
    client_file = tmp_path / "client.json"
    token_file = tmp_path / "secrets" / "smoke" / "google_oauth_token.json"
    _write_client(client_file)

    with pytest.raises(GoogleOAuthSetupError, match="secrets/smoke"):
        perform_setup(
            client_file=client_file,
            token_file=token_file,
            profiles=["calendar"],
            open_browser=False,
            port=0,
            timeout_seconds=60,
            force=False,
            flow_factory=lambda _client, _scopes: FakeFlow({}),
        )
    assert not token_file.exists()


def test_scope_profiles_drop_redundant_gmail_read_scope() -> None:
    assert resolve_scopes(["calendar", "gmail-read", "gmail-modify"]) == (
        CALENDAR_EVENTS_SCOPE,
        GMAIL_MODIFY_SCOPE,
    )


def test_default_workspace_profile_has_explicit_approved_scopes() -> None:
    assert resolve_scopes(DEFAULT_SCOPE_PROFILES) == (
        CALENDAR_EVENTS_SCOPE,
        DOCS_SCOPE,
        GMAIL_MODIFY_SCOPE,
        SHEETS_SCOPE,
    )


def test_client_validation_requires_desktop_credentials(tmp_path) -> None:
    client_file = tmp_path / "client.json"
    _write_client(client_file, client_type="web")
    with pytest.raises(GoogleOAuthSetupError, match="Desktop app"):
        validate_client_file(client_file)


def test_missing_and_dummy_token_status(tmp_path) -> None:
    assert authorized_user_file_status(tmp_path / "missing.json") == "setup_required"
    assert authorized_user_file_status(Path("secrets/smoke/google_oauth_token.json")) == "dummy"
