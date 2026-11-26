"""Per-candidate review and durable expense-inbox transitions."""

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.config import Settings
from docket.models.financial_intake import CandidateDecision, FinancialCandidate, FinancialIntake
from docket.services.financial_files import rejected
from docket.services.notifications import NotificationService


def locked_candidate(session: Session, ref: str, versions: dict[str, int]) -> FinancialCandidate:
    from docket.services.financial_review import lock_review

    owner = require_principal("commit").operator_ref
    lock_review(session, owner)
    candidate = session.scalar(
        select(FinancialCandidate)
        .join(FinancialIntake)
        .where(
            FinancialCandidate.ref_id == ref,
            FinancialIntake.operator_ref == owner,
            FinancialIntake.declarations["evidence_role"].as_string().is_(None),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if candidate is None:
        raise rejected("candidate_unavailable")
    if versions.get(ref) != candidate.version:
        raise rejected("candidate_version_conflict")
    return candidate


def wake(session: Session, settings: Settings, candidate: FinancialCandidate) -> None:
    NotificationService(session, settings).publish(
        "docket.finance.attention",
        candidate.source_ref,
        key=f"expense-inbox:{candidate.ref_id}:{candidate.version}",
    )


def transition(
    session: Session,
    candidate: FinancialCandidate,
    state: str,
    decision: str,
    note: str | None,
    changeset: str | None,
    actor: str,
) -> None:
    candidate.version += 1
    candidate.review_state = state
    candidate.changeset_ref = changeset
    candidate.review_reason = state if state in ("needs_review", "awaiting_operator") else None
    session.add(
        CandidateDecision(
            candidate_ref=candidate.ref_id,
            version=candidate.version,
            decision=decision,
            note=note,
            changeset_ref=changeset,
            actor=actor,
        )
    )
    session.flush()


def review_decision(
    session: Session,
    settings: Settings,
    candidate: FinancialCandidate,
    decision: str,
    note: str | None,
    changeset: str,
) -> None:
    moves = {
        "ask": ({"needs_review"}, "awaiting_operator"),
        "answer": ({"awaiting_operator"}, "needs_review"),
        "dismiss": ({"needs_review", "awaiting_operator"}, "dismissed"),
        "reopen": ({"dismissed"}, "needs_review"),
    }
    allowed, state = moves[decision]
    if candidate.expense_ref or candidate.review_state not in allowed:
        raise rejected("candidate_transition_conflict")
    transition(
        session,
        candidate,
        state,
        decision,
        note,
        changeset,
        require_principal("commit").principal_ref,
    )
    if state == "needs_review":
        wake(session, settings, candidate)


def decisions(session: Session, candidate: FinancialCandidate) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(CandidateDecision)
        .where(CandidateDecision.candidate_ref == candidate.ref_id)
        .order_by(CandidateDecision.version.desc())
        .limit(3)
    )
    return [
        dict(
            version=row.version,
            decision=row.decision,
            note=row.note,
            actor=row.actor,
            changeset_ref=row.changeset_ref,
            at=row.created_at.isoformat(),
        )
        for row in rows
    ]


def inbox(session: Session, view: str, cursor: str | None, limit: int) -> dict[str, Any]:
    stmt = (
        select(FinancialCandidate)
        .join(FinancialIntake)
        .where(
            FinancialIntake.operator_ref == require_principal("read").operator_ref,
            FinancialIntake.declarations["evidence_role"].as_string().is_(None),
            FinancialIntake.media_type != "text/csv",
        )
    )
    if view == "outstanding":
        stmt = stmt.where(
            FinancialCandidate.review_state.in_(("needs_review", "awaiting_operator"))
        )
    if cursor:
        if not cursor.startswith("fcand_") or len(cursor) != 32:
            raise rejected("invalid_inbox_cursor")
        stmt = stmt.where(FinancialCandidate.ref_id > cursor)
    limit = min(limit, 20)
    rows = list(session.scalars(stmt.order_by(FinancialCandidate.ref_id).limit(limit + 1)))
    return {
        "ok": True,
        "inbox": view,
        "items": [
            dict(
                candidate_ref=row.ref_id,
                source_ref=row.source_ref,
                version=row.version,
                state=row.review_state,
                anchor=row.facts.get("anchor"),
                classification=row.facts.get("classification"),
                expense_ref=row.expense_ref,
            )
            for row in rows[:limit]
        ],
        "next_cursor": rows[limit - 1].ref_id if len(rows) > limit else None,
    }


def known_purchase(session: Session, candidate: FinancialCandidate) -> str | None:
    """Look up strong source/document identity while the owner's review lock is held."""
    source = session.get(FinancialIntake, candidate.source_ref)
    assert source is not None
    match = (FinancialIntake.digest == source.digest) & (
        FinancialCandidate.facts["anchor"].as_string() == candidate.facts.get("anchor", "file")
    )
    if candidate.purchase_key:
        match = or_(match, FinancialCandidate.purchase_key == candidate.purchase_key)
    return session.scalar(
        select(FinancialCandidate.expense_ref)
        .join(FinancialIntake)
        .where(
            FinancialIntake.operator_ref == source.operator_ref,
            match,
            FinancialCandidate.expense_ref.is_not(None),
        )
        .limit(1)
    )
