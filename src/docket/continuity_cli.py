from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from docket.config import get_settings
from docket.database import configure_database, get_session_factory, session_scope
from docket.domain.errors import DocketError
from docket.providers.discord import HttpDiscordProjectionAdapter
from docket.services.continuity import ContinuityService
from docket.services.ingress_deployment import IngressDeploymentService


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
    subparsers.add_parser("quiesce-ingress-options")
    subparsers.add_parser("regenerate-ingress-options")
    return parser


def _execute(arguments: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    configure_database(settings.database_url)
    if arguments.command in {"quiesce-ingress-options", "regenerate-ingress-options"}:
        ingress_service = IngressDeploymentService(
            get_session_factory(),
            HttpDiscordProjectionAdapter(
                settings.discord_projection_url,
                settings.docket_to_hermes_token(),
            ),
        )
        if arguments.command == "quiesce-ingress-options":
            return ingress_service.quiesce()
        return ingress_service.regenerate()
    with session_scope() as session:
        continuity_service = ContinuityService(session)
        if arguments.command == "request":
            timeout_seconds = arguments.timeout_seconds or settings.deploy_drain_timeout_seconds
            return continuity_service.request_drain(
                requested_by=arguments.requested_by,
                timeout_seconds=timeout_seconds,
            )
        if arguments.command == "status":
            return continuity_service.drain_status(arguments.drain_ref)
        return continuity_service.release_drain(
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
