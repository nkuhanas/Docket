from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from docket.models import DeferredIngress, DiscordDailyThread, DrainBarrier, OperatorUtterance
from docket.providers.discord import DiscordProjectionAdapter


def _source_parts(source_ref: str, *, prefix: str) -> tuple[str, str, str] | None:
    parts = source_ref.split(":")
    if len(parts) != 4 or parts[0] != prefix:
        return None
    return parts[1], parts[2], parts[3]


class DeferredIngressRunner:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        adapter: DiscordProjectionAdapter,
    ) -> None:
        self.session_factory = session_factory
        self.adapter = adapter

    def run_once(self) -> bool:
        with self.session_factory.begin() as session:
            if session.scalar(
                select(DrainBarrier.id)
                .where(DrainBarrier.status.in_(("requested", "draining")))
                .limit(1)
            ) is not None:
                return False
            ingress = session.scalar(
                select(DeferredIngress)
                .where(DeferredIngress.status == "pending")
                .order_by(DeferredIngress.created_at, DeferredIngress.ref_id)
                .limit(1)
            )
            if ingress is None:
                return False
            utterance = session.scalar(
                select(OperatorUtterance).where(
                    OperatorUtterance.ref_id == ingress.utterance_ref
                )
            )
            if utterance is None:
                ingress.status = "rejected"
                ingress.last_error_code = "operator_utterance_not_found"
                return True
            payload = self._payload(session, ingress, utterance)
        self.adapter.post_deferred_ingress(payload)
        return True

    @staticmethod
    def _payload(
        session: Session,
        ingress: DeferredIngress,
        utterance: OperatorUtterance,
    ) -> dict[str, Any]:
        source_prefix = (
            "discord_message" if ingress.ingress_kind == "typed_message" else "discord_interaction"
        )
        source = _source_parts(utterance.source_message_ref, prefix=source_prefix)
        if source is None:
            raise RuntimeError("deferred ingress has an invalid Discord source binding")
        guild_id, channel_id, source_id = source
        parent_channel_id = session.scalar(
            select(DiscordDailyThread.channel_id).where(
                DiscordDailyThread.guild_id == guild_id,
                DiscordDailyThread.thread_id == channel_id,
            )
        )
        reply_to_message_id: str | None = None
        if utterance.reply_to_source_ref is not None:
            reply = _source_parts(utterance.reply_to_source_ref, prefix="discord_message")
            if reply is not None:
                reply_to_message_id = reply[2]
        return {
            "request_id": str(uuid.uuid4()),
            "deferred_ingress_ref": ingress.ref_id,
            "ingress_kind": ingress.ingress_kind,
            "utterance_ref": utterance.ref_id,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "parent_channel_id": parent_channel_id,
            "source_id": source_id,
            "reply_to_message_id": reply_to_message_id,
            "verbatim_text": utterance.verbatim_text,
            "selected_option_binding": ingress.selected_option_binding_json,
        }
