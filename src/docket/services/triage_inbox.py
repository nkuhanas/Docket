"""Expose ready intelligence work without selecting or launching an inference agent."""

import uuid

from sqlalchemy import String, cast, exists, func, literal, or_, select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import Settings
from docket.models import GmailSource, PluginNotification
from docket.models.base import utc_now
from docket.services.notifications import NotificationService


def triage_notice_key(ref: str, token: uuid.UUID | None) -> str:
    return f"triage-ready:{ref}:{token.hex if token is not None else 'initial'}"


class TriageInboxService:
    def __init__(self, factory: sessionmaker[Session], settings: Settings) -> None:
        self.factory, self.settings = factory, settings

    def run_due_once(self) -> int:
        if not self.settings.plugin_enabled:
            return 0
        now = utc_now()
        key = (literal("triage-ready:") + GmailSource.ref_id + literal(":") + func.coalesce(
            func.replace(cast(GmailSource.claim_token, String), "-", ""), literal("initial"),
        ))
        with self.factory.begin() as session:
            sources = session.scalars(select(GmailSource).where(
                or_(
                    GmailSource.status == "staged",
                    (GmailSource.status == "claimed") & (GmailSource.claimed_until <= now),
                    (GmailSource.status == "failed") & or_(
                        GmailSource.next_attempt_at.is_(None), GmailSource.next_attempt_at <= now,
                    ),
                ),
                ~exists(select(PluginNotification.id).where(
                    PluginNotification.deduplication_key == key,
                )),
            ).order_by(GmailSource.received_at, GmailSource.created_at, GmailSource.ref_id)
                .limit(100).with_for_update(skip_locked=True)).all()
            for source in sources:
                NotificationService(session, self.settings).publish(
                    "docket.triage.ready", source.ref_id,
                    key=triage_notice_key(source.ref_id, source.claim_token),
                )
            return len(sources)
