"""Explicit local administration; never returns OAuth credentials or subject claims."""

import argparse
import json
from collections.abc import Sequence

from docket.config import get_settings
from docket.database import configure_database, session_scope
from docket.services.oauth import OAuthPrincipalService


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docket-plugin-auth")
    commands = parser.add_subparsers(dest="command", required=True)
    approve = commands.add_parser("approve", help="Approve an existing identity-provider user")
    identity = approve.add_mutually_exclusive_group(required=True)
    identity.add_argument("--subject", help="Exact provider user subject, never a token")
    identity.add_argument("--principal-ref", help="Existing approved public principal ref")
    approve.add_argument("--write", action="store_true")
    approve.add_argument("--events", action="store_true")
    approve.add_argument("--triage", action="store_true")
    approve.add_argument("--execute", action="store_true")
    revoke = commands.add_parser("revoke", help="Revoke an approved public principal ref")
    revoke.add_argument("principal_ref")
    revoke.add_argument("--execute", action="store_true")
    arguments = parser.parse_args(argv)
    if not arguments.execute:
        print(json.dumps({"action": arguments.command, "executed": False}))
        return 0
    settings = get_settings()
    configure_database(settings.database_url)
    try:
        with session_scope() as session:
            service = OAuthPrincipalService(session, settings)
            if arguments.command == "approve":
                approve_owner = (
                    service.approve_existing if arguments.principal_ref else service.approve
                )
                row = approve_owner(
                    arguments.principal_ref or arguments.subject,
                    events=arguments.events, triage=arguments.triage, write=arguments.write,
                )
            else:
                row = service.revoke(arguments.principal_ref)
            receipt = {"principal_ref": row.ref_id, "enabled": row.enabled, "scopes": row.scopes}
        print(json.dumps(receipt))
        return 0
    except ValueError:
        print(json.dumps({"ok": False, "error": "principal_configuration_invalid"}))
        return 2
