from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.models import (
    AttentionCase,
    CaseItem,
    DailyBrief,
    DailyBriefCaseItem,
    DiscordDailyThread,
    DiscordProjection,
    OperatorUtterance,
)


class ReplyBindingService:
    """Resolve a Discord reply to the exact Docket projection revision it displayed."""

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _discord_reply_parts(utterance: OperatorUtterance) -> tuple[str, str, str] | None:
        value = utterance.reply_to_source_ref
        if value is None:
            return None
        parts = value.split(":")
        if len(parts) != 4 or parts[0] != "discord_message":
            return None
        return parts[1], parts[2], parts[3]

    def resolve(self, utterance: OperatorUtterance) -> dict[str, Any] | None:
        parts = self._discord_reply_parts(utterance)
        if parts is None:
            return None
        guild_id, channel_id, message_id = parts
        row = self.session.execute(
            select(DiscordProjection, DiscordDailyThread)
            .join(
                DiscordDailyThread,
                DiscordDailyThread.id == DiscordProjection.daily_thread_id,
            )
            .where(
                DiscordProjection.message_id == message_id,
                DiscordDailyThread.guild_id == guild_id,
                DiscordDailyThread.thread_id == channel_id,
            )
        ).one_or_none()
        if row is None:
            return None
        projection, _daily_thread = row
        primary_ref = projection.primary_public_ref
        if primary_ref is None:
            return None

        case_refs: list[str] = []
        revision_refs: list[str] = []
        brief_ref: str | None = None
        if primary_ref.startswith("case_"):
            case = self.session.scalar(
                select(AttentionCase).where(AttentionCase.ref_id == primary_ref)
            )
            if case is None or projection.primary_revision_ref is None:
                return None
            case_refs = [case.ref_id]
            revision_refs = [projection.primary_revision_ref]
            binding_kind = "attention_case"
        elif primary_ref.startswith("brief_"):
            brief = self.session.scalar(
                select(DailyBrief).where(DailyBrief.ref_id == primary_ref)
            )
            if brief is None:
                return None
            brief_ref = brief.ref_id
            rows = list(
                self.session.execute(
                    select(AttentionCase.ref_id, DailyBriefCaseItem.case_revision_ref)
                    .join(
                        DailyBriefCaseItem,
                        DailyBriefCaseItem.attention_case_id == AttentionCase.id,
                    )
                    .where(DailyBriefCaseItem.brief_id == brief.id)
                    .order_by(DailyBriefCaseItem.display_order)
                )
            )
            case_refs = [case_ref for case_ref, _revision_ref in rows]
            revision_refs = [revision_ref for _case_ref, revision_ref in rows]
            binding_kind = "daily_brief"
        else:
            return None

        context_refs = list(
            dict.fromkeys(
                ref
                for basis_refs in self.session.scalars(
                    select(CaseItem.basis_refs).where(
                        CaseItem.attention_case_id.in_(
                            select(AttentionCase.id).where(
                                AttentionCase.ref_id.in_(case_refs)
                            )
                        )
                    )
                )
                for ref in basis_refs
                if isinstance(ref, str) and ref.startswith("ctx_")
            )
        )
        return {
            "kind": binding_kind,
            "primary_ref": primary_ref,
            "primary_revision_ref": projection.primary_revision_ref,
            "projection_id": str(projection.id),
            "projection_version": projection.projection_version,
            "case_refs": case_refs,
            "case_revision_refs": revision_refs,
            "brief_ref": brief_ref,
            "trusted_context_refs": context_refs,
        }
