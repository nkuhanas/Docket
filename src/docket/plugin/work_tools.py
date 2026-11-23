"""Thin native work handlers; inference belongs to the connected agent."""

from typing import Any

from mcp.types import ToolAnnotations
from pydantic import ValidationError

from docket.config import Settings
from docket.database import session_scope
from docket.domain.errors import DocketError
from docket.mcp.instrumented import ProvenanceMCP
from docket.plugin.business import model_function
from docket.schemas.compensation import POLICY_TOOL_PURPOSES, GetCompensation
from docket.schemas.expenses import EXPENSE_TOOL_PURPOSES, QueryExpenses
from docket.schemas.financial_documents import FINANCIAL_TOOL_PURPOSES, ReadDocument
from docket.schemas.financial_review import ReadFinancialYear
from docket.schemas.followup import FOLLOWUP_TOOL_PURPOSES
from docket.schemas.funding import FundingContext
from docket.schemas.invoices import INVOICE_TOOL_PURPOSES
from docket.schemas.settlement import SETTLEMENT_TOOL_PURPOSES, GetFinancialContext
from docket.schemas.work import WORK_TOOL_PURPOSES, GetWorkContext, QueryWorkSessions
from docket.schemas.work_calendar import WORK_CALENDAR_PURPOSES
from docket.schemas.workload import WORKLOAD_TOOL_PURPOSES, GetWorkloadContext
from docket.services.business_commands import public_validation_error
from docket.services.compensation_evaluations import CompensationEvaluationService
from docket.services.compensation_policies import (
    POLICY_COMMAND_MODELS,
    CompensationPolicyCommandService,
)
from docket.services.expense_reports import ExpenseReportService
from docket.services.expenses import EXPENSE_COMMAND_MODELS, ExpenseCommandService
from docket.services.financial_context import FinancialContextService
from docket.services.financial_document_reads import FinancialDocumentReadService
from docket.services.financial_documents import (
    DOCUMENT_COMMAND_MODELS,
    FinancialDocumentCommandService,
)
from docket.services.financial_year import FinancialYearService
from docket.services.funding import FundingService
from docket.services.invoices import INVOICE_COMMAND_MODELS, InvoiceCommandService
from docket.services.native_context import NativeContextService
from docket.services.settlement_commands import SETTLEMENT_COMMAND_MODELS, SettlementCommandService
from docket.services.work_calendar import WORK_CALENDAR_MODELS, WorkCalendarCommandService
from docket.services.work_commands import WORK_COMMAND_MODELS, WorkCommandService
from docket.services.work_lineage import LINEAGE_COMMAND_MODELS, WorkLineageCommandService
from docket.services.work_reports import WorkReportService
from docket.services.work_timers import TIMER_COMMAND_MODELS, WorkTimerCommandService
from docket.services.workload_context import WorkloadContextService
from docket.services.workload_followup import (
    FOLLOWUP_COMMAND_MODELS,
    WorkloadFollowupCommandService,
)
from docket.services.workload_preferences import (
    WORKLOAD_COMMAND_MODELS,
    WorkloadPreferenceCommandService,
)

WORK_COMMAND_TOOLS = (
    frozenset(WORK_COMMAND_MODELS)
    | frozenset(TIMER_COMMAND_MODELS)
    | frozenset(LINEAGE_COMMAND_MODELS)
    | frozenset(POLICY_COMMAND_MODELS)
    | frozenset(DOCUMENT_COMMAND_MODELS)
    | frozenset(EXPENSE_COMMAND_MODELS)
    | frozenset(INVOICE_COMMAND_MODELS)
    | frozenset(SETTLEMENT_COMMAND_MODELS)
    | frozenset(WORKLOAD_COMMAND_MODELS)
    | frozenset(FOLLOWUP_COMMAND_MODELS)
    | frozenset(WORK_CALENDAR_MODELS)
)


