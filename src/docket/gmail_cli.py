from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict

from sqlalchemy import select

from docket.config import get_settings
from docket.database import configure_database, get_session_factory
from docket.domain.errors import DocketError
from docket.models import Account, ConnectorCheckpoint, SourceItem
from docket.providers.google.factory import build_gmail_read_provider
from docket.schemas.triage import ProposeClassifiedGmailActionInput
from docket.services.gmail_ingestion import GmailIngestionService
from docket.services.gmail_recovery import GmailRecoveryService
from docket.services.triage import TriageService


def _status() -> dict[str, object]:
    settings = get_settings()
    with get_session_factory()() as session:
        accounts = [
            account
            for account in session.scalars(
                select(Account).where(
                    Account.provider == "google",
                    Account.enabled.is_(True),
                )
            ).all()
            if "gmail" in account.capabilities
        ]
        checkpoints = session.scalars(
            select(ConnectorCheckpoint)
            .where(ConnectorCheckpoint.stream == "gmail:inbox")
            .order_by(ConnectorCheckpoint.account_id)
        ).all()
        source_counts = Counter(session.scalars(select(SourceItem.status)).all())
    return {
        "gmail_ingestion_enabled": settings.gmail_ingestion_enabled,
        "gmail_writes_enabled": settings.gmail_writes_enabled,
        "provider_mode": settings.gmail_provider_mode(),
        "triage_source_allowlist_count": len(settings.gmail_triage_source_allowlist),
        "gmail_account_count": len(accounts),
        "checkpoints": [
            {
                "account_id": str(checkpoint.account_id),
                "cursor_mode": str(checkpoint.cursor.get("mode") or "recovery"),
                "last_attempt_at": (
                    checkpoint.last_attempt_at.isoformat()
                    if checkpoint.last_attempt_at is not None
                    else None
                ),
                "last_success_at": (
                    checkpoint.last_success_at.isoformat()
                    if checkpoint.last_success_at is not None
                    else None
                ),
                "last_error_code": checkpoint.last_error_code,
                "leased_until": (
                    checkpoint.leased_until.isoformat()
                    if checkpoint.leased_until is not None
                    else None
                ),
                "version": checkpoint.version,
            }
            for checkpoint in checkpoints
        ],
        "source_counts": dict(sorted(source_counts.items())),
    }


def _scan() -> tuple[int, dict[str, object]]:
    settings = get_settings()
    if not settings.gmail_ingestion_enabled:
        return 2, {
            "error": "gmail_ingestion_disabled",
            "message": "Enable read-only Gmail ingestion and recreate Docket first.",
        }
    provider = build_gmail_read_provider(settings)
    if provider is None:
        return 2, {
            "error": "gmail_provider_disabled",
            "message": "The Gmail read provider is not available.",
        }
    service = GmailIngestionService(get_session_factory(), provider, settings)
    result = service.run_due_once(force=True)
    service.evaluate_staleness()
    return 0 if result.error_code is None else 1, {
        "scan": asdict(result),
        "status": _status(),
    }


def _propose_archive(
    *,
    source_id: uuid.UUID,
    expected_source_version: str,
    request_key: str,
) -> tuple[int, dict[str, object]]:
    settings = get_settings()
    if not settings.gmail_ingestion_enabled:
        return 2, {
            "error": "gmail_ingestion_disabled",
            "message": "Gmail ingestion must be enabled before proposing a write.",
        }
    provider = build_gmail_read_provider(settings)
    if provider is None:
        return 2, {
            "error": "gmail_provider_disabled",
            "message": "The Gmail provider is not available.",
        }
    try:
        result = TriageService(
            get_session_factory(),
            provider,
            settings,
        ).propose_classified_gmail_action(
            ProposeClassifiedGmailActionInput(
                request_key=request_key,
                source_id=source_id,
                expected_source_version=expected_source_version,
                action_type="gmail_archive_message",
                actor_id=settings.operator_discord_user_id,
            )
        )
    except DocketError as exc:
        return 1, exc.as_dict()
    return 0, result


def _recover_operation(
    *,
    operation_id: uuid.UUID,
    request_key: str,
) -> tuple[int, dict[str, object]]:
    settings = get_settings()
    if not settings.gmail_writes_enabled:
        return 2, {
            "error": "gmail_writes_disabled",
            "message": "Gmail writes must remain enabled during reconciliation.",
        }
    try:
        result = GmailRecoveryService(get_session_factory()).request_reconciliation(
            operation_id=operation_id,
            request_key=request_key,
            actor_id=settings.operator_discord_user_id,
        )
    except DocketError as exc:
        return 1, exc.as_dict()
    return 0, result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docket-gmail")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    subparsers.add_parser("scan")
    archive = subparsers.add_parser("propose-archive")
    archive.add_argument("source_id", type=uuid.UUID)
    archive.add_argument("expected_source_version")
    archive.add_argument("request_key")
    recovery = subparsers.add_parser("reconcile-operation")
    recovery.add_argument("operation_id", type=uuid.UUID)
    recovery.add_argument("request_key")
    arguments = parser.parse_args(argv)
    settings = get_settings()
    configure_database(settings.database_url)
    if arguments.command == "scan":
        exit_code, output = _scan()
    elif arguments.command == "propose-archive":
        exit_code, output = _propose_archive(
            source_id=arguments.source_id,
            expected_source_version=arguments.expected_source_version,
            request_key=arguments.request_key,
        )
    elif arguments.command == "reconcile-operation":
        exit_code, output = _recover_operation(
            operation_id=arguments.operation_id,
            request_key=arguments.request_key,
        )
    else:
        exit_code, output = 0, _status()
    json.dump(output, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
