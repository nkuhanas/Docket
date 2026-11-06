"""Exact full-set expense aggregates and retained, bounded report continuations."""

import base64
import csv
import io
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any, Literal

from pydantic import Field, ValidationError
from sqlalchemy import Numeric, case, cast, func, insert, literal, or_, select, union_all
from sqlalchemy.orm import Session, aliased

from docket.agent_auth import require_principal
from docket.config import Settings
from docket.domain.errors import DocketError
from docket.domain.work_time import work_zone
from docket.models import (
    Engagement,
    Expense,
    ExpenseObservation,
    ExpenseReportMember,
    ExpenseReportSnapshot,
    ExpenseRevision,
)
from docket.models.base import utc_now
from docket.schemas.common import StrictModel
from docket.schemas.expenses import QueryExpenses
from docket.services.compensation_policies import instant
from docket.services.expenses import DATA_FIELDS, decimal_text
from docket.services.work_attribution import WorkAttributionService

MONEY_FIELDS = (
    "reported_cost",
    "allocated_cost",
    "undated_cost",
    "known_eligible_amount",
    "reported_claim_amount",
    "approved_amount",
)
COUNT_FIELDS = (
    "expense_count",
    "undated_count",
    "eligibility_unknown_count",
    "report_amount_unknown_count",
    "approval_amount_unknown_count",
)
CSV_FIELDS = (
    "record_type",
    "report_ref",
    "as_of",
    "date_from",
    "date_to",
    "currency",
    *MONEY_FIELDS,
    *COUNT_FIELDS,
    "ref",
    "revision_ref",
    "revision",
    "amount",
    "incurred_on",
    "description",
    "vendor",
    "category",
    "engagement_ref",
    "organization_ref",
    "project_ref",
    "payer_ref",
    "attribution_label",
    "reimbursable",
    "reimbursable_amount",
    "receipt_ref",
    "receipt_status",
    "receipt_required",
    "receipt_note",
    "ownership",
    "owner_ref",
    "source_method",
    "uncertainty",
    "submission_status",
    "report_observation_ref",
    "report_basis_ref",
    "report_amount",
    "report_currency",
    "approval_observation_ref",
    "approved_basis_ref",
    "approval_amount",
    "approved_currency",
    "external_source",
    "external_id",
    "report_observed_at",
    "approval_observed_at",
    "basis_refs",
)


class ExpenseCursor(StrictModel):
    report_ref: str = Field(pattern=r"^exrep_[0-9A-HJKMNP-TV-Z]{26}$")
    mode: Literal["summary", "totals", "json", "csv"] = "summary"
    stage: int = Field(default=0, ge=0, le=5)
    currency_after: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    after: str | None = Field(default=None, max_length=40)
    offset: int = Field(default=0, ge=0, le=1_000_000)

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")


def invalid_cursor() -> DocketError:
    return DocketError(
        code="invalid_expense_report_cursor",
        message="Use this connection's returned expense cursor.",
        details={"field": "cursor"},
    )


