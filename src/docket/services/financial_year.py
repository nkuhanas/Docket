"""Scoped financial-year snapshots, paged exports and shared attention-case reconciliation."""

import csv
import io
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.models import (
    AttentionCase,
    AttentionCaseRevision,
    CaseItem,
    FinancialReportSnapshot,
    FinancialReviewFact,
)
from docket.models.base import utc_now
from docket.schemas.financial_review import AnnualReview, ReadFinancialYear
from docket.services.annual_workpapers import AnnualWorkpapers
from docket.services.financial_review import FinancialReviewService, reject
from docket.services.native_context import NativeContextService
from docket.services.provenance_refs import ProvenanceRefService


class FinancialYearService:
    def __init__(self, session: Session):
        self.session = session

    def snapshot(self, ref: str, year: int | None = None) -> FinancialReportSnapshot:
        p = require_principal("read")
        report = self.session.scalar(
            select(FinancialReportSnapshot).where(
                FinancialReportSnapshot.ref_id == ref,
                FinancialReportSnapshot.operator_ref == p.operator_ref,
                FinancialReportSnapshot.principal_ref == p.principal_ref,
            )
        )
        if (
            not report
            or report.parameters_json.get("report_kind") != "financial_year"
            or (year and report.parameters_json["tax_year"] != year)
        ):
            raise reject("Read a financial year with this connection.")
        return report

    def cases(self, scope: str, *, lock: bool = False) -> dict[str, tuple[AttentionCase, str]]:
        stmt = (
            select(AttentionCase, AttentionCaseRevision.content_hash)
            .join(
                AttentionCaseRevision,
                (AttentionCaseRevision.case_ref == AttentionCase.ref_id)
                & (AttentionCaseRevision.revision == AttentionCase.latest_revision),
            )
            .where(AttentionCaseRevision.dependency_summary["finance_scope"].as_string() == scope)
        )
        if lock:
            stmt = stmt.with_for_update()
        return {case.situation_key: (case, digest) for case, digest in self.session.execute(stmt)}

    def read(self, query: ReadFinancialYear) -> dict[str, Any]:
        principal = require_principal("read")
        if query.report_ref:
            report = self.snapshot(query.report_ref, query.tax_year)
        else:
            if self.session.get_bind().dialect.name == "postgresql":
                self.session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            now = utc_now()
            data = AnnualWorkpapers(self.session, query.tax_year).build()
            scope = sha256_json(["financial_review", principal.operator_ref, query.tax_year])
            known = self.cases(scope)
            findings = data["decisions"]
            data["findings"] = findings
            data["decisions"] = []
            for finding in findings:
                key = sha256_json([scope, finding["kind"], finding["subject"]])
                previous = known.get(key)
                if previous:
                    case, digest = previous
                    if case.status == "suppressed" or (
                        case.status == "resolved" and digest == sha256_json(["open", finding])
                    ):
                        continue
                data["decisions"].append(
                    {**finding, **({"case_ref": previous[0].ref_id} if previous else {})}
                )
            report = FinancialReportSnapshot(
                operator_ref=principal.operator_ref,
                principal_ref=principal.principal_ref,
                parameters_json={"report_kind": "financial_year", "tax_year": query.tax_year},
                result_json=data,
                as_of=now,
            )
            self.session.add(report)
            self.session.flush()
        objects, versions = [{"ref": report.ref_id, "type": "financial_year"}], {}
        allowed = {"exp", "eng", "fund", "actv", "pay", "src", "finrev", "fent"}
        for ref in query.refs:
            if ref.split("_")[0] not in allowed:
                raise reject("Select an owned financial input or source.")
            if ref.startswith("src_"):
                FinancialReviewService(self.session).evidence(ref)
                objects.append({"ref": ref})
            else:
                row = ProvenanceRefService(self.session).get(ref)
                if getattr(row, "operator_ref", None) != principal.operator_ref:
                    raise reject("Select an owned financial record.")
                detail: dict[str, Any] = {"ref": ref}
                if isinstance(row, FinancialReviewFact):
                    detail.update(
                        {
                            "kind": row.kind,
                            "revision": row.revision,
                            "prior_ref": row.prior_ref,
                            "state": row.fields,
                        }
                    )
                objects.append(detail)
                if hasattr(row, "version"):
                    versions[ref] = row.version
        all_rows = report.result_json[query.section]
        rows: list[dict[str, Any]] = []
        for row in all_rows[query.offset : query.offset + 15] if query.format == "json" else []:
            if len(json.dumps([*rows, row], ensure_ascii=False).encode()) > 9000:
                if not rows:
                    raise reject(
                        "Use format=json_text with this report_ref to export the full section."
                    )
                break
            rows.append(row)
        payload = {
            "objects": objects,
            "report_ref": report.ref_id,
            "tax_year": query.tax_year,
            "as_of": report.as_of.isoformat(),
            "section": query.section,
            "coverage": "Recorded evidence only. Unobserved periods have unknown coverage.",
            "counts": {k: len(v) for k, v in report.result_json.items() if k != "findings"},
            "next_offset": query.offset + len(rows)
            if query.offset + len(rows) < len(all_rows)
            else None,
        }
        if query.format == "csv":
            stream = io.StringIO(newline="")
            writer = csv.DictWriter(stream, fieldnames=sorted({k for row in all_rows for k in row}))
            writer.writeheader()
            for row in all_rows:
                writer.writerow(
                    {
                        k: json.dumps(v, separators=(",", ":"))
                        if isinstance(v, (dict, list))
                        else v
                        for k, v in row.items()
                    }
                )
            content = stream.getvalue()
        elif query.format == "json_text":
            content = json.dumps(all_rows, ensure_ascii=False, separators=(",", ":"))
        if query.format in ("csv", "json_text"):
            if query.offset > len(content):
                raise reject("Use an offset within the selected export.")
            end = min(len(content), query.offset + 4000)
            chunk = content[query.offset : end]
            while len(json.dumps(chunk, ensure_ascii=False).encode()) > 8000:
                end = (query.offset + end) // 2
                chunk = content[query.offset : end]
            payload["csv" if query.format == "csv" else "content"] = chunk
            payload["next_offset"] = end if end < len(content) else None
        else:
            payload["rows"] = rows
        return NativeContextService(self.session, get_settings()).retain(
            "financial_year", payload, versions
        )

    def reconcile(self, change: Any, changeset: Any) -> list[str]:
        report = self.snapshot(AnnualReview.model_validate(change.create_spec).report_ref)
        year = report.parameters_json["tax_year"]
        fresh = AnnualWorkpapers(self.session, year).build()["decisions"]
        if sha256_json(fresh) != sha256_json(report.result_json["findings"]):
            raise reject("Financial evidence changed. Read a fresh year before reconciling.")
        scope = sha256_json(["financial_review", report.operator_ref, year])
        cases = {key: value[0] for key, value in self.cases(scope, lock=True).items()}
        now = utc_now()

        def revision(case: AttentionCase, payload: dict[str, Any], status: str) -> None:
            old = self.session.scalar(
                select(AttentionCaseRevision).where(
                    AttentionCaseRevision.case_ref == case.ref_id,
                    AttentionCaseRevision.revision == case.latest_revision,
                )
            )
            digest = sha256_json([status, payload])
            if old and old.content_hash == digest:
                return
            if old:
                case.version += 1
                case.latest_revision += 1
            if case.status != "suppressed":
                case.status = status
                case.resolution_decision_ref = None
                case.resolved_at = now if status == "resolved" else None
            case.last_observed_at = now
            item = CaseItem(
                attention_case_id=case.id,
                item_key="finance:" + str(case.latest_revision),
                item_type="decision_required",
                resolution_role="supporting",
                status="resolved" if status == "resolved" else "open",
                payload_json={
                    "financial_report_ref": report.ref_id,
                    "finding_key": case.situation_key,
                },
                candidate_refs=[],
                basis_refs=[report.ref_id],
                source_refs=[],
            )
            self.session.add(item)
            self.session.flush()
            self.session.add(
                AttentionCaseRevision(
                    attention_case_id=case.id,
                    case_ref=case.ref_id,
                    revision=case.latest_revision,
                    title=case.title,
                    summary=case.summary,
                    semantic_classes=case.semantic_classes,
                    case_item_refs=[item.ref_id],
                    source_refs=[],
                    admission_rule_ref="financial_year_review_v1",
                    admission_basis_refs=[report.ref_id, changeset.ref_id],
                    required_case_item_refs=[],
                    canonical_consequence_classes=[],
                    dependency_summary={
                        "report_ref": report.ref_id,
                        "tax_year": year,
                        "finance_scope": scope,
                    },
                    content_hash=digest,
                )
            )

        for finding in fresh:
            key = sha256_json([scope, finding["kind"], finding["subject"]])
            case = cases.pop(key, None)
            if not case:
                case = AttentionCase(
                    situation_key=key,
                    title="Financial review: " + finding["kind"].replace("_", " "),
                    summary=finding["summary"],
                    status="open",
                    priority="normal",
                    semantic_classes=["financial_review", scope, finding["kind"]],
                    first_observed_at=now,
                    last_observed_at=now,
                )
                self.session.add(case)
                self.session.flush()
            revision(case, finding, "open")
        for case in cases.values():
            if case.status == "open":
                revision(case, {"reason": "finding_cleared"}, "resolved")
        self.session.flush()
        return [report.ref_id]
