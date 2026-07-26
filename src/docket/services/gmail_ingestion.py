from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import Settings, get_settings
from docket.domain.canonical import sha256_json
from docket.domain.enums import OutboxStatus
from docket.models import (
    Account,
    AuditEvent,
    ConnectorCheckpoint,
    OutboxEvent,
    SourceItem,
)
from docket.models.base import utc_now
from docket.providers.google.gmail import (
    GmailCursorInvalid,
    GmailMessageMetadata,
    GmailProviderError,
    GmailReadProvider,
)

_GMAIL_STREAM = "gmail:inbox"


@dataclass(frozen=True, slots=True)
class GmailScanClaim:
    checkpoint_id: uuid.UUID
    account_id: uuid.UUID
    lease_token: uuid.UUID
    cursor: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GmailScanRunResult:
    claimed: bool
    pages: int = 0
    observed: int = 0
    staged: int = 0
    recovery: bool = False
    completed: bool = False
    error_code: str | None = None


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _minimal_headers(message: GmailMessageMetadata) -> dict[str, Any]:
    return {
        "sender": message.sender,
        "subject": message.subject,
        "label_ids": list(message.label_ids),
        "thread_id": message.thread_id,
        "message_id": message.message_id,
        "size_estimate": message.size_estimate,
    }


def _source_fingerprint(
    account_id: uuid.UUID,
    message: GmailMessageMetadata,
) -> str:
    return sha256_json(
        {
            "provider": "gmail",
            "account_id": str(account_id),
            "message_id": message.message_id,
            "source_version": message.source_version,
            "headers": _minimal_headers(message),
        }
    )


