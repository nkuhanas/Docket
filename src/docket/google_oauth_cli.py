from __future__ import annotations

import argparse
import os
import shlex
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

DEFAULT_REMOTE_CALLBACK_PORT = 8765


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
    setup.add_argument(
        "--remote",
        action="store_true",
        help=(
            "Use a fixed loopback callback port for authorization through an "
            "SSH local-forward; implies --no-browser"
        ),
    )
    setup.add_argument(
        "--ssh-target",
        default=os.environ.get("DOCKET_OAUTH_SSH_TARGET"),
        help="SSH destination shown in remote-mode tunnel instructions",
    )
    setup.add_argument("--port", type=int, default=0, help="Local callback port; 0 chooses one")
    setup.add_argument("--timeout-seconds", type=int, default=300)
    setup.add_argument("--force", action="store_true", help="Replace an existing token file")

    status = subparsers.add_parser("status", help="Validate local OAuth setup without networking")
    _add_paths(status)
    return parser


def _remote_instructions(port: int, ssh_target: str | None) -> str:
    target = shlex.quote(ssh_target) if ssh_target else "<your-ssh-target>"
    return "\n".join(
        (
            "Remote OAuth mode enabled.",
            "On the computer running your browser, keep this tunnel open:",
            f"  ssh -N -L {port}:127.0.0.1:{port} {target}",
            "Then open the authorization URL printed below in that browser.",
            "The Google redirect will return through the tunnel to Docket.",
        )
    )


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
    port = arguments.port
    if arguments.remote:
        if port == 0:
            port = DEFAULT_REMOTE_CALLBACK_PORT
        print(_remote_instructions(port, arguments.ssh_target), flush=True)
    try:
        scopes = perform_setup(
            client_file=client_file,
            token_file=token_file,
            profiles=profiles,
            open_browser=not (arguments.no_browser or arguments.remote),
            port=port,
            timeout_seconds=arguments.timeout_seconds,
            force=arguments.force,
            callback_host="127.0.0.1",
        )
    except GoogleOAuthSetupError as exc:
        print(f"Google OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        hint = (
            "; retry with --remote from a remote terminal"
            if type(exc).__name__ == "Error" and not arguments.remote
            else ""
        )
        print(
            f"Google OAuth setup failed ({type(exc).__name__}){hint}; "
            "no credential details were logged",
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
