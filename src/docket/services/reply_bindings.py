from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.models import (
    AttentionCase,
    CaseItem,
    DailyBrief,
    DailyBriefCaseMembership,
    OperatorProjection,
    OperatorUtterance,
    ProjectionDelivery,
)


class ReplyBindingService:
    """Resolve a Discord reply to the exact Docket projection revision it displayed."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def resolve(self, utterance: OperatorUtterance) -> dict[str, Any] | None:
        reply_ref = utterance.reply_to_source_ref
        if reply_ref is None or not reply_ref.startswith("discord_message:"):
            return None
        row = self.session.execute(
            select(OperatorProjection, ProjectionDelivery)
            .join(
                ProjectionDelivery,
                ProjectionDelivery.projection_id == OperatorProjection.id,
            )
            .where(
                ProjectionDelivery.transport == "discord",
                ProjectionDelivery.external_message_ref == reply_ref,
                ProjectionDelivery.status == "delivered",
            )
        ).one_or_none()
        if row is None:
            return None
        projection, _delivery = row
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
            revision_ref = projection.case_revision_ref or projection.primary_revision_ref
            if case is None or revision_ref is None:
                return None
            case_refs = [case.ref_id]
            revision_refs = [revision_ref]
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
                    select(AttentionCase.ref_id, DailyBriefCaseMembership.case_revision_ref)
                    .join(
                        DailyBriefCaseMembership,
                        DailyBriefCaseMembership.attention_case_id == AttentionCase.id,
                    )
                    .where(DailyBriefCaseMembership.brief_id == brief.id)
                    .order_by(DailyBriefCaseMembership.display_order)
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
            "projection_ref": projection.ref_id,
            "projection_version": projection.render_schema_version,
            "case_refs": case_refs,
            "case_revision_refs": revision_refs,
            "brief_ref": brief_ref,
            "trusted_context_refs": context_refs,
        }
