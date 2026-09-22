"""Isolated Compose fixture: authenticate a real click using the restricted role."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.discord_ingress import ingress_database_url
from docket.models import PersistedSemanticOption, ProjectionDelivery
from docket.security import issue_semantic_option_token
from docket.services.ingress_ledger import IngressIdentity, IngressLedgerService

settings = get_settings()
assert settings.operator_discord_user_id == "000000000000000001"
assert not settings.external_writes_enabled and not settings.calendar_reads_enabled
signing_key = settings.read_secret(settings.interaction_signing_key_file).encode()
owner = create_engine(settings.database_url)
with Session(owner) as session, session.begin():
    option = session.scalars(select(PersistedSemanticOption)).one()
    delivery = session.scalars(select(ProjectionDelivery).where(
        ProjectionDelivery.projection_ref == option.projection_ref,
    )).one()
    # Synthetic transport acknowledgement only in the isolated dummy fixture.
    delivery.status = "delivered"
    delivery.external_message_ref = (
        f"discord_message:{settings.discord_guild_id}:"
        f"{settings.chat_channel_id}:1542799000000000601"
    )
    token = issue_semantic_option_token(
        option_row_id=option.id, actor_id=settings.operator_discord_user_id,
        expires_at=datetime.now(UTC) + timedelta(hours=1), signing_key=signing_key,
    )
engine = create_engine(ingress_database_url())
results = []
responded_at = datetime.now(UTC)
for _ in range(2):
    with Session(engine) as session, session.begin():
        service = IngressLedgerService(
            session, identity=IngressIdentity(
                operator_id=settings.operator_discord_user_id, guild_id=settings.discord_guild_id,
                chat_channel_id=settings.chat_channel_id,
                queue_channel_id=settings.queue_channel_id,
            ), signing_key=signing_key,
            attachment_encryption_key=settings.attachment_encryption_key(),
            attachment_encryption_key_ref=settings.attachment_encryption_key_ref,
            attachment_max_bytes=settings.attachment_max_bytes,
            attachment_total_max_bytes=settings.attachment_total_max_bytes,
        )
        results.append(service.capture_semantic_selection(
            actor_id=settings.operator_discord_user_id, guild_id=settings.discord_guild_id,
            channel_id=settings.chat_channel_id, parent_channel_id=None,
            interaction_id="1542799000000000602", message_id="1542799000000000601",
            option_token=token, responded_at=responded_at,
        ))
assert results[0]["utterance_ref"] == results[1]["utterance_ref"]
assert results[0]["deferred_ingress_ref"] == results[1]["deferred_ingress_ref"]
for statement in (
    "UPDATE operator_utterances SET verbatim_text = verbatim_text",
    "UPDATE projection_deliveries SET status = status",
    "DELETE FROM persisted_semantic_options",
    "SELECT * FROM canonical_events LIMIT 1",
):
    with engine.connect() as connection:
        try:
            connection.execute(text(statement))
        except ProgrammingError:
            connection.rollback()
        else:
            connection.rollback()
            raise AssertionError("restricted ingress role exceeded its authority")
engine.dispose()
owner.dispose()
print("Restricted ingress selection, replay, and authority boundaries passed.")
