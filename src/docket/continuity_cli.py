from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from docket.config import get_settings
from docket.database import configure_database, session_scope
from docket.domain.errors import DocketError
from docket.services.continuity import ContinuityService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Docket deployment-continuity control")
    subparsers = parser.add_subparsers(dest="command", required=True)
    request = subparsers.add_parser("request")
    request.add_argument("--requested-by", default="scripts/docket deploy")
    request.add_argument("--timeout-seconds", type=int)
    status = subparsers.add_parser("status")
    status.add_argument("drain_ref")
    release = subparsers.add_parser("release")
    release.add_argument("drain_ref")
    abort = subparsers.add_parser("abort")
    abort.add_argument("drain_ref")
    return parser


def _execute(arguments: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    configure_database(settings.database_url)
    with session_scope() as session:
        service = ContinuityService(session)
        if arguments.command == "request":
            timeout_seconds = arguments.timeout_seconds or settings.deploy_drain_timeout_seconds
            return service.request_drain(
                requested_by=arguments.requested_by,
                timeout_seconds=timeout_seconds,
            )
        if arguments.command == "status":
            return service.drain_status(arguments.drain_ref)
        return service.release_drain(
            arguments.drain_ref,
            aborted=arguments.command == "abort",
        )


def main() -> None:
    try:
        result = _execute(_parser().parse_args())
    except DocketError as exc:
        print(json.dumps(exc.as_dict(), separators=(",", ":"), sort_keys=True))
        sys.exit(1)
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))


if __name__ == "__main__":
    main()
