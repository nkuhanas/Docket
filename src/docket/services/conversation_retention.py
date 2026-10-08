"""Expire encrypted transcript payloads while preserving attribution and hashes."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import Settings
from docket.models import ConversationRecord


class ConversationRetentionService:
    def __init__(self, factory: sessionmaker[Session], settings: Settings) -> None:
        self.factory = factory
        self.settings = settings

    def run_once(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=self.settings.conversation_retention_days)
        with self.factory.begin() as session:
            rows = list(
                session.scalars(
                    select(ConversationRecord)
                    .where(
                        ConversationRecord.recorded_at < cutoff,
                        ConversationRecord.ciphertext.is_not(None),
                        ConversationRecord.purged_at.is_(None),
                    )
                    .order_by(ConversationRecord.recorded_at)
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
            )
            for row in rows:
                row.ciphertext = None
                row.nonce = None
                row.encryption_key_ref = None
                row.purged_at = now
            return len(rows)
