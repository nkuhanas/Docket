"""Bounded lossless exports of immutable formal document revisions."""

import base64
import csv
import io
from typing import Any, Literal

from pydantic import Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.domain.errors import DocketError
from docket.models import (
    CompensationEvaluation,
    FinancialDocumentRevision,
    InvoiceNumber,
    WorkSessionRevision,
)
from docket.schemas.common import StrictModel
from docket.schemas.financial_documents import ReadDocument
from docket.services.financial_documents import FinancialDocumentService
from docket.services.work_reports import compact


class DocumentCursor(StrictModel):
    revision_ref: str = Field(pattern=r"^fdrev_[0-9A-HJKMNP-TV-Z]{26}$")
    format: Literal["summary", "json", "csv", "markdown"] = "json"
    offset: int = Field(default=0, ge=0)

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")


class FinancialDocumentReadService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.documents = FinancialDocumentService(session)

    def resolve(self, public: ReadDocument) -> tuple[FinancialDocumentRevision, DocumentCursor]:
        require_principal("read")
        cursor = None
        if public.cursor:
            try:
                encoded = public.cursor
                cursor = DocumentCursor.model_validate_json(
                    base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
                )
            except (ValueError, ValidationError) as exc:
                raise DocketError(
                    code="invalid_document_cursor", message="Use the returned document cursor."
                ) from exc
            if "format" in public.model_fields_set and public.format != cursor.format:
                if cursor.offset:
                    raise DocketError(
                        code="document_cursor_format_changed",
                        message="Start a new export to change format.",
                    )
                cursor.format = public.format
        ref = cursor.revision_ref if cursor else public.ref
        if ref is None:
            raise DocketError(
                code="document_ref_required",
                message="Select a document or use its returned cursor.",
                details={"field": "ref"},
            )
        if ref.startswith("fdoc_"):
            revision = self.documents.revision(self.documents.head(ref))
        else:
            selected = self.session.scalar(
                select(FinancialDocumentRevision).where(FinancialDocumentRevision.ref_id == ref)
            )
            if selected is None:
                raise DocketError(code="document_unavailable", message="Select an owned document.")
            revision = selected
            self.documents.head(revision.document_ref)
        if cursor and public.ref and public.ref not in {revision.document_ref, revision.ref_id}:
            raise DocketError(
                code="document_cursor_ref_changed", message="Continue the selected revision."
            )
        return revision, cursor or DocumentCursor(
            revision_ref=revision.ref_id, format=public.format
        )

    def data(self, revision: FinancialDocumentRevision) -> dict[str, Any]:
        head = self.documents.head(revision.document_ref)
        evaluation = self.session.scalar(
            select(CompensationEvaluation).where(
                CompensationEvaluation.ref_id == revision.evaluation_ref
            )
        )
        assert evaluation is not None
        if head.kind == "invoice":
            body = revision.snapshot_json
            number = self.session.get(InvoiceNumber, head.ref_id)
            return {
                "document_ref": head.ref_id,
                "revision_ref": revision.ref_id,
                "kind": "invoice",
                "revision": revision.revision,
                "status": revision.status,
                "engagement_ref": head.engagement_ref,
                "engagement_label": body["engagement_label"],
                "component_key": head.component_key,
                "date_from": revision.date_from.isoformat(),
                "date_to": revision.date_to.isoformat(),
                "timezone": revision.timezone,
                "evaluation_ref": evaluation.ref_id,
                "calculation_version": evaluation.calculation_version,
                "calculation_as_of": evaluation.parameters_json["as_of"],
                "work_report_ref": evaluation.work_report_ref,
                "summary": body["summary"],
                "claim_refs": body["claim_refs"],
                "allocation_refs": body["allocation_refs"],
                "header": body["header"],
                "issued_at": body.get("issued_at"),
                "invoice_calculation_version": body.get("invoice_calculation_version"),
                "number": number.number
                if number and number.issued_revision_ref == revision.ref_id
                else None,
                "lines": body["lines"],
                "policy_basis": evaluation.result_json["basis"],
                "basis_refs": revision.basis_refs,
                "amends_ref": revision.amends_ref,
                "created_by_changeset_ref": revision.created_by_changeset_ref,
            }
        rows = evaluation.result_json["contributions"]
        refs = {line["revision_ref"] for line in rows}
        descriptions = {
            row.ref_id: row
            for row in self.session.scalars(
                select(WorkSessionRevision).where(WorkSessionRevision.ref_id.in_(refs))
            )
        }
        return {
            "document_ref": head.ref_id,
            "revision_ref": revision.ref_id,
            "kind": head.kind,
            "revision": revision.revision,
            "status": revision.status,
            "engagement_ref": head.engagement_ref,
            "engagement_label": revision.snapshot_json.get("engagement_label", head.engagement_ref),
            "component_key": head.component_key,
            "date_from": revision.date_from.isoformat(),
            "date_to": revision.date_to.isoformat(),
            "timezone": revision.timezone,
            "evaluation_ref": revision.evaluation_ref,
            "calculation_version": evaluation.calculation_version,
            "calculation_as_of": evaluation.parameters_json["as_of"],
            "work_report_ref": evaluation.work_report_ref,
            "summary": revision.snapshot_json["summary"],
            "claim_refs": revision.snapshot_json["claim_refs"],
            "amends_ref": revision.amends_ref,
            "observation": revision.observation,
            "observation_at": revision.observation_at.isoformat()
            if revision.observation_at
            else None,
            "basis_refs": revision.basis_refs,
            "created_by_changeset_ref": revision.created_by_changeset_ref,
            "policy_basis": evaluation.result_json["basis"],
            "lines": [
                {
                    **line,
                    "description": descriptions[line["revision_ref"]].description,
                    "outcome": descriptions[line["revision_ref"]].outcome,
                }
                for line in rows
            ],
        }

    @staticmethod
    def csv(data: dict[str, Any]) -> str:
        stream = io.StringIO(newline="")
        metadata = {
            key: data[key]
            for key in (
                "document_ref",
                "revision_ref",
                "engagement_ref",
                "component_key",
                "date_from",
                "date_to",
                "timezone",
                "calculation_version",
                "calculation_as_of",
                "amends_ref",
            )
        }
        if data["kind"] == "invoice":
            metadata.update(
                number=data["number"],
                issued_at=data["issued_at"],
                **{
                    key: data["header"].get(key)
                    for key in ("issuer_label", "client_label", "due_on", "payment_terms")
                },
            )
        fields = sorted(
            {key for line in data["lines"] for key in line} - set(metadata) - {"record_type"}
        )
        writer = csv.DictWriter(
            stream, fieldnames=["record_type", *metadata, *fields, "summary_json"]
        )
        writer.writeheader()
        writer.writerow(
            {"record_type": "summary", **metadata, "summary_json": compact(data["summary"])}
        )
        for line in data["lines"]:
            writer.writerow(
                {
                    "record_type": line.get("record_type", "work"),
                    **metadata,
                    **{
                        key: compact(value) if isinstance(value, (dict, list)) else value
                        for key, value in line.items()
                        if key not in metadata
                    },
                }
            )
        return stream.getvalue()

    @staticmethod
    def markdown(data: dict[str, Any]) -> str:
        def cell(value: Any) -> str:
            return (
                str(value if value is not None else "unknown")
                .replace("|", "\\|")
                .replace("\n", " ")
            )

        if data["kind"] == "invoice":
            header = data["header"]
            rows = [
                f"# Invoice {cell(data['number'] or 'DRAFT')}",
                "",
                f"Issuer: {cell(header['issuer_label'])}",
                f"Client: {cell(header['client_label'])}",
                f"Status: {data['status']}",
                f"Issued: {cell(data['issued_at'])}",
                f"Due: {cell(header.get('due_on'))}",
                f"Terms: {cell(header.get('payment_terms'))}",
                f"Period: {data['date_from']} to {data['date_to']} (end exclusive); "
                f"{data['timezone']}",
                "",
                "| Date/start | Description | Hours | Line estimate | Source |",
                "| --- | --- | ---: | ---: | --- |",
            ]
            from decimal import Decimal

            for row in data["lines"]:
                quantity = row.get("reportable_us")
                hours = (
                    format(Decimal(quantity) / Decimal(3_600_000_000), ".6f")
                    if quantity is not None
                    else "—"
                )
                amount = (
                    str(row["gross_line_estimate"]) + " " + str(row.get("currency", ""))
                    if row.get("gross_line_estimate") is not None
                    else "unresolved"
                )
                source = row.get("work_ref") or row.get("expense_ref")
                rows.append(
                    f"| {cell(row.get('day') or row.get('start'))} | "
                    f"{cell(row['description'])} | {hours} | {cell(amount)} | {cell(source)} |"
                )
            rows += ["", "Total:"] + [
                f"- {item['amount']} {item['currency']}"
                for item in data["summary"]["gross_estimates"]
            ]
            if data["summary"]["flags"]:
                rows += ["", "Unresolved: " + ", ".join(data["summary"]["flags"])]
            if header.get("notes"):
                rows += ["", header["notes"]]
            rows += [
                "",
                f"Document: {data['document_ref']} · Revision: {data['revision_ref']}",
                f"Calculation: {data['evaluation_ref']} ({data['calculation_version']}); "
                f"as of {data['calculation_as_of']}.",
                "The document total rounds once over exact contributions. "
                "Issuance, delivery and payment are separate. This export sends nothing.",
            ]
            return "\n".join(rows) + "\n"
        lines = [
            f"# {data['kind'].capitalize()}: {cell(data['engagement_label'])}",
            "",
            f"Document: {data['document_ref']} · Revision: {data['revision_ref']}",
            f"Status: {data['status']}",
            f"Period: {data['date_from']} to {data['date_to']} (end exclusive); {data['timezone']}",
            "",
            "| Date or start | Work performed | Reported hours | Gross estimate |",
            "| --- | --- | ---: | ---: |",
        ]
        from decimal import Decimal

        for row in data["lines"]:
            quantity = row.get("raw_us")
            hours = (
                format(Decimal(quantity) / Decimal(3_600_000_000), ".6f")
                if quantity is not None
                else "unknown"
            )
            gross = (
                str(row["gross_line_estimate"]) + " " + str(row.get("currency", ""))
                if row.get("gross_line_estimate") is not None
                else "unresolved"
            )
            lines.append(
                f"| {cell(row.get('day') or row.get('start'))} | {cell(row['description'])} | "
                f"{hours} | {cell(gross)} |"
            )
        lines += ["", "Gross estimates (rounded over the retained calculation):"]
        lines += [
            f"- {row['amount']} {row['currency']}" for row in data["summary"]["gross_estimates"]
        ]
        if data["summary"]["flags"]:
            lines += ["", "Unresolved or review: " + ", ".join(data["summary"]["flags"])]
        lines += [
            "",
            "Submission and payment are separate. This export sends nothing.",
            f"Calculation: {data['evaluation_ref']} ({data['calculation_version']}); "
            f"as of {data['calculation_as_of']}.",
        ]
        return "\n".join(lines) + "\n"

    def read(self, public: ReadDocument) -> dict[str, Any]:
        revision, cursor = self.resolve(public)
        head = self.documents.head(revision.document_ref)
        state = self.documents.view(head, revision)
        result: dict[str, Any] = {
            "ok": True,
            "document": state,
            "export_cursor": DocumentCursor(revision_ref=revision.ref_id).encode(),
        }
        if len(compact(result).encode()) > 8192:
            state["gross_estimate_bucket_count"] = len(state["summary"]["gross_estimates"])
            state["summary"] = {
                key: value for key, value in state["summary"].items() if key != "gross_estimates"
            }
            state["full_summary_in_export"] = True
        if cursor.format == "summary":
            result["complete"] = True
            return result
        data = self.data(revision)
        content = (
            compact(data)
            if cursor.format == "json"
            else self.csv(data)
            if cursor.format == "csv"
            else self.markdown(data)
        )
        encoded = content.encode()
        if cursor.offset > len(encoded) or (
            cursor.offset < len(encoded) and encoded[cursor.offset] & 0xC0 == 0x80
        ):
            raise DocketError(
                code="invalid_document_cursor", message="Use the returned document cursor."
            )
        end = min(len(encoded), cursor.offset + public.max_bytes)
        while end < len(encoded) and encoded[end] & 0xC0 == 0x80:
            end -= 1
        result.update(
            format=cursor.format,
            byte_offset=cursor.offset,
            content=encoded[cursor.offset : end].decode(),
            complete=end == len(encoded),
        )
        while len(compact(result).encode()) > 16 * 1024 - 512 and end > cursor.offset:
            end = cursor.offset + (end - cursor.offset) // 2
            while end < len(encoded) and encoded[end] & 0xC0 == 0x80:
                end -= 1
            result.update(
                content=encoded[cursor.offset : end].decode(), complete=end == len(encoded)
            )
        if end < len(encoded):
            result["next_cursor"] = DocumentCursor(
                revision_ref=revision.ref_id, format=cursor.format, offset=end
            ).encode()
        return result
