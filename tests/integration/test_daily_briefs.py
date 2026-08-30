from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from docket.config import get_settings
from docket.models import (
    AttentionCase,
    AttentionCaseRevision,
    BriefEntry,
    DailyBrief,
    DailyBriefCaseMembership,
    DailyBriefEntryMembership,
    OperatorProjection,
    OperatorUtterance,
    OutboxEvent,
    ProjectionDelivery,
    TriageRun,
)
from docket.providers.discord import FakeDiscordProjectionAdapter
from docket.services.briefs import DailyBriefService
from docket.services.discord_projection import DiscordProjectionRunner
from docket.services.reply_bindings import ReplyBindingService


def _settings():
    return get_settings().model_copy(
        update={
            "timezone": "UTC",
            "waking_window_start_hour": 8,
            "waking_window_end_hour": 1,
        }
    )


def _triage_run(session) -> TriageRun:
    run = TriageRun(
        claimed_by="test",
        contract_version="test",
        contract_hash="a" * 64,
    )
    session.add(run)
    session.flush()
    return run


def _case(session, *, observed_at: datetime, status: str = "open") -> AttentionCase:
    case = AttentionCase(
        situation_key=hashlib.sha256(observed_at.isoformat().encode()).hexdigest(),
        title="Overnight decision",
        summary="One concrete unresolved consequence.",
        status=status,
        semantic_classes=["action_request"],
        source_refs=["src_01M16BRIEFCASE000000000000"],
        first_observed_at=observed_at,
        last_observed_at=observed_at,
        resolved_at=observed_at if status == "resolved" else None,
    )
    session.add(case)
    session.flush()
    revision = AttentionCaseRevision(
        attention_case_id=case.id,
        case_ref=case.ref_id,
        revision=1,
        title=case.title,
        summary=case.summary,
        semantic_classes=case.semantic_classes,
        source_refs=case.source_refs,
        admission_rule_ref="triage.canonical_consequence.v1",
        admission_basis_refs=case.source_refs,
        required_case_item_refs=[],
        canonical_consequence_classes=["task_disposition"],
        content_hash="b" * 64,
        created_at=observed_at,
    )
    session.add(revision)
    return case


def _entry(
    session,
    *,
    created_at: datetime,
    disposition: str = "include",
) -> BriefEntry:
    run = _triage_run(session)
    entry = BriefEntry(
        triage_run_id=run.id,
        source_ref="src_01M16BRIEFENTRY0000000000",
        semantic_classes=["informational"],
        title="Useful context",
        summary="A bounded informational update.",
        disposition=disposition,
        reason="explicit_preference" if disposition == "suppress" else None,
        created_at=created_at,
    )
    session.add(entry)
    session.flush()
    return entry


@pytest.mark.integration
def test_overnight_cases_publish_once_in_replyable_morning_brief(session_factory) -> None:
    settings = _settings()
    observed_at = datetime(2026, 8, 30, 3, 0, tzinfo=UTC)
    with session_factory.begin() as session:
        case = _case(session, observed_at=observed_at)
        entry = _entry(session, created_at=observed_at)
        case_ref = case.ref_id
        entry_ref = entry.ref_id

    service = DailyBriefService(session_factory, settings)
    assert service.run_due_once(datetime(2026, 8, 30, 8, 1, tzinfo=UTC)) is True
    assert service.run_due_once(datetime(2026, 8, 30, 8, 2, tzinfo=UTC)) is False

    with session_factory() as session:
        brief = session.scalar(select(DailyBrief))
        projection = session.scalar(select(OperatorProjection))
        delivery = session.scalar(select(ProjectionDelivery))
        assert brief is not None and brief.brief_kind == "morning"
        assert brief.case_refs == [case_ref]
        assert projection is not None and projection.brief_ref == brief.ref_id
        assert projection.projection_kind == "daily_brief"
        assert delivery is not None and delivery.status == "pending"
        assert session.scalar(select(func.count(DailyBriefCaseMembership.id))) == 1
        assert session.scalar(select(func.count()).select_from(DailyBriefEntryMembership)) == 1
        included_entry = session.scalar(
            select(BriefEntry).where(BriefEntry.ref_id == entry_ref)
        )
        assert included_entry is not None
        assert included_entry.included_brief_ref == brief.ref_id
        assert session.scalar(select(func.count(OutboxEvent.id))) == 1
        projection_ref = projection.ref_id
        brief_ref = brief.ref_id

    adapter = FakeDiscordProjectionAdapter()
    assert DiscordProjectionRunner(session_factory, adapter, settings).run_due_once() is True
    with session_factory.begin() as session:
        delivery = session.scalar(
            select(ProjectionDelivery).where(
                ProjectionDelivery.projection_ref == projection_ref
            )
        )
        assert delivery is not None and delivery.external_message_ref is not None
        thread_id = delivery.external_message_ref.split(":")[2]
        assert thread_id != settings.queue_channel_id
        reply_text = "Handle the first case."
        reply = OperatorUtterance(
            actor_ref=f"discord_user:{settings.operator_discord_user_id}",
            transport="discord",
            source_message_ref=(
                f"discord_message:{settings.discord_guild_id}:{thread_id}:"
                "1542799000000000601"
            ),
            conversation_ref=f"discord_conversation:{settings.discord_guild_id}:{thread_id}",
            reply_to_source_ref=delivery.external_message_ref,
            said_at=datetime.now(UTC),
            verbatim_text=reply_text,
            content_hash=hashlib.sha256(reply_text.encode()).hexdigest(),
            request_key="daily-brief-reply",
        )
        session.add(reply)
        session.flush()
        binding = ReplyBindingService(session).resolve(reply)
        assert binding is not None
        assert binding["brief_ref"] == brief_ref
        assert binding["case_refs"] == [case_ref]


@pytest.mark.integration
def test_night_brief_includes_resolved_cases_and_suppressed_entries(session_factory) -> None:
    settings = _settings()
    observed_at = datetime(2026, 8, 29, 20, 0, tzinfo=UTC)
    with session_factory.begin() as session:
        _case(session, observed_at=observed_at, status="resolved")
        _entry(session, created_at=observed_at, disposition="suppress")

    assert DailyBriefService(session_factory, settings).run_due_once(
        datetime(2026, 8, 30, 1, 1, tzinfo=UTC)
    ) is True

    with session_factory() as session:
        brief = session.scalar(select(DailyBrief))
        projection = session.scalar(select(OperatorProjection))
        assert brief is not None and brief.brief_kind == "night"
        assert brief.local_date.isoformat() == "2026-08-29"
        assert projection is not None
        assert "Completed / resolved" in projection.visible_text
        assert "Suppressed" in projection.visible_text
