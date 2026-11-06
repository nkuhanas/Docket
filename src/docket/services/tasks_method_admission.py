"""Load method-specific G0 evidence for the configured account and current grant."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import Settings
from docket.models import ProviderAccount, TasksMethodValidation
from docket.providers.google.oauth import TASKS_SCOPE, authorized_user_file_status
from docket.providers.google.tasks import TasksWriteGate, WriteMethod, credential_fingerprint

ADAPTER_VERSION = 1
METHODS: tuple[WriteMethod, ...] = ("insert", "patch", "move", "delete")


class TasksMethodAdmission:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session, self.settings = session, settings

    def evidence(self, account: ProviderAccount) -> dict[WriteMethod, dict[str, bool]]:
        if (
            not self.settings.tasks_enabled
            or not account.enabled
            or account.provider != "google"
            or account.external_account_id != self.settings.google_account_external_id
            or "google_tasks" not in account.capabilities
        ):
            return {}
        if (
            authorized_user_file_status(
                self.settings.google_oauth_token_file,
                required_scopes=[TASKS_SCOPE],
            )
            != "configured"
        ):
            return {}
        fingerprint = credential_fingerprint(self.settings.google_oauth_token_file)
        if fingerprint is None:
            return {}
        result: dict[WriteMethod, dict[str, bool]] = {}
        for method in METHODS:
            row = self.session.scalar(
                select(TasksMethodValidation)
                .where(
                    TasksMethodValidation.account_id == account.id,
                    TasksMethodValidation.method == method,
                    TasksMethodValidation.adapter_version == ADAPTER_VERSION,
                    TasksMethodValidation.credential_fingerprint == fingerprint,
                )
                .order_by(TasksMethodValidation.verified_at.desc(), TasksMethodValidation.id.desc())
                .limit(1)
            )
            if row is None or not row.passed:
                continue
            required = (
                {"uncertain_insert_recovery"}
                if method == "insert"
                else {"conditional_write", "stable_move_identity"}
                if method == "move"
                else {"conditional_write"}
            )
            if all(row.capabilities.get(key) is True for key in required):
                result[method] = dict(row.capabilities)
        return result

    def gate(self, account: ProviderAccount) -> TasksWriteGate:
        evidence = self.evidence(account)
        return TasksWriteGate(
            frozenset(evidence),
            credential_fingerprint(self.settings.google_oauth_token_file),
        )

    def readiness(self, account: ProviderAccount) -> dict[str, Any]:
        evidence = self.evidence(account)
        return {
            method: {
                "verified": method in evidence,
                "date_write_verified": evidence.get(method, {}).get("date_write", False),
            }
            for method in METHODS
        }
