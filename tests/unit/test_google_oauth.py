import json
import stat
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from docket.google_oauth_cli import DEFAULT_REMOTE_CALLBACK_PORT
from docket.providers.google.oauth import (
    CALENDAR_EVENTS_SCOPE,
    CALENDAR_LIST_SCOPE,
    CALENDARS_SCOPE,
    DEFAULT_SCOPE_PROFILES,
    DOCS_SCOPE,
    GMAIL_MODIFY_SCOPE,
    GOOGLE_AUTHORIZATION_ENDPOINT,
    SHEETS_SCOPE,
    TASKS_READONLY_SCOPE,
    TASKS_SCOPE,
    GoogleOAuthSetupError,
    _default_flow_factory,
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


@pytest.mark.parametrize("endpoint", [
    "https://accounts.google.com/o/oauth2/auth", GOOGLE_AUTHORIZATION_ENDPOINT,
])
def test_real_library_uses_v2_endpoint_and_single_oauth_parameters(tmp_path, endpoint) -> None:
    client_file = tmp_path / "client.json"
    _write_client(client_file)
    document = json.loads(client_file.read_text())
    document["installed"]["auth_uri"] = endpoint
    original = json.dumps(document)
    client_file.write_text(original)
    validate_client_file(client_file)
    flow = _default_flow_factory(client_file, [TASKS_SCOPE, CALENDAR_EVENTS_SCOPE])
    flow.redirect_uri = "http://127.0.0.1:8765/"
    url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="false", prompt="consent",
    )
    parsed = urlsplit(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == GOOGLE_AUTHORIZATION_ENDPOINT
    query = parse_qs(parsed.query)
    assert all(len(values) == 1 for values in query.values())
    assert query["access_type"] == ["offline"]
    assert query["state"] == [state]
    assert query["code_challenge_method"] == ["S256"]
    assert set(query["scope"][0].split()) == {TASKS_SCOPE, CALENDAR_EVENTS_SCOPE}
    assert client_file.read_text() == original


@pytest.mark.parametrize("endpoint", [
    "https://accounts.google.com/o/oauth2/v2/auth?access_type=online",
    "https://accounts.google.com.example/o/oauth2/v2/auth",
])
def test_setup_rejects_unexpected_authorization_endpoint(tmp_path, endpoint) -> None:
    client_file = tmp_path / "client.json"
    _write_client(client_file)
    document = json.loads(client_file.read_text())
    document["installed"]["auth_uri"] = endpoint
    client_file.write_text(json.dumps(document))
    with pytest.raises(GoogleOAuthSetupError, match="unexpected authorization endpoint"):
        validate_client_file(client_file)


class FakeFlow:
    def __init__(self, calls: dict[str, Any], credentials: FakeCredentials | None = None) -> None:
        self.calls = calls
        self.credentials = credentials or FakeCredentials()
        self.redirect_uri = ""

    def run_local_server(self, **kwargs: Any) -> FakeCredentials:
        self.calls.update(kwargs)
        return self.credentials

    def authorization_url(self, **kwargs: Any) -> tuple[str, str]:
        self.calls["authorization_options"] = kwargs
        return "https://accounts.google.test/authorize", "expected-state"

    def fetch_token(self, **kwargs: Any) -> None:
        self.calls["fetch_token"] = kwargs


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
    assert scopes == (CALENDAR_LIST_SCOPE, CALENDARS_SCOPE, CALENDAR_EVENTS_SCOPE)
    assert document["refresh_token"] == "refresh-secret"
    assert document["scopes"] == [
        CALENDAR_LIST_SCOPE,
        CALENDARS_SCOPE,
        CALENDAR_EVENTS_SCOPE,
    ]
    assert "token" not in document
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_file.parent.stat().st_mode) == 0o700
    assert authorized_user_file_status(token_file) == "configured"
    assert calls["access_type"] == "offline"
    assert calls["include_granted_scopes"] == "false"
    assert calls["prompt"] == "consent"
    assert calls["open_browser"] is False
    assert calls["host"] == "127.0.0.1"


def test_remote_setup_exchanges_validated_manual_callback(tmp_path) -> None:
    client_file = tmp_path / "client.json"
    token_file = tmp_path / "token.json"
    _write_client(client_file)
    calls: dict[str, Any] = {}
    displayed: list[str] = []

    perform_setup(
        client_file=client_file,
        token_file=token_file,
        profiles=["workspace"],
        open_browser=False,
        port=DEFAULT_REMOTE_CALLBACK_PORT,
        timeout_seconds=60,
        force=False,
        manual_callback=True,
        authorization_url_handler=displayed.append,
        callback_url_reader=lambda: (
            "http://127.0.0.1:8765/?state=expected-state&code=one-time-code"
        ),
        flow_factory=lambda _client, _scopes: FakeFlow(calls),
    )

    assert displayed == ["https://accounts.google.test/authorize"]
    assert calls["fetch_token"] == {
        "authorization_response": (
            "https://127.0.0.1:8765/?state=expected-state&code=one-time-code"
        )
    }
    assert authorized_user_file_status(token_file) == "configured"


