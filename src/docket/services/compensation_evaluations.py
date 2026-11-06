"""Freeze work and effective policy basis, then serve bounded deterministic results."""

import base64
import csv
import io
from collections import defaultdict
from datetime import date
from typing import Any, Literal

from pydantic import Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.config import Settings
from docket.domain.compensation import (
    CompensationCalculator,
    Contribution,
    PolicyWindow,
    WorkQuantity,
    money,
    summarize,
)
from docket.domain.errors import DocketError
from docket.domain.work_time import elapsed_us
from docket.models import (
    CompensationEvaluation,
    CompensationPolicy,
    CompensationPolicyRevision,
    CompensationRevisionTerm,
    CompensationTerm,
    Engagement,
    EngagementRevision,
    WorkQuantityAllocation,
    WorkReportMember,
    WorkSegment,
    WorkSessionRevision,
)
from docket.schemas.common import StrictModel
from docket.schemas.compensation import GetCompensation
from docket.schemas.work import QueryWorkSessions
from docket.services.compensation_policies import instant, term_spec
from docket.services.work_attribution import WorkAttributionService
from docket.services.work_composition import time_leaves
from docket.services.work_reports import WorkReportService, compact

CALCULATION_VERSION = "compensation-v1"
MAX_OUTPUT = 16 * 1024


class EvaluationCursor(StrictModel):
    ref: str = Field(pattern=r"^ceval_[0-9A-HJKMNP-TV-Z]{26}$")
    offset: int = Field(default=0, ge=0)
    mode: Literal["summary", "json", "csv"] = "summary"

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")


def unavailable() -> DocketError:
    return DocketError(
        code="compensation_evaluation_unavailable",
        message="Use a calculation cursor returned to this connection.",
        details={"field": "cursor"},
    )


def public_piece(piece: Contribution) -> dict[str, Any]:
    pay = piece.pay.terms if piece.pay else None
    return {
        key: value
        for key, value in {
            "work_ref": piece.source.work_ref,
            "revision_ref": piece.source.revision_ref,
            "source_revision_ref": piece.source.source_revision_ref,
            "engagement_ref": piece.source.engagement_ref,
            "start": piece.start.isoformat() if piece.start else None,
            "end": piece.end.isoformat() if piece.end else None,
            "day": piece.day.isoformat() if piece.day else None,
            "raw_us": piece.raw_us,
            "reportable_us": piece.reportable_us,
            "compensable": pay.compensable if pay else None,
            "billable": pay.billable if pay else None,
            "reported_compensation_class": piece.source.reported_class,
            "overtime_us": piece.overtime_us,
            "pay_term_ref": piece.pay.ref if piece.pay else None,
            "reporting_term_ref": piece.rules.ref if piece.rules else None,
            "currency": pay.currency if pay else None,
            "gross_line_estimate": money(piece.gross, pay.money_decimals or 0)
            if pay and piece.gross is not None
            else None,
            "flags": sorted(piece.flags),
        }.items()
        if value is not None
    }