def register_work(server: ProvenanceMCP, settings: Settings) -> None:
    def register(name: str) -> None:
        def invoke(arguments: dict[str, Any]) -> dict[str, Any]:
            request_ref = arguments.pop("request_ref", None)
            request_key = arguments.pop("request_key", None)
            try:
                if not request_ref or not request_key:
                    raise DocketError(
                        code="request_context_required", message="Use the authenticated connection."
                    )
                with session_scope() as session:
                    service = (
                        WorkCalendarCommandService
                        if name in WORK_CALENDAR_MODELS
                        else WorkloadFollowupCommandService
                        if name in FOLLOWUP_COMMAND_MODELS
                        else WorkloadPreferenceCommandService
                        if name in WORKLOAD_COMMAND_MODELS
                        else SettlementCommandService
                        if name in SETTLEMENT_COMMAND_MODELS
                        else WorkTimerCommandService
                        if name in TIMER_COMMAND_MODELS
                        else InvoiceCommandService
                        if name in INVOICE_COMMAND_MODELS
                        else ExpenseCommandService
                        if name in EXPENSE_COMMAND_MODELS
                        else FinancialDocumentCommandService
                        if name in DOCUMENT_COMMAND_MODELS
                        else CompensationPolicyCommandService
                        if name in POLICY_COMMAND_MODELS
                        else WorkLineageCommandService
                        if name in LINEAGE_COMMAND_MODELS
                        else WorkCommandService
                    )
                    return service(session, settings).perform(
                        name,
                        arguments,
                        request_ref,
                        request_key,
                    )
            except ValidationError as exc:
                return public_validation_error(exc).as_dict()
            except DocketError as exc:
                return exc.as_dict()

        server.add_tool(
            model_function(
                (
                    {
                        **WORK_COMMAND_MODELS,
                        **TIMER_COMMAND_MODELS,
                        **LINEAGE_COMMAND_MODELS,
                        **POLICY_COMMAND_MODELS,
                        **DOCUMENT_COMMAND_MODELS,
                        **EXPENSE_COMMAND_MODELS,
                        **INVOICE_COMMAND_MODELS,
                        **SETTLEMENT_COMMAND_MODELS,
                        **WORKLOAD_COMMAND_MODELS,
                        **FOLLOWUP_COMMAND_MODELS,
                        **WORK_CALENDAR_MODELS,
                    }
                )[name],
                name,
                invoke,
            ),
            name=name,
            description={
                **WORK_TOOL_PURPOSES,
                **POLICY_TOOL_PURPOSES,
                **FINANCIAL_TOOL_PURPOSES,
                **EXPENSE_TOOL_PURPOSES,
                **INVOICE_TOOL_PURPOSES,
                **SETTLEMENT_TOOL_PURPOSES,
                **WORKLOAD_TOOL_PURPOSES,
                **FOLLOWUP_TOOL_PURPOSES,
                **WORK_CALENDAR_PURPOSES,
            }[name],
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=name in WORK_CALENDAR_MODELS or name in FOLLOWUP_COMMAND_MODELS,
            ),
        )

    for name in (
        *WORK_COMMAND_MODELS,
        *TIMER_COMMAND_MODELS,
        *LINEAGE_COMMAND_MODELS,
        *POLICY_COMMAND_MODELS,
        *DOCUMENT_COMMAND_MODELS,
        *EXPENSE_COMMAND_MODELS,
        *INVOICE_COMMAND_MODELS,
        *SETTLEMENT_COMMAND_MODELS,
        *WORKLOAD_COMMAND_MODELS,
        *FOLLOWUP_COMMAND_MODELS,
        *WORK_CALENDAR_MODELS,
    ):
        register(name)

    def financial_year(arguments: dict[str, Any]) -> dict[str, Any]:
        arguments.pop("request_ref", None)
        arguments.pop("request_key", None)
        try:
            with session_scope() as session:
                return FinancialYearService(session).read(
                    ReadFinancialYear.model_validate(arguments)
                )
        except ValidationError as exc:
            return public_validation_error(exc).as_dict()
        except DocketError as exc:
            return exc.as_dict()

    server.add_tool(
        model_function(ReadFinancialYear, "docket_get_financial_year", financial_year),
        name="docket_get_financial_year",
        description=("Read a pinned financial year and its decisions. Use refs to observe inputs "
                     "for financial commands. Reuse report_ref for paged JSON or CSV."),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )

    def funding(arguments: dict[str, Any]) -> dict[str, Any]:
        arguments.pop("request_ref", None)
        arguments.pop("request_key", None)
        try:
            with session_scope() as session:
                return FundingService(session).context(
                    FundingContext.model_validate(arguments), settings
                )
        except DocketError as exc:
            return exc.as_dict()
        except ValidationError as exc:
            return public_validation_error(exc).as_dict()

    server.add_tool(
        model_function(FundingContext, "docket_get_funding_context", funding),
        name="docket_get_funding_context",
        description="Read owned funding identities and observations.",
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )

    def context(arguments: dict[str, Any]) -> dict[str, Any]:
        arguments.pop("request_ref", None)
        arguments.pop("request_key", None)
        try:
            public = GetWorkContext.model_validate(arguments)
            with session_scope() as session:
                return NativeContextService(session, settings).work(public.refs, public.search)
        except ValidationError as exc:
            return public_validation_error(exc).as_dict()
        except DocketError as exc:
            return exc.as_dict()

    def workload(arguments: dict[str, Any]) -> dict[str, Any]:
        arguments.pop("request_ref", None)
        arguments.pop("request_key", None)
        try:
            public = GetWorkloadContext.model_validate(arguments)
            with session_scope() as session:
                return WorkloadContextService(session, settings).query(public)
        except ValidationError as exc:
            return public_validation_error(exc).as_dict()
        except DocketError as exc:
            return exc.as_dict()

    server.add_tool(
        model_function(GetWorkloadContext, "docket_get_workload_context", workload),
        name="docket_get_workload_context",
        description=WORKLOAD_TOOL_PURPOSES["docket_get_workload_context"],
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )

    server.add_tool(
        model_function(GetWorkContext, "docket_get_work_context", context),
        name="docket_get_work_context",
        description=WORK_TOOL_PURPOSES["docket_get_work_context"],
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )

    def query(arguments: dict[str, Any]) -> dict[str, Any]:
        arguments.pop("request_ref", None)
        arguments.pop("request_key", None)
        try:
            public = QueryWorkSessions.model_validate(arguments)
            with session_scope() as session:
                return WorkReportService(session, settings).query(public)
        except ValidationError as exc:
            return public_validation_error(exc).as_dict()
        except DocketError as exc:
            return exc.as_dict()

    server.add_tool(
        model_function(QueryWorkSessions, "docket_query_work_sessions", query),
        name="docket_query_work_sessions",
        description=WORK_TOOL_PURPOSES["docket_query_work_sessions"],
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )

    def compensation(arguments: dict[str, Any]) -> dict[str, Any]:
        arguments.pop("request_ref", None)
        arguments.pop("request_key", None)
        try:
            public = GetCompensation.model_validate(arguments)
            with session_scope() as session:
                return CompensationEvaluationService(session, settings).query(public)
        except ValidationError as exc:
            return public_validation_error(exc).as_dict()
        except DocketError as exc:
            return exc.as_dict()

    server.add_tool(
        model_function(GetCompensation, "docket_get_compensation", compensation),
        name="docket_get_compensation",
        description=POLICY_TOOL_PURPOSES["docket_get_compensation"],
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )

    def document(arguments: dict[str, Any]) -> dict[str, Any]:
        arguments.pop("request_ref", None)
        arguments.pop("request_key", None)
        try:
            public = ReadDocument.model_validate(arguments)
            with session_scope() as session:
                return FinancialDocumentReadService(session).read(public)
        except ValidationError as exc:
            return public_validation_error(exc).as_dict()
        except DocketError as exc:
            return exc.as_dict()

    server.add_tool(
        model_function(ReadDocument, "docket_read_document", document),
        name="docket_read_document",
        description=FINANCIAL_TOOL_PURPOSES["docket_read_document"],
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )

    def expenses(arguments: dict[str, Any]) -> dict[str, Any]:
        arguments.pop("request_ref", None)
        arguments.pop("request_key", None)
        try:
            public = QueryExpenses.model_validate(arguments)
            with session_scope() as session:
                return ExpenseReportService(session, settings).query(public)
        except ValidationError as exc:
            return public_validation_error(exc).as_dict()
        except DocketError as exc:
            return exc.as_dict()

    server.add_tool(
        model_function(QueryExpenses, "docket_query_expenses", expenses),
        name="docket_query_expenses",
        description=EXPENSE_TOOL_PURPOSES["docket_query_expenses"],
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )

    def financial(arguments: dict[str, Any]) -> dict[str, Any]:
        arguments.pop("request_ref", None)
        arguments.pop("request_key", None)
        try:
            public = GetFinancialContext.model_validate(arguments)
            with session_scope() as session:
                if public.intake_ref:
                    from docket.plugin.financial_intake import intake_context

                    return intake_context(
                        session,
                        settings,
                        public.intake_ref,
                        public.refs,
                        public.cursor,
                        public.limit,
                    )
                result = FinancialContextService(session, settings).query(public)
                if public.format == "summary" and not public.cursor:
                    from sqlalchemy import select

                    from docket.agent_auth import require_principal
                    from docket.models.financial_intake import FinancialIntake
                    from docket.services.financial_intake import FinancialIntakeService

                    service = FinancialIntakeService(session, settings)
                    owner = require_principal("read").operator_ref
                    result["linked_intake"] = {
                        ref: service.related_evidence(ref, owner)
                        for ref in public.refs
                        if ref.startswith("exp_")
                    }
                    result["recent_intake_refs"] = list(
                        session.scalars(
                            select(FinancialIntake.source_ref)
                            .where(
                                FinancialIntake.operator_ref
                                == require_principal("read").operator_ref
                            )
                            .order_by(FinancialIntake.created_at.desc())
                            .limit(8)
                        )
                    )
                return result
        except ValidationError as exc:
            return public_validation_error(exc).as_dict()
        except DocketError as exc:
            return exc.as_dict()

    server.add_tool(
        model_function(GetFinancialContext, "docket_get_financial_context", financial),
        name="docket_get_financial_context",
        description=SETTLEMENT_TOOL_PURPOSES["docket_get_financial_context"],
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