def test_remote_setup_rejects_callback_from_another_run(tmp_path) -> None:
    client_file = tmp_path / "client.json"
    token_file = tmp_path / "token.json"
    _write_client(client_file)

    with pytest.raises(GoogleOAuthSetupError, match="state does not match"):
        perform_setup(
            client_file=client_file,
            token_file=token_file,
            profiles=["workspace"],
            open_browser=False,
            port=DEFAULT_REMOTE_CALLBACK_PORT,
            timeout_seconds=60,
            force=False,
            manual_callback=True,
            authorization_url_handler=lambda _url: None,
            callback_url_reader=lambda: (
                "http://127.0.0.1:8765/?state=wrong-state&code=one-time-code"
            ),
            flow_factory=lambda _client, _scopes: FakeFlow({}),
        )


def test_setup_rejects_non_loopback_callback_host(tmp_path) -> None:
    client_file = tmp_path / "client.json"
    token_file = tmp_path / "token.json"
    _write_client(client_file)

    with pytest.raises(GoogleOAuthSetupError, match="loopback-only"):
        perform_setup(
            client_file=client_file,
            token_file=token_file,
            profiles=["calendar"],
            open_browser=False,
            port=8765,
            timeout_seconds=60,
            force=False,
            callback_host="0.0.0.0",
            flow_factory=lambda _client, _scopes: FakeFlow({}),
        )


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
        CALENDAR_LIST_SCOPE,
        CALENDARS_SCOPE,
        CALENDAR_EVENTS_SCOPE,
        GMAIL_MODIFY_SCOPE,
    )


def test_default_workspace_profile_has_explicit_approved_scopes() -> None:
    assert resolve_scopes(DEFAULT_SCOPE_PROFILES) == (
        CALENDAR_LIST_SCOPE,
        CALENDARS_SCOPE,
        CALENDAR_EVENTS_SCOPE,
        DOCS_SCOPE,
        GMAIL_MODIFY_SCOPE,
        SHEETS_SCOPE,
    )


def test_token_status_fails_closed_when_required_scope_is_absent(tmp_path) -> None:
    token_file = tmp_path / "token.json"
    token_file.write_text(
        json.dumps(
            {
                "refresh_token": "refresh-secret",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "dummy-client.apps.googleusercontent.com",
                "client_secret": "dummy-client-secret",
                "scopes": [CALENDAR_EVENTS_SCOPE],
            }
        ),
        encoding="utf-8",
    )

    assert authorized_user_file_status(token_file, required_scopes=[CALENDARS_SCOPE]) == "invalid"


def test_client_validation_requires_desktop_credentials(tmp_path) -> None:
    client_file = tmp_path / "client.json"
    _write_client(client_file, client_type="web")
    with pytest.raises(GoogleOAuthSetupError, match="Desktop app"):
        validate_client_file(client_file)


def test_missing_and_dummy_token_status(tmp_path) -> None:
    assert authorized_user_file_status(tmp_path / "missing.json") == "setup_required"
    assert authorized_user_file_status(Path("secrets/smoke/google_oauth_token.json")) == "dummy"


def test_tasks_scope_upgrade_preserves_calendar_and_requires_confirmed_grants(tmp_path) -> None:
    client_file = tmp_path / "client.json"
    token_file = tmp_path / "credentials" / "token.json"
    _write_client(client_file)
    options: dict[str, Any] = dict(
        client_file=client_file,
        token_file=token_file,
        open_browser=False,
        port=0,
        timeout_seconds=60,
        force=True,
    )
    perform_setup(**options, profiles=["calendar"], flow_factory=lambda *_: FakeFlow({}))
    original = token_file.read_bytes()
    requested: list[str] = []

    def denied(_client: Path, scopes: Any) -> FakeFlow:
        requested.extend(scopes)
        credentials = FakeCredentials()
        credentials.granted_scopes = [TASKS_SCOPE]  # type: ignore[attr-defined]
        return FakeFlow({}, credentials)

    with pytest.raises(GoogleOAuthSetupError, match="existing credentials were preserved"):
        perform_setup(**options, profiles=["tasks"], flow_factory=denied)
    assert token_file.read_bytes() == original
    assert set(resolve_scopes(["calendar", "tasks"])) == set(requested)

    def accepted(_client: Path, scopes: Any) -> FakeFlow:
        credentials = FakeCredentials()
        credentials.granted_scopes = list(scopes)  # type: ignore[attr-defined]
        return FakeFlow({}, credentials)

    perform_setup(**options, profiles=["tasks"], flow_factory=accepted)
    assert (
        authorized_user_file_status(
            token_file, required_scopes=resolve_scopes(["calendar", "tasks"])
        )
        == "configured"
    )
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_tasks_scope_profiles_do_not_expand_default_workspace() -> None:
    assert resolve_scopes(["tasks-read"]) == (TASKS_READONLY_SCOPE,)
    assert resolve_scopes(["tasks", "tasks-read"]) == (TASKS_SCOPE,)
    assert TASKS_SCOPE not in resolve_scopes(DEFAULT_SCOPE_PROFILES)
