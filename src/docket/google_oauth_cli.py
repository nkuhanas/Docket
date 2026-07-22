from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from docket.providers.google.oauth import (
    DEFAULT_SCOPE_PROFILES,
    SCOPE_PROFILES,
    GoogleOAuthSetupError,
    authorized_user_file_status,
    perform_setup,
    validate_client_file,
)


def _credentials_dir() -> Path:
    return Path(os.environ.get("DOCKET_CREDENTIALS_DIR", "secrets/local"))


def _add_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--credentials-dir", type=Path, default=_credentials_dir())
    parser.add_argument("--client-file", type=Path)
    parser.add_argument("--token-file", type=Path)


def _paths(arguments: argparse.Namespace) -> tuple[Path, Path]:
    credentials_dir: Path = arguments.credentials_dir
    client_file: Path = arguments.client_file or credentials_dir / "google_oauth_client.json"
    token_file: Path = arguments.token_file or credentials_dir / "google_oauth_token.json"
    return client_file, token_file


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docket-google-auth",
        description="Create and inspect Docket-owned Google OAuth credentials.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser("setup", help="Run Google's installed-app consent flow")
    _add_paths(setup)
    setup.add_argument(
        "--scope-profile",
        action="append",
        choices=sorted(SCOPE_PROFILES),
        help="Repeat to request multiple profiles; defaults to the approved workspace bundle",
    )
    setup.add_argument("--no-browser", action="store_true")
    setup.add_argument("--port", type=int, default=0, help="Local callback port; 0 chooses one")
    setup.add_argument("--timeout-seconds", type=int, default=300)
    setup.add_argument("--force", action="store_true", help="Replace an existing token file")

    status = subparsers.add_parser("status", help="Validate local OAuth setup without networking")
    _add_paths(status)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    client_file, token_file = _paths(arguments)

    if arguments.command == "status":
        try:
            validate_client_file(client_file)
        except GoogleOAuthSetupError as exc:
            print(f"client=invalid: {exc}", file=sys.stderr)
            return 1
        print("client=configured")
        print(f"token={authorized_user_file_status(token_file)}")
        return 0

    profiles = arguments.scope_profile or DEFAULT_SCOPE_PROFILES
    try:
        scopes = perform_setup(
            client_file=client_file,
            token_file=token_file,
            profiles=profiles,
            open_browser=not arguments.no_browser,
            port=arguments.port,
            timeout_seconds=arguments.timeout_seconds,
            force=arguments.force,
        )
    except GoogleOAuthSetupError as exc:
        print(f"Google OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"Google OAuth setup failed ({type(exc).__name__}); no credential details were logged",
            file=sys.stderr,
        )
        return 1

    print(f"Google OAuth token written securely to {token_file}")
    print(f"Granted scope count: {len(scopes)}")
    print(
        "Docket Calendar reads and writes remain controlled independently by "
        "DOCKET_CALENDAR_READS_ENABLED and DOCKET_EXTERNAL_WRITES_ENABLED."
    )
    return 0
