from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol, cast

CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
DOCS_SCOPE = "https://www.googleapis.com/auth/documents"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

SCOPE_PROFILES: dict[str, tuple[str, ...]] = {
    "calendar": (CALENDAR_EVENTS_SCOPE,),
    "docs": (DOCS_SCOPE,),
    "gmail-read": (GMAIL_READONLY_SCOPE,),
    "gmail-modify": (GMAIL_MODIFY_SCOPE,),
    "sheets": (SHEETS_SCOPE,),
    "workspace": (
        CALENDAR_EVENTS_SCOPE,
        DOCS_SCOPE,
        GMAIL_MODIFY_SCOPE,
        SHEETS_SCOPE,
    ),
}
DEFAULT_SCOPE_PROFILES = ("workspace",)

GoogleOAuthStatus = Literal["setup_required", "dummy", "configured", "invalid"]


class GoogleOAuthSetupError(RuntimeError):
    """A safe-to-display Google OAuth setup error."""


class OAuthCredentials(Protocol):
    refresh_token: str | None

    def to_json(self, strip: Sequence[str] | None = None) -> str: ...


class OAuthFlow(Protocol):
    def run_local_server(self, **kwargs: Any) -> OAuthCredentials: ...


FlowFactory = Callable[[Path, Sequence[str]], OAuthFlow]


def resolve_scopes(profiles: Sequence[str]) -> tuple[str, ...]:
    unknown = sorted(set(profiles) - SCOPE_PROFILES.keys())
    if unknown:
        raise GoogleOAuthSetupError(f"Unknown Google OAuth scope profile: {', '.join(unknown)}")

    scopes = {scope for profile in profiles for scope in SCOPE_PROFILES[profile]}
    if GMAIL_MODIFY_SCOPE in scopes:
        scopes.discard(GMAIL_READONLY_SCOPE)
    if not scopes:
        raise GoogleOAuthSetupError("At least one Google OAuth scope profile is required")
    return tuple(sorted(scopes))


def validate_client_file(path: Path) -> None:
    if not path.is_file():
        raise GoogleOAuthSetupError(f"Google OAuth client file not found: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoogleOAuthSetupError(f"Google OAuth client file is not valid JSON: {path}") from exc

    installed = document.get("installed") if isinstance(document, dict) else None
    if not isinstance(installed, dict):
        raise GoogleOAuthSetupError(
            "Google OAuth client must be a Desktop app credential with an 'installed' section"
        )

    required = {"client_id", "client_secret", "auth_uri", "token_uri"}
    missing = sorted(name for name in required if not installed.get(name))
    if missing:
        raise GoogleOAuthSetupError(
            f"Google OAuth client is missing required fields: {', '.join(missing)}"
        )
    if installed["auth_uri"] != "https://accounts.google.com/o/oauth2/auth":
        raise GoogleOAuthSetupError("Google OAuth client has an unexpected authorization endpoint")
    if installed["token_uri"] != "https://oauth2.googleapis.com/token":
        raise GoogleOAuthSetupError("Google OAuth client has an unexpected token endpoint")


def authorized_user_file_status(path: Path) -> GoogleOAuthStatus:
    if not path.exists():
        return "setup_required"
    if not path.is_file() or path.is_symlink():
        return "invalid"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid"
    if not isinstance(document, dict):
        return "invalid"

    refresh_token = document.get("refresh_token")
    token_uri = document.get("token_uri")
    if isinstance(refresh_token, str) and refresh_token.casefold().startswith("dummy"):
        return "dummy"
    required = ("refresh_token", "token_uri", "client_id", "client_secret", "scopes")
    if not all(document.get(name) for name in required):
        return "invalid"
    if token_uri != "https://oauth2.googleapis.com/token":
        return "invalid"
    return "configured"


def _default_flow_factory(client_file: Path, scopes: Sequence[str]) -> OAuthFlow:
    from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

    return cast(
        OAuthFlow,
        InstalledAppFlow.from_client_secrets_file(str(client_file), scopes=list(scopes)),
    )


def _write_credentials(path: Path, credentials: OAuthCredentials, scopes: Sequence[str]) -> None:
    if not credentials.refresh_token:
        raise GoogleOAuthSetupError(
            "Google did not return a refresh token; revoke the prior grant and retry setup"
        )
    try:
        document = json.loads(credentials.to_json(strip=["token"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise GoogleOAuthSetupError(
            "Google returned credentials that could not be serialized"
        ) from exc
    if not isinstance(document, dict):
        raise GoogleOAuthSetupError("Google returned an unexpected credential document")

    document.pop("token", None)
    document["scopes"] = list(scopes)
    serialized = json.dumps(document, indent=2, sort_keys=True) + "\n"

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if path.is_symlink():
        raise GoogleOAuthSetupError(f"Refusing to replace symlinked token file: {path}")

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def perform_setup(
    *,
    client_file: Path,
    token_file: Path,
    profiles: Sequence[str],
    open_browser: bool,
    port: int,
    timeout_seconds: int,
    force: bool,
    flow_factory: FlowFactory = _default_flow_factory,
) -> tuple[str, ...]:
    validate_client_file(client_file)
    resolved_parent = token_file.resolve().parent
    if resolved_parent.name == "smoke" and resolved_parent.parent.name == "secrets":
        raise GoogleOAuthSetupError("Refusing to write real credentials into secrets/smoke")
    token_status = authorized_user_file_status(token_file)
    if token_file.exists() and not force and token_status != "dummy":
        raise GoogleOAuthSetupError(
            f"Token file already exists: {token_file}; pass --force to reauthorize"
        )
    if port < 0 or port > 65535:
        raise GoogleOAuthSetupError("OAuth callback port must be between 0 and 65535")

    scopes = resolve_scopes(profiles)
    flow = flow_factory(client_file, scopes)
    credentials = flow.run_local_server(
        host="localhost",
        port=port,
        open_browser=open_browser,
        access_type="offline",
        include_granted_scopes="false",
        prompt="consent",
        timeout_seconds=timeout_seconds,
        authorization_prompt_message="Open this URL to authorize Docket:\n{url}",
        success_message="Docket authorization completed. You may close this window.",
    )
    _write_credentials(token_file, credentials, scopes)
    return scopes
