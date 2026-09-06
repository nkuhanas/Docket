from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from docket.config import get_settings
from docket.database import configure_database, get_session_factory
from docket.domain.errors import DocketError
from docket.providers.google.calendar import CalendarProviderError
from docket.providers.google.factory import build_calendar_write_provider
from docket.services.operations import OperationRunner


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docket-calendar-recovery",
        description="Inspect or recover Calendar operations for one committed ChangeSet.",
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
    return parser


def _error(code: str, message: str) -> dict[str, object]:
    return {"ok": False, "error": {"code": code, "message": message}}


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    settings = get_settings()
    configure_database(settings.database_url)
    runner = OperationRunner(
        get_session_factory(),
        build_calendar_write_provider(settings),
        execution_enabled=settings.external_writes_enabled,
    )
    try:
        if arguments.command == "status":
            status = runner.auth_failure_recovery_status(arguments.changeset_ref)
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