class CompensationEvaluationService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session, self.settings = session, settings

    def sources(self, report_id: Any) -> list[WorkQuantity]:
        r, member = WorkSessionRevision, WorkReportMember
        roots = self.session.execute(
            select(r, EngagementRevision)
            .join(member, member.revision_ref == r.ref_id)
            .outerjoin(
                EngagementRevision, EngagementRevision.ref_id == member.engagement_revision_ref
            )
            .where(member.report_id == report_id)
        ).all()
        root_map = {row[0].ref_id: row for row in roots}
        references = (
            select(member.revision_ref.label("ref_id"))
            .where(member.report_id == report_id)
            .subquery()
        )
        leaves = time_leaves(references)
        selected = self.session.execute(
            select(leaves.c.root_ref, r)
            .join(r, r.ref_id == leaves.c.ref_id)
            .order_by(leaves.c.root_ref, leaves.c.path)
        ).all()
        # One batched query, independent of the number of sessions/years.
        segments: dict[Any, list[WorkSegment]] = defaultdict(list)
        for segment in self.session.scalars(
            select(WorkSegment)
            .where(WorkSegment.revision_id.in_(select(leaves.c.id)), WorkSegment.kind == "active")
            .order_by(WorkSegment.revision_id, WorkSegment.ordinal)
        ):
            segments[segment.revision_id].append(segment)
        review = set(
            self.session.scalars(
                select(WorkQuantityAllocation.revision_ref).where(
                    WorkQuantityAllocation.revision_ref.in_(references.select()),
                    WorkQuantityAllocation.needs_review.is_(True),
                )
            )
        )
        offsets: dict[str, int] = defaultdict(int)
        sources = []
        for root_ref, leaf in selected:
            root, engagement = root_map[root_ref]
            common: dict[str, Any] = {
                "work_ref": root.work_ref,
                "revision_ref": root.ref_id,
                "source_revision_ref": leaf.ref_id,
                "engagement_ref": root.engagement_ref,
                "reported_class": root.reported_compensation_class,
                "timezone": leaf.timezone,
                "quantity_needs_review": root_ref in review,
                "engagement_from": engagement.effective_start if engagement else None,
                "engagement_to": engagement.effective_end if engagement else None,
                "open_timer": root.lifecycle in {"running", "paused"},
            }
            active = segments[leaf.id]
            if leaf.time_precision in {"interval", "timer"} and active:
                for segment in active:
                    start = instant(segment.start_at)
                    end = instant(segment.end_at) if segment.end_at else None
                    quantity = elapsed_us(end - start) if end else None
                    sources.append(
                        WorkQuantity(
                            **common,
                            segment_key=f"{leaf.ref_id}:{segment.ordinal}",
                            offset_us=offsets[root_ref],
                            quantity_us=quantity,
                            start=start,
                            end=end,
                        )
                    )
                    offsets[root_ref] += quantity or 0
            else:
                sources.append(
                    WorkQuantity(
                        **common,
                        segment_key=leaf.ref_id,
                        offset_us=offsets[root_ref],
                        quantity_us=leaf.duration_us,
                        day=leaf.work_date,
                        period_start=leaf.period_start,
                        period_end=leaf.period_end,
                    )
                )
                offsets[root_ref] += leaf.duration_us or 0
        return sources

    def policies(
        self, as_of: Any, component: str
    ) -> tuple[dict[str, list[PolicyWindow]], dict[str, list[PolicyWindow]], list[dict[str, Any]]]:
        principal = require_principal("read")
        p, r = CompensationPolicy, CompensationPolicyRevision
        latest = (
            select(
                r.ref_id,
                func.row_number()
                .over(partition_by=r.policy_ref, order_by=r.revision.desc())
                .label("n"),
            )
            .join(p, p.ref_id == r.policy_ref)
            .where(
                p.operator_ref == principal.operator_ref,
                r.created_at <= as_of,
                r.sealed.is_(True),
                ((p.kind == "pay") & (p.component_key == component)) | (p.kind == "reporting"),
            )
            .subquery()
        )
        rows = self.session.execute(
            select(p.engagement_ref, p.kind, r.ref_id, CompensationTerm)
            .join(r, r.policy_ref == p.ref_id)
            .join(latest, (latest.c.ref_id == r.ref_id) & (latest.c.n == 1))
            .join(CompensationRevisionTerm, CompensationRevisionTerm.revision_ref == r.ref_id)
            .join(CompensationTerm, CompensationTerm.ref_id == CompensationRevisionTerm.term_ref)
            .order_by(p.ref_id, CompensationRevisionTerm.ordinal)
        ).all()
        pay: dict[str, list[PolicyWindow]] = defaultdict(list)
        reporting: dict[str, list[PolicyWindow]] = defaultdict(list)
        basis = []
        for engagement, kind, revision_ref, term in rows:
            spec = term_spec(term)
            window = PolicyWindow(term.ref_id, revision_ref, spec)
            (pay if kind == "pay" else reporting)[engagement].append(window)
            basis.append(
                {
                    "term_ref": term.ref_id,
                    "revision_ref": revision_ref,
                    "engagement_ref": engagement,
                    "terms": spec.model_dump(mode="json"),
                }
            )
        return pay, reporting, basis

    def capture(self, public: GetCompensation) -> CompensationEvaluation:
        principal = require_principal("read")
        reports = WorkReportService(self.session, self.settings)
        parameters, as_of = reports._parameters(
            QueryWorkSessions(
                date_from=public.date_from,
                date_to=public.date_to,
                timezone=public.timezone,
                as_of=public.as_of,
            )
        )
        first, last = (
            date.fromisoformat(parameters["date_from"]),
            date.fromisoformat(parameters["date_to"]),
        )
        if public.engagement_ref:
            WorkAttributionService(self.session).head(Engagement, public.engagement_ref)
        # Include complete rule periods/week and source sessions before filtering the
        # requested result. The caller never assembles employer-wide joins or sums.
        lower = date.fromordinal(max(1, first.toordinal() - 367))
        upper = date.fromordinal(min(date.max.toordinal(), last.toordinal() + 367))
        report = reports._capture(
            QueryWorkSessions(
                date_from=lower,
                date_to=upper,
                timezone=parameters["timezone"],
                as_of=as_of,
            )
        )
        pay, reporting, basis = self.policies(as_of, public.component_key)
        pieces = CompensationCalculator(
            pay, reporting, first, last, parameters["timezone"]
        ).evaluate(self.sources(report.id))
        for piece in pieces:
            if public.engagement_ref and piece.source.engagement_ref != public.engagement_ref:
                piece.included = False
        included = [piece for piece in pieces if piece.included]
        summary = summarize(included)
        public_rows = [public_piece(piece) for piece in included]
        parameters = {key: parameters[key] for key in ("date_from", "date_to", "timezone")}
        parameters.update(
            engagement_ref=public.engagement_ref,
            component_key=public.component_key,
            as_of=as_of.isoformat(),
        )
        # Exact rational amounts and root quantity offsets are private retained
        # calculation data for later claims, not a client-constructed graph.
        retained = [
            {
                "offset_us": piece.offset_us,
                "gross_numerator": str(piece.gross.numerator) if piece.gross is not None else None,
                "gross_denominator": str(piece.gross.denominator)
                if piece.gross is not None
                else None,
            }
            for piece in included
        ]
        row = CompensationEvaluation(
            principal_ref=principal.principal_ref,
            operator_ref=principal.operator_ref,
            work_report_ref=report.ref_id,
            calculation_version=CALCULATION_VERSION,
            parameters_json=parameters,
            result_json={
                "summary": summary,
                "contributions": public_rows,
                "basis": basis,
                "retained": retained,
            },
        )
        self.session.add(row)
        self.session.flush()
        return row

    def resume(self, public: GetCompensation) -> tuple[CompensationEvaluation, EvaluationCursor]:
        try:
            encoded = public.cursor or ""
            cursor = EvaluationCursor.model_validate_json(
                base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            )
        except (ValueError, ValidationError) as exc:
            raise unavailable() from exc
        principal = require_principal("read")
        row = self.session.scalar(
            select(CompensationEvaluation).where(
                CompensationEvaluation.ref_id == cursor.ref,
                CompensationEvaluation.principal_ref == principal.principal_ref,
                CompensationEvaluation.operator_ref == principal.operator_ref,
            )
        )
        if row is None:
            raise unavailable()
        for key in public.model_fields_set - {"cursor", "format", "limit"}:
            value = getattr(public, key)
            serialized = value.isoformat() if isinstance(value, date) else value
            if row.parameters_json.get(key) != serialized:
                raise DocketError(
                    code="compensation_cursor_filter_mismatch",
                    message="Continue with the returned cursor or start a new query.",
                    details={"field": key},
                )
        if "format" in public.model_fields_set and public.format != cursor.mode:
            if cursor.offset:
                raise unavailable()
            cursor.mode = public.format
        return row, cursor

    def query(self, public: GetCompensation) -> dict[str, Any]:
        if public.cursor:
            row, cursor = self.resume(public)
        else:
            row = self.capture(public)
            cursor = EvaluationCursor(ref=row.ref_id, mode=public.format)
        result: dict[str, Any] = {
            "ok": True,
            "evaluation_ref": row.ref_id,
            "parameters": row.parameters_json,
            "calculation_version": row.calculation_version,
            "work_report_ref": row.work_report_ref,
            "summary": row.result_json["summary"],
        }
        entries = row.result_json["contributions"]
        result["contribution_count"] = len(entries)
        # Return a zero-offset export handle even when the summary fits one page.
        result["export_cursor"] = EvaluationCursor(ref=row.ref_id).encode()
        if cursor.mode == "summary":
            page = entries[cursor.offset : cursor.offset + public.limit]
            result["contributions"] = page
            while len(compact(result).encode()) > MAX_OUTPUT - 1024 and page:
                page.pop()
            if not page and cursor.offset < len(entries):
                raise DocketError(
                    code="compensation_result_too_large",
                    message="Use JSON export for this calculation.",
                )
            next_offset = cursor.offset + len(page)
            if next_offset < len(entries):
                result["next_cursor"] = EvaluationCursor(
                    ref=row.ref_id, offset=next_offset
                ).encode()
            result["complete"] = next_offset == len(entries)
            result["policy_basis_count"] = len(row.result_json["basis"])
        else:
            if cursor.mode == "json":
                content = compact(
                    {**result, "contributions": entries, "policy_basis": row.result_json["basis"]}
                )
            else:
                stream = io.StringIO(newline="")
                fields = sorted({key for entry in entries for key in entry})
                metadata = {
                    "evaluation_ref": row.ref_id,
                    "calculation_version": row.calculation_version,
                    **{
                        key: row.parameters_json[key]
                        for key in ("date_from", "date_to", "timezone", "as_of")
                    },
                }
                writer = csv.DictWriter(stream, fieldnames=[*metadata, *fields])
                writer.writeheader()
                for entry in entries:
                    writer.writerow(
                        {
                            **metadata,
                            **{
                                key: compact(value) if isinstance(value, (dict, list)) else value
                                for key, value in entry.items()
                            },
                        }
                    )
                content = stream.getvalue()
            encoded = content.encode()
            offset = cursor.offset
            if offset > len(encoded):
                raise unavailable()
            end = min(len(encoded), offset + 8192)
            while end < len(encoded) and encoded[end] & 0xC0 == 0x80:
                end -= 1
            result["content"] = encoded[offset:end].decode()
            result["format"] = cursor.mode
            result["byte_offset"] = offset
            result["complete"] = end == len(encoded)
            if end < len(encoded):
                result["next_cursor"] = EvaluationCursor(
                    ref=row.ref_id, offset=end, mode=cursor.mode
                ).encode()
        return result
