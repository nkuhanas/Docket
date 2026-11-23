"""Operator-local source authorization for the dedicated financial collector."""

import argparse
import hashlib
import re
import sys
from datetime import date

from sqlalchemy import select

from docket.config import get_settings
from docket.database import configure_database, session_scope
from docket.models.financial_intake import FinancialCollector


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["authorize", "revoke"])
    parser.add_argument("--backfill-start", type=date.fromisoformat, default=date(2026, 11, 22))
    parser.add_argument("--owner-only-retain-until-removed", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    configure_database(settings.database_url)
    with session_scope() as session:
        grant = session.scalar(
            select(FinancialCollector)
            .where(FinancialCollector.identity == "dedicated-billing")
            .with_for_update()
        )
        if args.action == "revoke":
            if grant:
                grant.enabled = False
                grant.revision += 1
            print("Collector authorization disabled.")
            return
        if not args.owner_only_retain_until_removed:
            parser.error("Confirm the original-file retention policy.")
        token = sys.stdin.readline(128).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise SystemExit("A local collector authentication key is required on standard input.")
        if not grant:
            grant = FinancialCollector(
                operator_ref=settings.canonical_operator_ref,
                identity="dedicated-billing",
                revision=0,
            )
            session.add(grant)
        if grant.operator_ref != settings.canonical_operator_ref:
            raise SystemExit("Source belongs to another operator.")
        grant.policy = {
            "mailbox": "billing@chacel.ai",
            "folder": "INBOX",
            "backfill_start": args.backfill_start.isoformat(),
            "retention": "until_operator_removal",
            "access": "owner_only",
            "allowed_actions": ["submit_eml"],
            "checkpoint": "durable_source",
            "max_bytes": settings.attachment_max_bytes,
            "declaration": {"business_use_percent": "100", "write_off_intent": True},
        }
        grant.enabled = True
        grant.revision += 1
        grant.credential_digest = hashlib.sha256(token.encode()).hexdigest()
        session.flush()
        print(f"Registered source {grant.ref_id} at policy revision {grant.revision}.")


if __name__ == "__main__":
    main()
