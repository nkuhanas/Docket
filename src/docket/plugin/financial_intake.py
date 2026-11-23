"""Native host file boundary. File capabilities never enter persisted command payloads."""

from typing import Any

from mcp.types import ToolAnnotations
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.config import Settings
from docket.database import session_scope
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.mcp.instrumented import ProvenanceMCP
from docket.models import Expense
from docket.models.funding import AccountActivity
from docket.plugin.business import model_function
from docket.schemas.business import BUSINESS_BY_TOOL
from docket.schemas.financial_intake import SubmitEvidence
from docket.services.agent_requests import AgentRequestService
from docket.services.business_commands import BusinessCommandService, public_validation_error
from docket.services.financial_files import download_file, rejected
from docket.services.financial_intake import FinancialIntakeService
from docket.services.funding import FundingService
from docket.services.native_context import NativeContextService


def intake_context(
    session: Session,
    settings: Settings,
    source: str,
    refs: list[str],
    cursor: str | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    service = FinancialIntakeService(session, settings)
    row = service.owned(source)
    if cursor and (not cursor.isascii() or not cursor.isdecimal() or len(cursor) > 6):
        raise rejected("invalid_intake_cursor")
    packet = service.receipt(row, int(cursor or 0), min(limit, 8))
    versions = {c["candidate_ref"]: c["version"] for c in packet["outcomes"]}
    objects: list[dict[str, Any]] = []
    for ref in refs:
        if ref.startswith("card_"):
            head = FundingService(session).head(ref)
            objects.append({"ref": ref, "state": FundingService(session).revision(head).fields})
            versions[ref] = head.version
        elif ref.startswith("actv_"):
            activity = session.get(AccountActivity, ref)
            if not activity or activity.operator_ref != row.operator_ref:
                raise rejected("activity_unavailable")
            objects.append({"ref": ref, "state": activity.fields})
        elif ref.startswith("exp_"):
            expense = session.scalar(
                select(Expense).where(
                    Expense.operator_ref == row.operator_ref, Expense.ref_id == ref
                )
            )
            if not expense:
                raise rejected("expense_unavailable")
            objects.append({"ref": ref, "version": expense.version})
            versions[ref] = expense.version
        else:
            raise rejected("unsupported_intake_link")
    return NativeContextService(session, settings).retain(
        "financial_intake", {**packet, "objects": objects}, versions
    )


def register_financial_intake(server: ProvenanceMCP, settings: Settings) -> None:
    def submit(args: dict[str, Any]) -> dict[str, Any]:
        request_ref, request_key = args.pop("request_ref", None), args.pop("request_key", None)
        source = None
        try:
            public = SubmitEvidence.model_validate(args)
            if not request_ref or not request_key:
                raise rejected("request_context_required")
            principal = require_principal("commit")
            declarations = public.model_dump(
                mode="json",
                exclude_none=True,
                include={"business_use_percent", "write_off_intent", "reported_instrument"},
            )
            identity = sha256_json([public.context_id, public.file.file_id, declarations])
            with session_scope() as session:
                AgentRequestService(session).require(
                    request_ref, permission="commit", allow_committed=True
                )
                NativeContextService(session, settings).require(public.context_id)
                existing = FinancialIntakeService(session, settings).find(
                    principal.operator_ref, principal.principal_ref, public.operation_id
                )
                if existing and existing.input_hash != identity:
                    raise rejected("financial_delivery_conflict")
                source = existing.source_ref if existing else None
            if not source:
                hosts = frozenset(
                    h.strip().lower() for h in settings.financial_file_hosts.split(",") if h.strip()
                )
                blob = download_file(public.file.download_url, hosts, settings.attachment_max_bytes)
                with session_scope() as session:
                    request = AgentRequestService(session).require(
                        request_ref, permission="commit", allow_committed=True
                    )
                    service = FinancialIntakeService(session, settings)
                    source = service.admit(
                        blob,
                        delivery=public.operation_id,
                        input_hash=identity,
                        declared=public.file.mime_type,
                        filename=public.file.file_name,
                        declarations=declarations,
                        request=request,
                    ).source_ref
            with session_scope() as session:
                service = FinancialIntakeService(session, settings)
                row = service.owned(source)
                if row.byte_count <= 128_000 and row.media_type != "application/pdf":
                    service.process(source)
            with session_scope() as session:
                service = FinancialIntakeService(session, settings)
                candidates = service.candidates(source)
                # Auto-adopt one unambiguous purchase after committing the original.
                if (
                    len(candidates) == 1
                    and candidates[0].purchase_key
                    and not candidates[0].expense_ref
                ):
                    packet = intake_context(session, settings, source, [])
                    definition = BUSINESS_BY_TOOL["docket_update_financial_candidate"]
                    result = BusinessCommandService(session, settings).perform(
                        definition,
                        {
                            "context_id": packet["context_id"],
                            "operation_id": public.operation_id,
                            "target_ref": candidates[0].ref_id,
                        },
                        request_ref,
                        request_key,
                    )
                    if result.get("disposition") != "committed":
                        candidates[0].review_reason = "expense_adoption_requires_review"
                return service.receipt(service.owned(source))
        except ValidationError as exc:
            return public_validation_error(exc).as_dict()
        except DocketError as exc:
            if source:
                with session_scope() as session:
                    service = FinancialIntakeService(session, settings)
                    result = service.receipt(service.owned(source))
                    result["processing_issue"] = exc.code
                    return result
            return exc.as_dict()

    server.add_tool(
        model_function(SubmitEvidence, "docket_submit_financial_evidence", submit),
        name="docket_submit_financial_evidence",
        description=(
            "Submit an original receipt, EML or statement. Report operator declarations. "
            "May record an unambiguous expense. Follow the receipt for unresolved funding."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
        ),
    )
