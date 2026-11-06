"""Private, stable quantity coordinates; clients never construct these allocations."""

from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.errors import DocketError
from docket.models import (
    WorkQuantityAllocation,
    WorkQuantityBasis,
    WorkQuantityOrigin,
    WorkSession,
    WorkSessionRevision,
    WorkTimeComponent,
)
from docket.services.work_composition import component_quantity


@dataclass(frozen=True)
class WorkSpan:
    origin_ref: str
    start: int
    end: int
    needs_review: bool = False

    @property
    def quantity(self) -> int:
        return self.end - self.start


def slice_spans(spans: list[WorkSpan], start: int, end: int) -> list[WorkSpan]:
    """Select quantity offsets while preserving their original identity."""
    result: list[WorkSpan] = []
    cursor = 0
    for span in spans:
        lo, hi = max(start, cursor), min(end, cursor + span.quantity)
        if hi > lo:
            result.append(
                replace(span, start=span.start + lo - cursor, end=span.start + hi - cursor)
            )
        cursor += span.quantity
    if start < 0 or end < start or end > cursor:
        raise ValueError("Work quantity slice is outside its basis")
    return result


def uncovered(span: WorkSpan, seen: list[WorkSpan]) -> list[WorkSpan]:
    pieces = [span]
    for prior in seen:
        if prior.origin_ref != span.origin_ref:
            continue
        remaining: list[WorkSpan] = []
        for piece in pieces:
            if prior.end <= piece.start or prior.start >= piece.end:
                remaining.append(piece)
            else:
                if piece.start < prior.start:
                    remaining.append(replace(piece, end=prior.start))
                if piece.end > prior.end:
                    remaining.append(replace(piece, start=prior.end))
        pieces = remaining
    return pieces


def compact_spans(spans: list[WorkSpan]) -> list[WorkSpan]:
    result: list[WorkSpan] = []
    for span in spans:
        if (
            result
            and result[-1].origin_ref == span.origin_ref
            and result[-1].end == span.start
            and result[-1].needs_review == span.needs_review
        ):
            result[-1] = replace(result[-1], end=span.end)
        else:
            result.append(span)
    return result


