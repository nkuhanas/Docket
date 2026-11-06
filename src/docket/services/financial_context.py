"""Bounded financial context and exact, private, retained JSON/CSV reports."""

import base64
import csv
import io
import json
from fractions import Fraction
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.config import Settings
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.models import FinancialReportSnapshot, PaymentEvent
from docket.models.base import utc_now
from docket.schemas.settlement import GetFinancialContext
from docket.services.compensation_policies import instant
from docket.services.financial_state import FinancialState, amount_text
from docket.services.native_context import NativeContextService
from docket.services.settlement import SettlementService

AMOUNTS = (
    "gross_claim",
    "credits",
    "deductions",
    "cash_applied",
    "outstanding",
    "received",
    "unapplied",
    "work_claim_gross",
    "expense_claim_gross",
    "reported_wages_received",
    "reported_contract_received",
    "reported_reimbursement_received",
    "unclassified_received",
    "unmatched_allocated_cash",
)


def encode_cursor(ref: str, representation: str, position: int) -> str:
    return base64.urlsafe_b64encode(json.dumps([ref, representation, position]).encode()).decode()


class FinancialContextService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def capture(self, query: GetFinancialContext) -> FinancialReportSnapshot:
        principal = require_principal("read")
        if self.session.get_bind().dialect.name == "postgresql":
            # Claims and receipts are read by separate queries. Capture one
            # transaction snapshot rather than mixing concurrent commits.
            self.session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        as_of = query.as_of or utc_now()
        if as_of.tzinfo is None or as_of > utc_now():
            raise DocketError(
                code="invalid_financial_as_of", message="Use a past offset as_of timestamp."
            )
        as_of = instant(as_of)
        service = SettlementService(self.session)
        if query.engagement_ref:
            from docket.models import Engagement
            from docket.services.work_attribution import WorkAttributionService

            WorkAttributionService(self.session).head(Engagement, query.engagement_ref)
        state = FinancialState(self.session, as_of=as_of)
        selected_targets = {
            ref
            for ref, data in state.sources.targets.items()
            if not query.engagement_ref or data.get("engagement_ref") == query.engagement_ref
        }
        selected_payments = {
            ref
            for ref, row in state.payments.items()
            if not query.engagement_ref or row.engagement_ref == query.engagement_ref
        }
        selected_entries = set(state.by_ref)
        if query.refs:
            from docket.models import FinancialEntry, FinancialTransaction, Payment

            for ref in query.refs:
                if ref.startswith(("fdoc_", "exp_")):
                    service.target(ref)
                else:
                    service.owned(
                        Payment
                        if ref.startswith("pay_")
                        else (FinancialEntry if ref.startswith("fent_") else FinancialTransaction),
                        ref,
                    )
            selected_targets &= set(query.refs) | {
                row.target_ref
                for row in state.entries
                if row.ref_id in query.refs
                or row.transaction_ref in query.refs
                or row.payment_ref in query.refs
            }
            selected_entries = {
                row.ref_id
                for row in state.entries
                if row.ref_id in query.refs
                or row.transaction_ref in query.refs
                or row.payment_ref in query.refs
                or row.target_ref in selected_targets
            }
            selected_payments &= set(query.refs) | {
                row.payment_ref for row in state.entries if row.ref_id in selected_entries
            }
        elif query.engagement_ref:
            selected_entries = {
                row.ref_id
                for row in state.entries
                if row.target_ref in selected_targets or row.payment_ref in selected_payments
            }
            selected_payments |= {
                row.payment_ref
                for row in state.entries
                if row.ref_id in selected_entries and row.payment_ref
            }
        scopes = [scope for ref in selected_targets for scope in state.sources.scopes(ref)]
        part_refs = {p.ref for p in state.parts if any(scope.covers(p) for scope in scopes)}
        by_kind = {
            kind: {
                part.ref: sum(
                    (
                        matches.get(part.ref, Fraction(0))
                        for ref, matches in state.match.matches.items()
                        if state.by_ref[ref].kind == kind
                    ),
                    Fraction(0),
                )
                for part in state.parts
            }
            for kind in ("credit", "deduction", "cash")
        }
        totals: dict[str, dict[str, Fraction]] = {}

        def currency(code: str) -> dict[str, Fraction]:
            return totals.setdefault(code, dict.fromkeys(AMOUNTS, Fraction(0)))

        for part in state.parts:
            if part.ref not in part_refs:
                continue
            bucket = currency(part.currency)
            bucket["gross_claim"] += part.amount
            category = state.primary[part.ref].category
            bucket[
                "expense_claim_gross" if category == "reimbursement" else "work_claim_gross"
            ] += part.amount
            for key, kind in (
                ("credits", "credit"),
                ("deductions", "deduction"),
                ("cash_applied", "cash"),
            ):
                bucket[key] += by_kind[kind][part.ref]
            bucket["outstanding"] += state.match.outstanding[part.ref]
        rows: list[dict[str, Any]] = []
        versions: dict[str, int] = {}
        today = as_of.date()
        for ref in sorted(selected_targets):
            target = dict(state.sources.targets[ref])
            if (
                target["kind"] in {"invoice", "timesheet"}
                and ref in query.refs
                and query.as_of is None
            ):
                from docket.services.financial_documents import FinancialDocumentService

                documents = FinancialDocumentService(self.session)
                revision = documents.revision(documents.head(ref))
                if target["kind"] == "invoice":
                    from docket.services.invoices import InvoiceService

                    target["reconciliation_flags"] = InvoiceService(self.session).flags(revision)
                else:
                    target["reconciliation_flags"] = documents.reconcile_flags(revision)
            versions[ref] = target["version"]
            parts = state.target_parts(ref)
            currencies: list[str | None] = list(sorted({part.currency for part in parts}))
            for code in currencies or [None]:
                chosen = [p for p in parts if p.currency == code]
                values = {
                    "gross_claim": sum((p.amount for p in chosen), Fraction(0)),
                    "outstanding": sum(
                        (state.match.outstanding[p.ref] for p in chosen), Fraction(0)
                    ),
                }
                for key, kind in (
                    ("credits", "credit"),
                    ("deductions", "deduction"),
                    ("cash_applied", "cash"),
                ):
                    values[key] = sum((by_kind[kind][p.ref] for p in chosen), Fraction(0))
                rows.append(
                    {
                        "record_type": "claim",
                        **target,
                        "currency": code,
                        **{key: amount_text(value) for key, value in values.items()},
                        "settlement_status": "unpriced"
                        if not chosen
                        else (
                            "settled"
                            if not values["outstanding"]
                            else "partial"
                            if values["cash_applied"] or values["credits"] or values["deductions"]
                            else "outstanding"
                        ),
                        "overdue": bool(
                            target.get("due_on")
                            and target["due_on"] < today.isoformat()
                            and values["outstanding"]
                        ),
                        "shared_source_balance": True,
                    }
                )
        for ref in sorted(selected_payments):
            row = state.payments[ref]
            version = (
                self.session.scalar(
                    select(func.max(PaymentEvent.version)).where(
                        PaymentEvent.payment_ref == ref,
                        PaymentEvent.created_at <= as_of,
                    )
                )
                or 1
            )
            versions[ref] = version
            received, unapplied = state.received[ref], state.available(ref)
            currency(row.currency)["received"] += received
            currency(row.currency)["unapplied"] += unapplied
            currency(row.currency)["unmatched_allocated_cash"] += sum(
                (
                    state.match.unmatched.get(entry.ref_id, Fraction(0))
                    for entry in state.entries
                    if entry.kind == "cash" and entry.payment_ref == ref
                ),
                Fraction(0),
            )
            key = {
                "wages": "reported_wages_received",
                "contract": "reported_contract_received",
                "reimbursement": "reported_reimbursement_received",
            }.get(row.category, "unclassified_received")
            currency(row.currency)[key] += received
            rows.append(
                {
                    "record_type": "payment",
                    "ref": ref,
                    "version": version,
                    "currency": row.currency,
                    "amount": row.amount,
                    "received": amount_text(received),
                    "unapplied": amount_text(unapplied),
                    "cash_allocated": amount_text(state.cash_active(ref)),
                    "received_at": instant(row.received_at).isoformat()
                    if row.received_at
                    else None,
                    "received_on": row.received_on.isoformat() if row.received_on else None,
                    "category": row.category,
                    "description": row.description,
                    "payer_ref": row.payer_ref,
                    "external_source": row.external_source,
                    "external_id": row.external_id,
                    "engagement_ref": row.engagement_ref,
                    "source_method": row.source_method,
                    "source_ref": row.source_ref,
                    "evidence_gap": row.evidence_gap,
                    "date_unknown": not row.received_at and not row.received_on,
                    "replaces_ref": row.replaces_ref,
                }
            )
        for entry in state.entries:
            if entry.ref_id in selected_entries:
                rows.append(
                    {
                        "record_type": "ledger",
                        "ref": entry.ref_id,
                        "transaction_ref": entry.transaction_ref,
                        "kind": entry.kind,
                        "amount": entry.amount,
                        "currency": entry.currency,
                        "effective_amount": amount_text(
                            state.effective.get(entry.ref_id, Fraction(0))
                        ),
                        "matched_amount": amount_text(state.matched(entry.ref_id))
                        if entry.kind in {"cash", "credit", "deduction"}
                        else None,
                        "unmatched_amount": amount_text(
                            state.match.unmatched.get(entry.ref_id, Fraction(0))
                        )
                        if entry.kind in {"cash", "credit", "deduction"}
                        else None,
                        "payment_ref": entry.payment_ref,
                        "target_ref": entry.target_ref,
                        "target_basis_ref": entry.target_basis_ref,
                        "reverses_ref": entry.reverses_ref,
                        "deduction_kind": entry.deduction_kind,
                        "note": entry.note,
                        "created_at": instant(entry.created_at).isoformat(),
                    }
                )
        currency_rows = [
            {
                "record_type": "currency_total",
                "currency": code,
                **{key: amount_text(value) for key, value in values.items()},
            }
            for code, values in sorted(totals.items())
        ]
        coverage = {
            "basis": "Retained posted claims and reported receipts; "
            "no bank sync or inferred net pay.",
            "shared_claims": "Document balances overlap. "
            "Currency totals count each source quantity once.",
            "document_reconciliation": "Explicit current document refs include source-change "
            "flags. "
            "Other rows retain their posted monetary basis.",
            "dates": "Missing receipt dates remain unknown; "
            "as_of uses record creation, not invented receipt dates.",
            "flags": sorted(state.sources.flags),
            "unmatched_entries": sum(bool(value) for value in state.match.unmatched.values()),
            "provider_effects": False,
        }
        report_ref = new_public_ref("frep")
        snapshot = FinancialReportSnapshot(
            ref_id=report_ref,
            operator_ref=principal.operator_ref,
            principal_ref=principal.principal_ref,
            parameters_json=query.model_dump(mode="json"),
            as_of=as_of,
            result_json={"rows": currency_rows + rows, "coverage": coverage},
        )
        self.session.add(snapshot)
        self.session.flush()
        return snapshot

    def query(self, query: GetFinancialContext) -> dict[str, Any]:
        principal = require_principal("read")
        position, representation = 0, query.format
        if query.cursor:
            try:
                ref, saved_format, position = json.loads(base64.urlsafe_b64decode(query.cursor))
                if (
                    saved_format not in {"summary", "json", "csv"}
                    or not isinstance(ref, str)
                    or not ref.startswith("frep_")
                    or type(position) is not int
                    or position < 0
                ):
                    raise ValueError("cursor")
            except (ValueError, TypeError, UnicodeError) as exc:
                raise DocketError(
                    code="invalid_financial_cursor", message="Use a returned financial cursor."
                ) from exc
            snapshot = self.session.scalar(
                select(FinancialReportSnapshot).where(
                    FinancialReportSnapshot.ref_id == ref,
                    FinancialReportSnapshot.operator_ref == principal.operator_ref,
                    FinancialReportSnapshot.principal_ref == principal.principal_ref,
                )
            )
            if snapshot is None:
                raise DocketError(
                    code="financial_report_unavailable",
                    message="Read a report with this connection.",
                )
            if "format" not in query.model_fields_set:
                representation = saved_format
            elif representation != saved_format:
                position = 0
        else:
            snapshot = self.capture(query)
        result = snapshot.result_json
        rows = result["rows"]
        base = {
            "ok": True,
            "report_ref": snapshot.ref_id,
            "as_of": instant(snapshot.as_of).isoformat(),
            "format": representation,
        }
        if representation != "summary":
            if representation == "json":
                content = json.dumps({**base, **result}, ensure_ascii=False, separators=(",", ":"))
            else:
                fields = sorted({key for row in rows for key in row})
                stream = io.StringIO(newline="")
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for row in rows:
                    writer.writerow(
                        {
                            key: json.dumps(value, ensure_ascii=False)
                            if isinstance(value, (dict, list))
                            else value
                            for key, value in row.items()
                        }
                    )
                content = stream.getvalue()
            # Slice on Unicode boundaries. Escaped JSON response stays under 16 KiB.
            if position > len(content):
                raise DocketError(
                    code="invalid_financial_cursor", message="Use a returned financial cursor."
                )
            stop = min(len(content), position + 6000)
            chunk = content[position:stop]
            while len(json.dumps({**base, "content": chunk}, ensure_ascii=False).encode()) > 12000:
                stop = position + max(1, (stop - position) // 2)
                chunk = content[position:stop]
            return {
                **base,
                "content": chunk,
                "complete": stop == len(content),
                "next_cursor": encode_cursor(snapshot.ref_id, representation, stop)
                if stop < len(content)
                else None,
            }
        if position > len(rows):
            raise DocketError(
                code="invalid_financial_cursor", message="Use a returned financial cursor."
            )
        selected = rows[position : position + query.limit]
        context: dict[str, Any] = {}
        while True:
            versions = {row["ref"]: row["version"] for row in selected if row.get("version")}
            objects = [{"ref": row["ref"], "state": row} for row in selected if row.get("ref")]
            packet = {
                "report_ref": snapshot.ref_id,
                "as_of": base["as_of"],
                "objects": objects,
                "rows": [row for row in selected if not row.get("ref")],
                "coverage": result["coverage"],
                "allowed_actions": [
                    "docket_record_payment",
                    "docket_apply_payment",
                    "docket_adjust_financial_claim",
                    "docket_reverse_financial_record",
                ]
                if "commit" in principal.permissions
                else [],
            }
            if len(json.dumps(packet, ensure_ascii=False).encode()) <= 7000:
                context = NativeContextService(self.session, self.settings).retain(
                    "financial", packet, versions
                )
                break
            if len(selected) == 1:
                original = selected[0]
                if "omitted_fields" in original:
                    raise DocketError(
                        code="financial_context_too_large",
                        message="Read this retained report as JSON or CSV.",
                        details={"export_cursor": encode_cursor(snapshot.ref_id, "json", 0)},
                    )
                selected = [
                    {
                        key: value
                        for key, value in original.items()
                        if key not in {"note", "description", "label", "flags"}
                    }
                ]
                selected[0]["omitted_fields"] = [
                    key for key in ("note", "description", "label", "flags") if original.get(key)
                ]
            else:
                selected = selected[: max(1, len(selected) // 2)]
        stop = position + len(selected)
        return {
            **context,
            **base,
            "row_count": len(rows),
            "complete": stop == len(rows),
            "next_cursor": encode_cursor(snapshot.ref_id, representation, stop)
            if stop < len(rows)
            else None,
            "export_cursor": encode_cursor(snapshot.ref_id, "json", 0),
            "coverage": result["coverage"],
        }
