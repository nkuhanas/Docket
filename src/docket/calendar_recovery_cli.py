from __future__ import annotations

import argparse
import hmac
import json
import re
import sys
from collections.abc import Sequence

from docket.config import get_settings
from docket.database import configure_database, get_session_factory
from docket.domain.errors import DocketError
from docket.providers.google.calendar import CalendarProviderError
from docket.providers.google.factory import build_calendar_write_provider
from docket.providers.google.oauth import GoogleOAuthSetupError, credential_fingerprint
from docket.services.operations import OperationRunner


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docket-calendar-recovery",
        description="Recover committed Calendar deliveries after Google reauthorization.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status")
    status.add_argument("changeset_ref")
    requeue = subparsers.add_parser("requeue-auth-failures")
    requeue.add_argument("changeset_ref")
    requeue.add_argument(
        "--execute",
        action="store_true",
        help="Requeue the exact failed operations after validating Google authorization.",
    )
    restored = subparsers.add_parser("requeue-after-reauth")
    restored.add_argument("--execute", action="store_true")
    restored.add_argument("--credential-sha256-stdin", action="store_true", required=True)
    return parser


def _error(code: str, message: str) -> dict[str, object]:
    return {"ok": False, "error": {"code": code, "message": message}}


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    settings = get_settings()
    try:
        configure_database(settings.database_url)
        runner = OperationRunner(
            get_session_factory(),
            build_calendar_write_provider(settings),
            execution_enabled=settings.external_writes_enabled,
        )
        if arguments.command == "status":
            status = runner.auth_failure_recovery_status(arguments.changeset_ref)
        elif arguments.command == "requeue-after-reauth":
            if not arguments.execute:
                raise DocketError(
                    code="calendar_recovery_execution_confirmation_required",
                    message="Reauthorization recovery requires --execute.",
                )
            expected = sys.stdin.read(66).strip()
            if not re.fullmatch(r"[0-9a-f]{64}", expected) or not hmac.compare_digest(
                expected, credential_fingerprint(settings.google_oauth_token_file),
            ):
                raise DocketError(
                    code="calendar_recovery_credential_mismatch",
                    message="The running service does not use the credential just authorized.",
                )
            if settings.calendar_write_mode() != "google":
                raise DocketError(
                    code="calendar_recovery_writes_disabled",
                    message="The running service is not enabled for real Google delivery.",
                )
            recovered = runner.requeue_after_reauthorization(
                external_account_id=settings.google_account_external_id,
                credential_ref=str(settings.google_oauth_token_file),
            )
            print(json.dumps({"ok": True, "recovery": recovered.projection()}, sort_keys=True))
            return 0
        else:
            if not arguments.execute:
                output = _error(
                    "calendar_recovery_execution_confirmation_required",
                    "Pass --execute to requeue provider operations.",
                )
                print(json.dumps(output, separators=(",", ":"), sort_keys=True))
                return 2
            status = runner.requeue_auth_failures(arguments.changeset_ref)
    except DocketError as exc:
        print(json.dumps(exc.as_dict(), separators=(",", ":"), sort_keys=True))
        return 1
    except CalendarProviderError as exc:
        output = _error(exc.code, exc.safe_message)
        print(json.dumps(output, separators=(",", ":"), sort_keys=True))
        return 1
    except GoogleOAuthSetupError:
        print(json.dumps(_error(
            "calendar_recovery_credential_unavailable", "The runtime credential is unavailable.",
        )))
        return 1
    except Exception:
        # Database/transport exceptions can contain connection strings or
        # credential paths. The host reports recovery separately from consent.
        print(json.dumps(_error(
            "calendar_recovery_unavailable", "Delivery recovery did not complete; inspect status.",
        )))
        return 1
    print(
        json.dumps(
            {"ok": True, "recovery": status.projection()},
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