class GmailIngestionService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: GmailReadProvider,
        settings: Settings | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.provider = provider
        self.settings = settings or get_settings()

    @staticmethod
    def _is_continuation(cursor: dict[str, Any]) -> bool:
        page_token = cursor.get("page_token")
        return isinstance(page_token, str) and bool(page_token)

    def _claim(self, *, force: bool) -> GmailScanClaim | None:
        now = utc_now()
        with self.session_factory.begin() as session:
            accounts = session.scalars(
                select(Account)
                .where(
                    Account.provider == "google",
                    Account.enabled.is_(True),
                )
                .order_by(Account.created_at)
                .with_for_update(skip_locked=True)
            ).all()
            account = next(
                (
                    candidate
                    for candidate in accounts
                    if "gmail" in candidate.capabilities
                ),
                None,
            )
            if account is None:
                return None
            checkpoint = session.scalar(
                select(ConnectorCheckpoint)
                .where(
                    ConnectorCheckpoint.account_id == account.id,
                    ConnectorCheckpoint.stream == _GMAIL_STREAM,
                )
                .with_for_update()
            )
            if checkpoint is None:
                checkpoint = ConnectorCheckpoint(
                    account_id=account.id,
                    stream=_GMAIL_STREAM,
                    cursor={},
                    version=1,
                )
                session.add(checkpoint)
                session.flush()
            if (
                checkpoint.leased_until is not None
                and _aware(checkpoint.leased_until) > now
            ):
                return None
            if (
                not force
                and not self._is_continuation(checkpoint.cursor)
                and checkpoint.last_success_at is not None
                and _aware(checkpoint.last_success_at)
                + timedelta(seconds=self.settings.gmail_scan_interval_seconds)
                > now
            ):
                return None
            lease_token = uuid.uuid4()
            checkpoint.lease_token = lease_token
            checkpoint.leased_until = now + timedelta(
                seconds=self.settings.gmail_scan_lease_seconds
            )
            checkpoint.last_attempt_at = now
            return GmailScanClaim(
                checkpoint_id=checkpoint.id,
                account_id=account.id,
                lease_token=lease_token,
                cursor=dict(checkpoint.cursor),
            )

    def _stage_page(
        self,
        claim: GmailScanClaim,
        *,
        messages: tuple[GmailMessageMetadata, ...],
        next_cursor: dict[str, Any],
        observed_through: datetime,
        provider_request_id: str | None,
        recovery: bool,
    ) -> tuple[int, bool]:
        now = utc_now()
        completed = not self._is_continuation(next_cursor)
        staged = 0
        with self.session_factory.begin() as session:
            checkpoint = session.get(ConnectorCheckpoint, claim.checkpoint_id)
            if checkpoint is None or checkpoint.lease_token != claim.lease_token:
                raise GmailProviderError(
                    "gmail_scan_lease_lost",
                    "The Gmail scan lease was lost.",
                    transient=True,
                )
            for message in messages:
                existing = session.scalar(
                    select(SourceItem.id).where(
                        SourceItem.account_id == claim.account_id,
                        SourceItem.provider == "gmail",
                        SourceItem.external_object_id == message.message_id,
                        SourceItem.source_version == message.source_version,
                    )
                )
                if existing is not None:
                    continue
                session.add(
                    SourceItem(
                        account_id=claim.account_id,
                        provider="gmail",
                        external_object_id=message.message_id,
                        external_parent_id=message.thread_id,
                        source_version=message.source_version,
                        source_fingerprint=_source_fingerprint(claim.account_id, message),
                        received_at=message.received_at,
                        minimal_headers=_minimal_headers(message),
                        status="staged",
                    )
                )
                staged += 1
            checkpoint.cursor = next_cursor
            checkpoint.observed_through = _aware(observed_through)
            checkpoint.last_attempt_at = now
            checkpoint.last_error_code = None
            checkpoint.version += 1
            checkpoint.leased_until = now + timedelta(
                seconds=self.settings.gmail_scan_lease_seconds
            )
            if completed:
                checkpoint.last_success_at = now
                checkpoint.lease_token = None
                checkpoint.leased_until = None
            session.add(
                AuditEvent(
                    event_type=(
                        "gmail.recovery_page_staged"
                        if recovery
                        else "gmail.history_page_staged"
                    ),
                    entity_type="connector_checkpoint",
                    entity_id=checkpoint.id,
                    actor_type="system",
                    actor_id=None,
                    data={
                        "observed_count": len(messages),
                        "staged_count": staged,
                        "completed": completed,
                        "provider_request_id": provider_request_id,
                    },
                )
            )
        return staged, completed

    def _reset_invalid_cursor(self, claim: GmailScanClaim) -> GmailScanClaim:
        with self.session_factory.begin() as session:
            checkpoint = session.get(ConnectorCheckpoint, claim.checkpoint_id)
            if checkpoint is None or checkpoint.lease_token != claim.lease_token:
                raise GmailProviderError(
                    "gmail_scan_lease_lost",
                    "The Gmail scan lease was lost.",
                    transient=True,
                )
            checkpoint.cursor = {}
            checkpoint.last_error_code = "gmail_cursor_recovery"
            checkpoint.version += 1
            session.add(
                AuditEvent(
                    event_type="gmail.cursor_recovery_started",
                    entity_type="connector_checkpoint",
                    entity_id=checkpoint.id,
                    actor_type="system",
                    actor_id=None,
                    data={"previous_cursor_kind": str(claim.cursor.get("mode") or "unknown")},
                )
            )
        return GmailScanClaim(
            checkpoint_id=claim.checkpoint_id,
            account_id=claim.account_id,
            lease_token=claim.lease_token,
            cursor={},
        )

    def _release_with_error(self, claim: GmailScanClaim, error_code: str) -> None:
        now = utc_now()
        with self.session_factory.begin() as session:
            checkpoint = session.get(ConnectorCheckpoint, claim.checkpoint_id)
            if checkpoint is None or checkpoint.lease_token != claim.lease_token:
                return
            checkpoint.last_error_code = error_code[:128]
            checkpoint.lease_token = None
            checkpoint.leased_until = None
            self._ensure_stale_alert(session, checkpoint, now)
            session.add(
                AuditEvent(
                    event_type="gmail.scan_failed",
                    entity_type="connector_checkpoint",
                    entity_id=checkpoint.id,
                    actor_type="system",
                    actor_id=None,
                    data={"error_code": checkpoint.last_error_code},
                )
            )

    def _ensure_stale_alert(
        self,
        session: Session,
        checkpoint: ConnectorCheckpoint,
        now: datetime,
    ) -> bool:
        stale = (
            checkpoint.last_success_at is None
            or (now - _aware(checkpoint.last_success_at)).total_seconds()
            > self.settings.gmail_stale_seconds
        )
        if not stale:
            return False
        episode = (
            _aware(checkpoint.last_success_at).isoformat()
            if checkpoint.last_success_at is not None
            else "never"
        )
        key = f"discord_system_alert:gmail_stale:{checkpoint.id}:{episode}"
        if (
            session.scalar(
                select(OutboxEvent.id).where(
                    OutboxEvent.deduplication_key == key
                )
            )
            is None
        ):
            session.add(
                OutboxEvent(
                    id=uuid.uuid5(uuid.NAMESPACE_URL, key),
                    event_type="discord.system_alert.requested",
                    aggregate_type="connector_checkpoint",
                    aggregate_id=checkpoint.id,
                    deduplication_key=key,
                    payload={
                        "title": "Docket Gmail ingestion is stale",
                        "summary": (
                            "New email may not be staged yet. Docket retained its "
                            "committed cursor and did not discard pending sources."
                        ),
                        "error_code": (
                            checkpoint.last_error_code
                            or "gmail_ingestion_stale"
                        ),
                        "occurred_at": now.isoformat(),
                    },
                    status=OutboxStatus.PENDING.value,
                )
            )
        return True

    def evaluate_staleness(self) -> int:
        if not self.settings.gmail_ingestion_enabled:
            return 0
        now = utc_now()
        count = 0
        with self.session_factory.begin() as session:
            checkpoints = session.scalars(
                select(ConnectorCheckpoint).where(
                    ConnectorCheckpoint.stream == _GMAIL_STREAM
                )
            ).all()
            for checkpoint in checkpoints:
                if self._ensure_stale_alert(session, checkpoint, now):
                    count += 1
        return count

    def run_due_once(self, *, force: bool = False) -> GmailScanRunResult:
        if not self.settings.gmail_ingestion_enabled:
            return GmailScanRunResult(claimed=False)
        claim = self._claim(force=force)
        if claim is None:
            return GmailScanRunResult(claimed=False)
        recovery_after = utc_now() - timedelta(
            days=self.settings.gmail_recovery_overlap_days
        )
        pages = observed = staged = 0
        recovery_used = False
        cursor_recovered = False
        try:
            while pages < self.settings.gmail_scan_max_pages:
                try:
                    page = self.provider.scan_page(
                        cursor=claim.cursor,
                        recovery_after=recovery_after,
                    )
                except GmailCursorInvalid:
                    if cursor_recovered:
                        raise
                    claim = self._reset_invalid_cursor(claim)
                    cursor_recovered = True
                    recovery_used = True
                    continue
                pages += 1
                observed += len(page.messages)
                if observed > self.settings.gmail_scan_max_messages:
                    raise GmailProviderError(
                        "gmail_scan_message_bound",
                        "The Gmail scan exceeded its message bound.",
                        transient=False,
                    )
                page_staged, completed = self._stage_page(
                    claim,
                    messages=page.messages,
                    next_cursor=page.next_cursor,
                    observed_through=page.observed_through,
                    provider_request_id=page.provider_request_id,
                    recovery=page.recovery,
                )
                staged += page_staged
                recovery_used = recovery_used or page.recovery
                claim = GmailScanClaim(
                    checkpoint_id=claim.checkpoint_id,
                    account_id=claim.account_id,
                    lease_token=claim.lease_token,
                    cursor=page.next_cursor,
                )
                if completed:
                    return GmailScanRunResult(
                        claimed=True,
                        pages=pages,
                        observed=observed,
                        staged=staged,
                        recovery=recovery_used,
                        completed=True,
                    )
            self._release_with_error(claim, "gmail_scan_page_bound")
            return GmailScanRunResult(
                claimed=True,
                pages=pages,
                observed=observed,
                staged=staged,
                recovery=recovery_used,
                completed=False,
                error_code="gmail_scan_page_bound",
            )
        except GmailProviderError as exc:
            self._release_with_error(claim, exc.code)
            return GmailScanRunResult(
                claimed=True,
                pages=pages,
                observed=observed,
                staged=staged,
                recovery=recovery_used,
                completed=False,
                error_code=exc.code,
            )