class WorkQuantityService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def spans(self, revision_ref: str) -> list[WorkSpan]:
        rows = self.session.scalars(
            select(WorkQuantityAllocation)
            .where(WorkQuantityAllocation.revision_ref == revision_ref)
            .order_by(WorkQuantityAllocation.ordinal)
        )
        return [WorkSpan(r.origin_ref, r.start_us, r.end_us, r.needs_review) for r in rows]

    def pool(self, work_ref: str) -> list[WorkSpan]:
        rows = self.session.scalars(
            select(WorkQuantityAllocation)
            .join(
                WorkSessionRevision,
                WorkSessionRevision.ref_id == WorkQuantityAllocation.revision_ref,
            )
            .where(WorkSessionRevision.work_ref == work_ref)
            .order_by(WorkSessionRevision.revision, WorkQuantityAllocation.ordinal)
        )
        result: list[WorkSpan] = []
        for row in rows:
            span = WorkSpan(row.origin_ref, row.start_us, row.end_us, row.needs_review)
            result = compact_spans(result + uncovered(span, result))
        return result

    def ensure_history(self, head: WorkSession, changeset_ref: str) -> None:
        """Initialize legacy revisions on an authorized mutation, never during migration/read."""
        rows = self.session.scalars(
            select(WorkSessionRevision)
            .where(WorkSessionRevision.work_ref == head.ref_id)
            .order_by(WorkSessionRevision.revision)
        ).all()
        for row in rows:
            if self.session.get(WorkQuantityBasis, row.ref_id) is None:
                self.record(head, row, changeset_ref)

    def origin(self, head: WorkSession, changeset_ref: str) -> WorkQuantityOrigin:
        origin = self.session.scalar(
            select(WorkQuantityOrigin)
            .where(WorkQuantityOrigin.source_work_ref == head.ref_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if origin is None:
            origin = WorkQuantityOrigin(
                operator_ref=head.operator_ref,
                source_work_ref=head.ref_id,
                next_offset_us=0,
                created_by_changeset_ref=changeset_ref,
            )
            self.session.add(origin)
            self.session.flush()
        return origin

    def retired_quantity(self, origin: WorkQuantityOrigin) -> bool:
        """Unowned historical units can require a later financial allocation decision."""
        rows = self.session.scalars(
            select(WorkQuantityAllocation)
            .join(
                WorkSessionRevision,
                WorkSessionRevision.ref_id == WorkQuantityAllocation.revision_ref,
            )
            .join(WorkSession, WorkSession.ref_id == WorkSessionRevision.work_ref)
            .where(
                WorkQuantityAllocation.origin_ref == origin.ref_id,
                WorkSession.superseded.is_(False),
            )
        )
        seen: list[WorkSpan] = []
        for row in rows:
            seen.extend(uncovered(WorkSpan(row.origin_ref, row.start_us, row.end_us), seen))
        return sum(s.quantity for s in seen) < origin.next_offset_us

    def record(
        self,
        head: WorkSession,
        revision: WorkSessionRevision,
        changeset_ref: str,
        *,
        spans: list[WorkSpan] | None = None,
    ) -> None:
        if self.session.get(WorkQuantityBasis, revision.ref_id) is not None:
            raise ValueError("Work quantity basis already exists")
        quantity = revision.duration_us
        has_unknown = quantity is None
        if revision.time_precision == "composite":
            refs = list(
                self.session.scalars(
                    select(WorkTimeComponent.source_revision_ref)
                    .where(
                        WorkTimeComponent.revision_ref == revision.ref_id,
                    )
                    .order_by(WorkTimeComponent.ordinal)
                )
            )
            quantity, has_unknown = component_quantity(self.session, refs)
        if spans is None:
            pool = self.pool(head.ref_id)
            refs = sorted({s.origin_ref for s in pool})
            origins = self.session.scalars(
                select(WorkQuantityOrigin)
                .where(WorkQuantityOrigin.ref_id.in_(refs))
                .order_by(WorkQuantityOrigin.ref_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
            available = sum(s.quantity for s in pool)
            spans = slice_spans(pool, 0, min(quantity or 0, available))
            if quantity is not None and quantity > available:
                origin = origins[0] if len(origins) == 1 else self.origin(head, changeset_ref)
                review = len(origins) > 1 or self.retired_quantity(origin)
                start = origin.next_offset_us
                if start + quantity - available > 2**63 - 1:
                    raise DocketError(
                        code="work_quantity_capacity",
                        message="This work history exceeds supported quantity precision.",
                        details={"field": "duration_minutes"},
                    )
                origin.next_offset_us += quantity - available
                self.session.flush()
                spans.append(WorkSpan(origin.ref_id, start, origin.next_offset_us, review))
        spans = compact_spans(spans)
        if sum(s.quantity for s in spans) != (quantity or 0):
            raise DocketError(
                code="work_quantity_mismatch", message="Work quantity is not conserved."
            )
        seen: list[WorkSpan] = []
        for span in spans:
            if (
                span.quantity <= 0
                or sum(p.quantity for p in uncovered(span, seen)) != span.quantity
            ):
                raise DocketError(
                    code="work_quantity_overlap", message="Work quantity is assigned twice."
                )
            seen.append(span)
        basis = WorkQuantityBasis(
            revision_ref=revision.ref_id,
            status="unknown"
            if has_unknown
            else "review"
            if any(s.needs_review for s in spans)
            else "resolved",
            created_by_changeset_ref=changeset_ref,
            sealed=False,
        )
        self.session.add(basis)
        self.session.flush()
        for ordinal, span in enumerate(spans):
            self.session.add(
                WorkQuantityAllocation(
                    revision_ref=revision.ref_id,
                    ordinal=ordinal,
                    origin_ref=span.origin_ref,
                    start_us=span.start,
                    end_us=span.end,
                    needs_review=span.needs_review,
                )
            )
        self.session.flush()
        basis.sealed = True
        self.session.flush()
