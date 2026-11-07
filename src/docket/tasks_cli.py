"""Local administration for actual Google Tasks method admission; never an MCP grant tool."""

import argparse
import hmac
import json
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select

from docket.config import get_settings
from docket.database import configure_database, get_session_factory
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import AuditEvent, ProviderAccount, TasksMethodValidation
from docket.providers.google.oauth import TASKS_SCOPE, authorized_user_file_status
from docket.providers.google.tasks import (
    GoogleTasksProvider,
    TasksProviderError,
    TasksWriteGate,
    credential_fingerprint,
)
from docket.services.accounts import AccountService
from docket.services.tasks_method_admission import ADAPTER_VERSION, METHODS, TasksMethodAdmission
from docket.services.tasks_validation import validate_tasks_methods


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docket-tasks")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    validate = commands.add_parser("validate")
    validate.add_argument("--targets-file", type=Path, required=True)
    validate.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        settings = get_settings()
        configure_database(settings.database_url)
        factory = get_session_factory()
        if args.command == "status":
            with factory() as session:
                account = session.scalar(
                    select(ProviderAccount).where(
                        ProviderAccount.provider == "google",
                        ProviderAccount.external_account_id == settings.google_account_external_id,
                    )
                )
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "enabled": settings.tasks_enabled,
                            "methods": TasksMethodAdmission(session, settings).readiness(account)
                            if account
                            else {method: {"verified": False} for method in METHODS},
                        },
                        sort_keys=True,
                    )
                )
            return 0
        path: Path = args.targets_file
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise DocketError(
                code="tasks_validation_targets_private",
                message="Use a protected regular validation-target file (0600).",
            )
        targets = json.loads(path.read_text())
        fingerprint = credential_fingerprint(settings.google_oauth_token_file)
        if (
            not fingerprint
            or authorized_user_file_status(
                settings.google_oauth_token_file, required_scopes=[TASKS_SCOPE]
            )
            != "configured"
        ):
            raise DocketError(
                code="tasks_validation_grant_mismatch",
                message="Configure the current Google Tasks grant before method validation.",
            )
        source, destination = targets.get("source_list"), targets.get("destination_list")
        if not all(
            isinstance(value, str) and 0 < len(value) <= 1024 for value in (source, destination)
        ):
            raise DocketError(
                code="tasks_validation_targets_invalid",
                message="Supply the two dedicated validation-list identities.",
            )
        with factory.begin() as session:
            account = AccountService(session).ensure_configured_google(
                settings.model_copy(update={"tasks_enabled": True}),
            )
            if not account.enabled:
                raise DocketError(
                    code="tasks_account_disabled", message="The configured account is disabled."
                )
            account_id, account_ref = account.id, account.ref_id
        provider = GoogleTasksProvider(
            str(settings.google_oauth_token_file),
            gate=TasksWriteGate(
                frozenset(METHODS),
                fingerprint,
            ),
        )
        report = validate_tasks_methods(provider, source, destination)
        if not hmac.compare_digest(
            fingerprint, credential_fingerprint(settings.google_oauth_token_file) or ""
        ):
            raise DocketError(
                code="tasks_validation_grant_changed",
                message="The grant changed during validation; no methods were admitted.",
            )
        report.update(
            adapter_version=ADAPTER_VERSION,
            account_ref=account_ref,
            current_grant_bound=True,
            real_google_api=True,
        )
        evidence = sha256_json(report)
        passed = {}
        with factory.begin() as session:
            account = session.get(ProviderAccount, account_id, with_for_update=True)
            if account is None or not account.enabled:
                raise DocketError(
                    code="tasks_account_disabled", message="The account became unavailable."
                )
            for method in METHODS:
                capabilities = report["capabilities"][method]
                passed[method] = report["created_records_cleaned"] and all(capabilities.values())
                session.add(
                    TasksMethodValidation(
                        account_id=account_id,
                        adapter_version=ADAPTER_VERSION,
                        credential_fingerprint=fingerprint,
                        method=method,
                        passed=passed[method],
                        capabilities=capabilities,
                        evidence_sha256=evidence,
                    )
                )
            session.add(
                AuditEvent(
                    event_type="tasks.methods_validated",
                    actor_type="system",
                    actor_id="local_tasks_validation",
                    entity_type="provider_account",
                    entity_id=account_id,
                    primary_ref=account_ref,
                    affected_refs=[account_ref],
                    data={
                        "adapter_version": ADAPTER_VERSION,
                        "methods_passed": passed,
                        "evidence_sha256": evidence,
                    },
                )
            )
        report.update(methods_passed=passed, ok=all(passed.values()), evidence_sha256=evidence)
        report["admission_recorded"] = True
        if args.report:
            try:
                args.report.write_text(json.dumps(report, indent=2) + "\n")
            except OSError:
                report["report_saved"] = False
        print(json.dumps(report, sort_keys=True))
        return 0 if report["ok"] else 1
    except (DocketError, TasksProviderError) as exc:
        message = exc.message if isinstance(exc, DocketError) else exc.safe_message
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": message}}))
        return 1
    except Exception as exc:
        # File parsing and third-party responses must never dump credential data.
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": type(exc).__name__,
                        "message": "Tasks validation could not finish; no success was asserted.",
                    },
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