class DecimalSum:
    def __init__(self) -> None:
        self.nano = 0
        self.seen = False

    def step(self, *args: Any) -> None:
        value = args[0]
        if value is not None:
            self.seen = True
            scaled = Fraction(Decimal(str(value))) * 1_000_000_000
            assert scaled.denominator == 1
            self.nano += int(scaled)

    def finalize(self) -> str | None:
        if not self.seen:
            return None
        sign = "-" if self.nano < 0 else ""
        digits = str(abs(self.nano)).zfill(10)
        return (sign + digits[:-9] + "." + digits[-9:]).rstrip("0").rstrip(".")


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class ExpenseReportService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session, self.settings = session, settings
        self.postgres = session.get_bind().dialect.name == "postgresql"
        if not self.postgres:
            connection = session.connection().connection.driver_connection
            assert isinstance(connection, sqlite3.Connection)
            # sqlite accepts text/null aggregate results; typeshed restricts these to int.
            connection.create_aggregate("docket_decimal_sum", 1, DecimalSum)  # type: ignore[arg-type]

    def capture(self, public: QueryExpenses) -> ExpenseReportSnapshot:
        principal = require_principal("read")
        now = utc_now()
        today = now.astimezone(work_zone(self.settings.timezone)).date()
        first = public.date_from or today.replace(day=1)
        last = public.date_to or (first.replace(day=1) + timedelta(days=32)).replace(day=1)
        as_of = public.as_of or now
        if as_of.tzinfo is None or as_of > now:
            raise DocketError(
                code="invalid_expense_as_of",
                message="Use a past offset timestamp.",
                details={"field": "as_of"},
            )
        if last <= first:
            raise DocketError(
                code="invalid_expense_period",
                message="date_to must follow date_from.",
                details={"field": "date_to"},
            )
        if public.engagement_ref:
            WorkAttributionService(self.session).head(Engagement, public.engagement_ref)
        parameters = public.model_dump(mode="json", exclude={"cursor", "format", "limit", "as_of"})
        parameters.update(
            date_from=first.isoformat(),
            date_to=last.isoformat(),
            calculation_version="expense-report-v1",
        )
        report = ExpenseReportSnapshot(
            principal_ref=principal.principal_ref,
            operator_ref=principal.operator_ref,
            parameters_json=parameters,
            as_of=as_of.astimezone(UTC),
        )
        self.session.add(report)
        self.session.flush()
        r, o = ExpenseRevision, ExpenseObservation
        latest = (
            select(
                r.ref_id,
                func.row_number()
                .over(partition_by=r.expense_ref, order_by=r.revision.desc())
                .label("n"),
            )
            .join(Expense, Expense.ref_id == r.expense_ref)
            .where(Expense.operator_ref == principal.operator_ref, r.created_at <= as_of)
            .subquery()
        )

        def observation(approved: bool = False) -> Any:
            statement = select(o.ref_id).where(
                o.expense_ref == r.expense_ref, o.created_at <= as_of
            )
            if approved:
                statement = statement.where(o.status == "approved")
            return statement.order_by(o.version.desc()).limit(1).correlate(r).scalar_subquery()

        report_observation = observation()
        selected = (
            select(
                literal(report.id), r.expense_ref, r.ref_id, report_observation, observation(True)
            )
            .join(latest, (latest.c.ref_id == r.ref_id) & (latest.c.n == 1))
            .where(
                r.retracted.is_(False),
                or_(
                    (r.incurred_on >= first) & (r.incurred_on < last),
                    literal(public.include_undated) & r.incurred_on.is_(None),
                ),
            )
        )
        for key in (
            "engagement_ref",
            "organization_ref",
            "project_ref",
            "currency",
            "reimbursable",
        ):
            if getattr(public, key) is not None:
                selected = selected.where(getattr(r, key) == getattr(public, key))
        if public.submission_status:
            status = (
                select(o.status)
                .where(o.ref_id == report_observation)
                .correlate(r)
                .scalar_subquery()
            )
            selected = selected.where(
                func.coalesce(status, "not_submitted") == public.submission_status
            )
        self.session.execute(
            insert(ExpenseReportMember).from_select(
                [
                    "report_id",
                    "expense_ref",
                    "revision_ref",
                    "report_observation_ref",
                    "approval_observation_ref",
                ],
                selected,
            )
        )
        report.totals_json = self.totals(report)
        report.sealed_at = utc_now()
        self.session.flush()
        return report

    def owned(self, public: QueryExpenses) -> tuple[ExpenseReportSnapshot, ExpenseCursor]:
        try:
            value = public.cursor or ""
            cursor = ExpenseCursor.model_validate_json(
                base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            )
        except (ValueError, ValidationError) as exc:
            raise invalid_cursor() from exc
        principal = require_principal("read")
        report = self.session.scalar(
            select(ExpenseReportSnapshot).where(
                ExpenseReportSnapshot.ref_id == cursor.report_ref,
                ExpenseReportSnapshot.principal_ref == principal.principal_ref,
                ExpenseReportSnapshot.operator_ref == principal.operator_ref,
                ExpenseReportSnapshot.sealed_at.is_not(None),
            )
        )
        if report is None:
            raise invalid_cursor()
        for key in public.model_fields_set - {"cursor", "format", "limit"}:
            value = getattr(public, key)
            expected = (
                instant(report.as_of).isoformat()
                if key == "as_of"
                else report.parameters_json.get(key)
            )
            serialized = (
                instant(value).isoformat()
                if isinstance(value, datetime)
                else value.isoformat()
                if isinstance(value, date)
                else value
            )
            if expected != serialized:
                raise DocketError(
                    code="expense_cursor_filter_changed",
                    message="Continue the captured filters or start a new query.",
                    details={"field": key},
                )
        if "format" in public.model_fields_set and cursor.mode != public.format:
            if cursor.stage or cursor.after or cursor.offset or cursor.currency_after:
                raise invalid_cursor()
            cursor.mode = public.format
        return report, cursor

    def rows(self, report: ExpenseReportSnapshot, after: str | None = None) -> Any:
        r, m = ExpenseRevision, ExpenseReportMember
        statement = select(r).join(m, m.revision_ref == r.ref_id).where(m.report_id == report.id)
        return statement.where(m.expense_ref > after) if after else statement

    def entries(
        self, report: ExpenseReportSnapshot, after: str | None, limit: int, *, full: bool = False
    ) -> list[dict[str, Any]]:
        r, m = ExpenseRevision, ExpenseReportMember
        reported, approved = aliased(ExpenseObservation), aliased(ExpenseObservation)
        statement = (
            select(r, reported, approved, Expense.external_source, Expense.external_id)
            .join(m, m.revision_ref == r.ref_id)
            .join(Expense, Expense.ref_id == m.expense_ref)
            .outerjoin(reported, reported.ref_id == m.report_observation_ref)
            .outerjoin(approved, approved.ref_id == m.approval_observation_ref)
            .where(m.report_id == report.id)
        )
        if after:
            statement = statement.where(m.expense_ref > after)
        entries = []
        for row, status, approval, external_source, external_id in self.session.execute(
            statement.order_by(m.expense_ref).limit(limit)
        ):
            data = {
                "ref": row.expense_ref,
                "revision_ref": row.ref_id,
                "revision": row.revision,
                **{key: getattr(row, key) for key in DATA_FIELDS},
                "submission_status": status.status if status else "not_submitted",
                "basis_refs": row.basis_refs,
            }
            if not full:
                data["description"] = data["description"][:200]
                data.pop("uncertainty", None)
                data.pop("receipt_note", None)
            else:
                data.update(external_source=external_source, external_id=external_id)
            if status:
                data.update(
                    report_observation_ref=status.ref_id,
                    report_basis_ref=status.basis_revision_ref,
                    report_amount=status.amount,
                    report_currency=status.currency,
                    report_observed_at=instant(status.observed_at).isoformat()
                    if status.observed_at
                    else None,
                )
            if approval:
                data.update(
                    approval_observation_ref=approval.ref_id,
                    approved_basis_ref=approval.basis_revision_ref,
                    approval_amount=approval.amount,
                    approved_currency=approval.currency,
                    approval_observed_at=instant(approval.observed_at).isoformat()
                    if approval.observed_at
                    else None,
                )
            if data["incurred_on"]:
                data["incurred_on"] = data["incurred_on"].isoformat()
            entries.append(data)
        return entries

    def totals(self, report: ExpenseReportSnapshot) -> list[dict[str, Any]]:
        if report.totals_json is not None:
            return report.totals_json
        r, m = ExpenseRevision, ExpenseReportMember
        observed, approved = aliased(ExpenseObservation), aliased(ExpenseObservation)
        base = (
            select(
                r.currency.label("currency"),
                r.amount.label("reported_cost"),
                case((r.incurred_on.is_not(None), r.amount)).label("allocated_cost"),
                case((r.incurred_on.is_(None), r.amount)).label("undated_cost"),
                case(
                    (r.reimbursable.is_(True), func.coalesce(r.reimbursable_amount, r.amount))
                ).label("known_eligible_amount"),
                literal(None).label("reported_claim_amount"),
                literal(None).label("approved_amount"),
                literal(1).label("expense_count"),
                case((r.incurred_on.is_(None), 1), else_=0).label("undated_count"),
                case((r.reimbursable.is_(None), 1), else_=0).label("eligibility_unknown_count"),
                literal(0).label("report_amount_unknown_count"),
                literal(0).label("approval_amount_unknown_count"),
            )
            .join(m, m.revision_ref == r.ref_id)
            .where(m.report_id == report.id)
        )

        def observation_part(model: Any, approval: bool) -> Any:
            values: dict[str, Any] = {key: literal(None) for key in MONEY_FIELDS}
            values["approved_amount" if approval else "reported_claim_amount"] = model.amount
            counts: dict[str, Any] = {key: literal(0) for key in COUNT_FIELDS}
            counts[
                "approval_amount_unknown_count" if approval else "report_amount_unknown_count"
            ] = case((model.amount.is_(None), 1), else_=0)
            return (
                select(
                    model.currency.label("currency"),
                    *(values[key].label(key) for key in MONEY_FIELDS),
                    *(counts[key].label(key) for key in COUNT_FIELDS),
                )
                .select_from(m)
                .join(
                    model,
                    model.ref_id
                    == (m.approval_observation_ref if approval else m.report_observation_ref),
                )
                .where(m.report_id == report.id)
            )

        values = union_all(
            base, observation_part(observed, False), observation_part(approved, True)
        ).subquery()

        def money_sum(column: Any) -> Any:
            return (
                func.sum(cast(column, Numeric()))
                if self.postgres
                else func.docket_decimal_sum(column)
            )

        statement = (
            select(
                values.c.currency,
                *(money_sum(getattr(values.c, key)).label(key) for key in MONEY_FIELDS),
                *(func.sum(getattr(values.c, key)).label(key) for key in COUNT_FIELDS),
            )
            .group_by(values.c.currency)
            .order_by(values.c.currency)
        )
        return [
            {
                "currency": row["currency"],
                **{
                    key: None
                    if row[key] is None
                    and (
                        (key == "reported_claim_amount" and row["report_amount_unknown_count"])
                        or (key == "approved_amount" and row["approval_amount_unknown_count"])
                    )
                    else decimal_text(Decimal(str(row[key] or "0")))
                    for key in MONEY_FIELDS
                },
                **{key: int(row[key] or 0) for key in COUNT_FIELDS},
            }
            for row in self.session.execute(statement).mappings()
        ]

    @staticmethod
    def csv_row(value: dict[str, Any]) -> str:
        output = io.StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerow(
            [
                compact(value.get(key))
                if isinstance(value.get(key), (dict, list))
                else value.get(key, "")
                for key in CSV_FIELDS
            ]
        )
        return output.getvalue()

    def pieces(
        self, base: dict[str, Any], report: ExpenseReportSnapshot, state: ExpenseCursor, limit: int
    ) -> Any:
        common = {
            "report_ref": report.ref_id,
            "as_of": instant(report.as_of).isoformat(),
            "date_from": report.parameters_json["date_from"],
            "date_to": report.parameters_json["date_to"],
        }
        if state.stage == 0:
            header = (
                '{"report":' + compact(base["report"]) + ',"totals":['
                if state.mode == "json"
                else ",".join(CSV_FIELDS) + "\n"
            )
            next_state = state.model_copy(update={"stage": 1, "offset": 0})
            yield header, next_state
            state = next_state
        if state.stage == 1:
            totals = [
                row
                for row in self.totals(report)
                if state.currency_after is None or row["currency"] > state.currency_after
            ][:limit]
            for row in totals:
                value = (
                    ("," if state.currency_after else "") + compact(row)
                    if state.mode == "json"
                    else self.csv_row({**common, "record_type": "currency_total", **row})
                )
                next_state = state.model_copy(
                    update={"currency_after": row["currency"], "offset": 0}
                )
                yield value, next_state
                state = next_state
            if len(totals) == limit:
                return
            state = state.model_copy(update={"stage": 2})
        if state.stage == 2:
            next_state = state.model_copy(update={"stage": 3, "offset": 0})
            yield ('],"entries":[' if state.mode == "json" else ""), next_state
            state = next_state
        if state.stage == 3:
            entries = self.entries(report, state.after, limit, full=True)
            for entry in entries:
                value = (
                    ("," if state.after else "") + compact(entry)
                    if state.mode == "json"
                    else self.csv_row({**common, "record_type": "expense", **entry})
                )
                next_state = state.model_copy(update={"after": entry["ref"], "offset": 0})
                yield value, next_state
                state = next_state
            if len(entries) == limit:
                return
            state = state.model_copy(update={"stage": 4})
        if state.stage == 4:
            yield (
                ("]}\n" if state.mode == "json" else ""),
                state.model_copy(update={"stage": 5, "offset": 0}),
            )

    def query(self, public: QueryExpenses) -> dict[str, Any]:
        if public.cursor:
            report, cursor = self.owned(public)
        else:
            report = self.capture(public)
            cursor = ExpenseCursor(report_ref=report.ref_id, mode=public.format)
        totals = self.totals(report)
        base: dict[str, Any] = {
            "ok": True,
            "report": {
                "ref": report.ref_id,
                **report.parameters_json,
                "as_of": instant(report.as_of).isoformat(),
                "coverage": "reported expenses; undated cost is not assigned to this period; "
                "money subtotals include known amounts only; unknown counts remain explicit",
            },
            "expense_count": sum(row["expense_count"] for row in totals),
            "currency_count": len(totals),
            "export_cursor": ExpenseCursor(report_ref=report.ref_id).encode(),
        }
        if cursor.mode in {"summary", "totals"}:
            selected = [
                row
                for row in totals
                if cursor.currency_after is None or row["currency"] > cursor.currency_after
            ]
            page = selected[: public.limit]
            while len(compact(page).encode()) > 4 * 1024 and len(page) > 1:
                page.pop()
            result: dict[str, Any] = {
                **base,
                "totals": page,
                "totals_complete": len(page) == len(selected),
            }
            if len(page) < len(selected):
                result["totals_next_cursor"] = ExpenseCursor(
                    report_ref=report.ref_id, mode="totals", currency_after=page[-1]["currency"]
                ).encode()
            if cursor.mode == "totals":
                result["complete"] = result["totals_complete"]
                if not result["complete"]:
                    result["next_cursor"] = result["totals_next_cursor"]
                return result
            entries = self.entries(report, cursor.after, public.limit + 1)
            result["entries"] = entries[: public.limit]
            while len(compact(result).encode()) > 14 * 1024 and result["entries"]:
                result["entries"].pop()
            more = len(entries) > len(result["entries"])
            if more and not result["entries"]:
                raise DocketError(
                    code="expense_report_too_large", message="Use JSON export for this report."
                )
            result["complete"] = not more
            if more:
                result["next_cursor"] = ExpenseCursor(
                    report_ref=report.ref_id, after=result["entries"][-1]["ref"]
                ).encode()
            return result
        state = cursor.model_copy()
        result = {**base, "content": "", "format": state.mode}
        for value, next_state in self.pieces(base, report, state, public.limit):
            try:
                raw = value.encode()
                if state.offset > len(raw):
                    raise ValueError("invalid record offset")
                remaining = raw[state.offset :].decode()
            except (ValueError, UnicodeDecodeError) as exc:
                raise invalid_cursor() from exc
            lo, hi = 0, len(remaining)
            while lo < hi:
                middle = (lo + hi + 1) // 2
                if (
                    len(
                        compact(
                            {**result, "content": result["content"] + remaining[:middle]}
                        ).encode()
                    )
                    < 14 * 1024
                ):
                    lo = middle
                else:
                    hi = middle - 1
            result["content"] += remaining[:lo]
            if lo < len(remaining):
                state.offset += len(remaining[:lo].encode())
                break
            state = next_state
        result["complete"] = state.stage == 5
        if not result["complete"]:
            result["next_cursor"] = state.encode()
        return result
