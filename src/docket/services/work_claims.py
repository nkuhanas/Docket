"""Reserve conserved work units once, independently of report or invoice wording."""

from dataclasses import dataclass, field
from fractions import Fraction
from itertools import pairwise
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.domain.compensation import allocate
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.models import (
    CompensationEvaluation,
    FinancialDocument,
    FinancialDocumentRevision,
    WorkClaim,
    WorkClaimEvent,
    WorkQuantityAllocation,
    WorkQuantityOrigin,
    WorkSession,
    WorkSessionRevision,
)
from docket.services.work_quantities import WorkQuantityService, WorkSpan, slice_spans


@dataclass
class ClaimPlan:
    document_ref: str
    new: list[WorkClaim] = field(default_factory=list)
    replaced: list[WorkClaim] = field(default_factory=list)
    shared_refs: set[str] = field(default_factory=set)
    flags: set[str] = field(default_factory=set)

    @property
    def refs(self) -> list[str]:
        return [*(row.ref_id for row in self.new), *sorted(self.shared_refs)]


class WorkClaimService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.quantities = WorkQuantityService(session)

    def active(self, origins: list[str], component: str, *, lock: bool = False) -> list[WorkClaim]:
        if lock and origins:
            self.session.scalars(
                select(WorkQuantityOrigin)
                .where(WorkQuantityOrigin.ref_id.in_(origins))
                .order_by(WorkQuantityOrigin.ref_id)
                .with_for_update()
            ).all()
        return list(
            self.session.scalars(
                select(WorkClaim)
                .where(
                    WorkClaim.operator_ref == require_principal("read").operator_ref,
                    WorkClaim.origin_ref.in_(origins),
                    WorkClaim.component_key == component,
                    WorkClaim.status != "released",
                )
                .order_by(WorkClaim.origin_ref, WorkClaim.start_us)
                .execution_options(populate_existing=True)
            )
        )

    def plan(
        self,
        document: FinancialDocument,
        evaluation: CompensationEvaluation,
        changeset_ref: str,
    ) -> ClaimPlan:
        principal = require_principal("commit")
        if (
            document.operator_ref != principal.operator_ref
            or evaluation.operator_ref != principal.operator_ref
            or evaluation.principal_ref != principal.principal_ref
            or evaluation.parameters_json.get("engagement_ref") != document.engagement_ref
            or evaluation.parameters_json.get("component_key") != document.component_key
        ):
            raise DocketError(
                code="financial_basis_unavailable",
                message="Use this connection's owned engagement calculation.",
            )
        plan = ClaimPlan(document_ref=document.ref_id)
        lines: list[dict[str, Any]] = evaluation.result_json["contributions"]
        refs = sorted({line["revision_ref"] for line in lines})
        source_spans: dict[str, list[WorkSpan]] = {ref: [] for ref in refs}
        for offset in range(0, len(refs), 1000):
            for allocation in self.session.scalars(
                select(WorkQuantityAllocation)
                .where(WorkQuantityAllocation.revision_ref.in_(refs[offset : offset + 1000]))
                .order_by(WorkQuantityAllocation.revision_ref, WorkQuantityAllocation.ordinal)
            ):
                source_spans[allocation.revision_ref].append(
                    WorkSpan(
                        allocation.origin_ref,
                        allocation.start_us,
                        allocation.end_us,
                        allocation.needs_review,
                    )
                )
        origins = sorted({span.origin_ref for spans in source_spans.values() for span in spans})
        existing = self.active(origins, document.component_key, lock=True)
        plan.replaced = [
            row
            for row in existing
            if row.document_ref == document.ref_id and row.status == "reserved"
        ]
        ignored = {row.ref_id for row in plan.replaced}
        existing = [row for row in existing if row.ref_id not in ignored]
        for index, line in enumerate(lines):
            raw = line.get("raw_us")
            if (
                line.get("pay_basis") == "unpaid"
                or (line.get("compensable") is False and line.get("billable") is False)
                or not raw
                or set(line.get("flags", []))
                & {
                    "unplaced_time",
                    "cross_period_quantity_unallocated",
                    "open_timer",
                    "quantity_unknown",
                }
            ):
                continue
            retained = evaluation.result_json["retained"][index]
            offset = retained["offset_us"]
            spans = slice_spans(source_spans[line["revision_ref"]], offset, offset + raw)
            # Partition at existing claim boundaries before assigning rounded
            # microseconds. Rounding of a new remainder never claims old units.
            parts: list[tuple[WorkSpan, WorkClaim | None]] = []
            for span in spans:
                covering = [
                    row
                    for row in existing
                    if row.origin_ref == span.origin_ref
                    and row.start_us < span.end
                    and row.end_us > span.start
                ]
                points = sorted(
                    {
                        span.start,
                        span.end,
                        *(
                            point
                            for row in covering
                            for point in (max(span.start, row.start_us), min(span.end, row.end_us))
                        ),
                    }
                )
                for start, end in pairwise(points):
                    owner = next(
                        (row for row in covering if row.start_us <= start < row.end_us), None
                    )
                    if owner and owner.status == "reserved":
                        raise DocketError(
                            code="work_quantity_already_reserved",
                            message="These hours belong to another draft. Read or cancel it.",
                            details={"document_ref": owner.document_ref, "claim_ref": owner.ref_id},
                        )
                    if owner:
                        plan.shared_refs.add(owner.ref_id)
                        if owner.engagement_ref != document.engagement_ref:
                            plan.flags.add("claimed_work_attribution_changed")
                        numerator = retained.get("gross_numerator")
                        if owner.gross_numerator is not None and numerator is not None:
                            assert owner.gross_denominator is not None
                            old = Fraction(int(owner.gross_numerator), int(owner.gross_denominator))
                            new = Fraction(int(numerator), int(retained["gross_denominator"]))
                            if (
                                owner.currency != line.get("currency")
                                or old / (owner.end_us - owner.start_us) != new / raw
                            ):
                                plan.flags.add("claimed_monetary_basis_changed")
                    parts.append((WorkSpan(span.origin_ref, start, end, span.needs_review), owner))
            reportable = line.get("reportable_us")
            rounded = (
                allocate(reportable, [part.quantity for part, _owner in parts])
                if reportable is not None
                else [None] * len(parts)
            )
            for (part, owner), reported in zip(parts, rounded, strict=True):
                if owner:
                    continue
                numerator = retained.get("gross_numerator")
                gross = (
                    Fraction(int(numerator), int(retained["gross_denominator"]))
                    * part.quantity
                    / raw
                    if numerator is not None
                    else None
                )
                plan.new.append(
                    WorkClaim(
                        ref_id=new_public_ref("clm"),
                        operator_ref=principal.operator_ref,
                        origin_ref=part.origin_ref,
                        component_key=document.component_key,
                        engagement_ref=document.engagement_ref,
                        start_us=part.start,
                        end_us=part.end,
                        source_revision_ref=line["revision_ref"],
                        evaluation_ref=evaluation.ref_id,
                        evaluation_line=index,
                        document_ref=document.ref_id,
                        reportable_us=reported,
                        pay_term_ref=line.get("pay_term_ref"),
                        currency=line.get("currency"),
                        gross_numerator=str(gross.numerator) if gross is not None else None,
                        gross_denominator=str(gross.denominator) if gross is not None else None,
                        status="reserved",
                        version=1,
                        created_by_changeset_ref=changeset_ref,
                    )
                )
        return plan

    def record_event(self, claim: WorkClaim, revision: FinancialDocumentRevision) -> None:
        self.session.add(
            WorkClaimEvent(
                claim_ref=claim.ref_id,
                version=claim.version,
                status=claim.status,
                document_revision_ref=revision.ref_id,
                created_by_changeset_ref=revision.created_by_changeset_ref,
            )
        )

    def apply(self, plan: ClaimPlan, revision: FinancialDocumentRevision) -> None:
        # The matching immutable document revision must be assembled first.
        self.session.flush()
        for claim in plan.replaced:
            claim.status, claim.version = "released", claim.version + 1
        self.session.flush()
        for claim in plan.replaced:
            self.record_event(claim, revision)
        self.session.flush()
        for claim in plan.new:
            self.session.add(claim)
            self.session.flush()
            self.record_event(claim, revision)
        self.session.flush()

    def transition(self, document: FinancialDocument, revision: FinancialDocumentRevision) -> None:
        target = {"submitted": "reported", "issued": "issued", "cancelled": "released"}.get(
            document.status
        )
        if target is None:
            return
        claims = self.session.scalars(
            select(WorkClaim)
            .where(
                WorkClaim.document_ref == document.ref_id,
                WorkClaim.status == "reserved",
            )
            .order_by(WorkClaim.origin_ref, WorkClaim.start_us)
            .with_for_update()
        ).all()
        for claim in claims:
            claim.status, claim.version = target, claim.version + 1
        self.session.flush()
        for claim in claims:
            self.record_event(claim, revision)
        self.session.flush()

    def availability(self, revision_ref: str, component: str = "base") -> dict[str, int]:
        owned = self.session.scalar(
            select(WorkSessionRevision.ref_id)
            .join(WorkSession, WorkSession.ref_id == WorkSessionRevision.work_ref)
            .where(
                WorkSessionRevision.ref_id == revision_ref,
                WorkSession.operator_ref == require_principal("read").operator_ref,
            )
        )
        if owned is None:
            raise DocketError(code="work_quantity_unavailable", message="Select owned work.")
        spans = self.quantities.spans(revision_ref)
        claims = self.active(sorted({span.origin_ref for span in spans}), component)
        result = {
            "known_us": sum(span.quantity for span in spans),
            "reserved_us": 0,
            "reported_us": 0,
            "issued_us": 0,
        }
        for span in spans:
            for claim in claims:
                if claim.origin_ref == span.origin_ref:
                    overlap = max(0, min(span.end, claim.end_us) - max(span.start, claim.start_us))
                    result[claim.status + "_us"] += overlap
        result["available_us"] = result["known_us"] - sum(
            result[key] for key in ("reserved_us", "reported_us", "issued_us")
        )
        return result

    def context(self, revision_ref: str) -> dict[str, Any] | None:
        spans = self.quantities.spans(revision_ref)
        if not spans:
            return None
        components = list(
            self.session.scalars(
                select(WorkClaim.component_key)
                .where(
                    WorkClaim.operator_ref == require_principal("read").operator_ref,
                    WorkClaim.origin_ref.in_({span.origin_ref for span in spans}),
                )
                .distinct()
                .order_by(WorkClaim.component_key)
                .limit(6)
            )
        )
        if not components:
            return None
        return {
            "components": [
                {"component_key": key, **self.availability(revision_ref, key)}
                for key in components[:5]
            ],
            "more_components": len(components) > 5,
        }
