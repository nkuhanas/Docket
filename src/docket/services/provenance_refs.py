from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.errors import DocketError
from docket.domain.public_refs import parse_public_ref
from docket.models import (
    Affiliation,
    AgentResponse,
    AttentionCase,
    AttentionCaseRevision,
    AuditEvent,
    AuthenticatedRequest,
    BriefEntry,
    CalendarLane,
    CanonicalEvent,
    CaseItem,
    ChangeSet,
    CompensationEvaluation,
    Conflict,
    ContextPacket,
    ConversationRecord,
    DailyBrief,
    Decision,
    DocumentAllocation,
    Engagement,
    EngagementRevision,
    Entity,
    Expense,
    ExpenseObservation,
    ExpenseRevision,
    Fact,
    FinancialDocument,
    FinancialDocumentObservation,
    FinancialDocumentRevision,
    FinancialEntry,
    FinancialTransaction,
    GoogleTaskList,
    GoogleTaskObservation,
    IdentityHandle,
    IntentSession,
    IntentTurn,
    Interaction,
    InterpretedStatement,
    Item,
    LaneRoutingDecision,
    Operation,
    OperatorProjection,
    OperatorUtterance,
    Payment,
    PersistedSemanticOption,
    Preference,
    Relationship,
    ReminderPlan,
    SemanticRequest,
    SemanticRequestAttempt,
    Source,
    Task,
    TaskBindingRevision,
    TaskListPolicyRevision,
    TaskPolicyEffect,
    TaskProviderBinding,
    TaskReminderAuthority,
    TaskReminderGeneration,
    TaskSyncConflict,
    TemporalBinding,
    TemporalCalendarProjection,
    ToolInvocation,
    TriageRun,
    WorkCalendarProjection,
    WorkCalendarRevision,
    WorkClaim,
    WorkloadReconciliation,
    WorkSession,
    WorkSessionRevision,
)

PROVENANCE_PREFIXES = frozenset(
    {
        "tlist",
        "tpol",
        "tobs",
        "tbnd",
        "tbrv",
        "tscf",
        "teff",
        "trgen",
        "pay",
        "ftxn",
        "fent",
        "fdoc",
        "fdrev",
        "clm",
        "ceval",
        "alloc",
        "fobs",
        "exp",
        "exrev",
        "exobs",
        "eng",
        "erev",
        "work",
        "wrev",
        "wrec",
        "wcal",
        "wcrv",
        "utt",
        "req",
        "rec",
        "src",
        "stm",
        "dec",
        "conf",
        "chg",
        "ent",
        "idn",
        "aff",
        "rel",
        "fact",
        "int",
        "pref",
        "lane",
        "route",
        "evt",
        "rsp",
        "tri",
        "case",
        "caserev",
        "item",
        "task",
        "time",
        "tproj",
        "rem",
        "brief",
        "ctx",
        "ses",
        "turn",
        "call",
        "aud",
        "op",
        "proj",
        "opt",
        "citem",
        "bentry",
        "sattempt",
        "sreq",
    }
)

_PHASE_TWO_MODELS: dict[str, type[Any]] = {
    "tlist": GoogleTaskList,
    "tpol": TaskListPolicyRevision,
    "tobs": GoogleTaskObservation,
    "tbnd": TaskProviderBinding,
    "tbrv": TaskBindingRevision,
    "tscf": TaskSyncConflict,
    "teff": TaskPolicyEffect,
    "trgen": TaskReminderGeneration,
    "wrec": WorkloadReconciliation,
    "wcal": WorkCalendarProjection,
    "wcrv": WorkCalendarRevision,
    "pay": Payment,
    "ftxn": FinancialTransaction,
    "fent": FinancialEntry,
    "fobs": FinancialDocumentObservation,
    "alloc": DocumentAllocation,
    "exp": Expense,
    "exrev": ExpenseRevision,
    "exobs": ExpenseObservation,
    "fdoc": FinancialDocument,
    "fdrev": FinancialDocumentRevision,
    "clm": WorkClaim,
    "ceval": CompensationEvaluation,
    "eng": Engagement,
    "erev": EngagementRevision,
    "work": WorkSession,
    "wrev": WorkSessionRevision,
    "req": AuthenticatedRequest,
    "rec": ConversationRecord,
    "utt": OperatorUtterance,
    "src": Source,
    "stm": InterpretedStatement,
    "dec": Decision,
    "conf": Conflict,
    "chg": ChangeSet,
    "ent": Entity,
    "idn": IdentityHandle,
    "aff": Affiliation,
    "rel": Relationship,
    "fact": Fact,
    "int": Interaction,
    "lane": CalendarLane,
    "pref": Preference,
    "route": LaneRoutingDecision,
    "evt": CanonicalEvent,
    "rsp": AgentResponse,
    "brief": DailyBrief,
    "tri": TriageRun,
    "ctx": ContextPacket,
    "case": AttentionCase,
    "caserev": AttentionCaseRevision,
    "item": Item,
    "task": Task,
    "time": TemporalBinding,
    "tproj": TemporalCalendarProjection,
    "rem": ReminderPlan,
    "citem": CaseItem,
    "bentry": BriefEntry,
    "ses": IntentSession,
    "turn": IntentTurn,
    "call": ToolInvocation,
    "aud": AuditEvent,
    "op": Operation,
    "proj": OperatorProjection,
    "opt": PersistedSemanticOption,
    "sattempt": SemanticRequestAttempt,
    "sreq": SemanticRequest,
}


class ProvenanceRefService:
    """Validate the frozen typed provenance algebra and trace authority bases."""

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def prefix(ref_id: str) -> str:
        try:
            prefix, _payload = parse_public_ref(ref_id)
        except ValueError as exc:
            raise DocketError(
                code="invalid_provenance_ref",
                message="Basis values must be typed Docket public references.",
                details={"ref": ref_id},
            ) from exc
        if prefix not in PROVENANCE_PREFIXES:
            raise DocketError(
                code="invalid_provenance_ref",
                message="This public-reference type cannot be semantic provenance.",
                details={"ref": ref_id},
            )
        return prefix

    def get(self, ref_id: str) -> Any:
        prefix = self.prefix(ref_id)
        model = _PHASE_TWO_MODELS.get(prefix)
        if model is None:
            raise DocketError(
                code="provenance_type_not_migrated",
                message="This provenance type is not available in the current migration phase.",
                details={"ref": ref_id, "prefix": prefix},
            )
        item = self.session.scalar(select(model).where(model.ref_id == ref_id))
        if item is None:
            raise DocketError(
                code="provenance_ref_not_found",
                message="A referenced provenance object does not exist.",
                details={"ref": ref_id},
            )
        if isinstance(item, WorkCalendarProjection | WorkCalendarRevision):
            from docket.services.work_calendar import WorkCalendarService

            WorkCalendarService(self.session).head(
                item.projection_ref if isinstance(item, WorkCalendarRevision) else item.ref_id
            )
        if isinstance(item, WorkloadReconciliation):
            from docket.services.work_attribution import read_operator

            if item.operator_ref != read_operator():
                raise DocketError(
                    code="workload_record_unavailable", message="Select owned records."
                )
        if isinstance(item, Payment | FinancialTransaction | FinancialEntry):
            from docket.services.work_attribution import read_operator

            if item.operator_ref != read_operator():
                raise DocketError(
                    code="financial_record_unavailable", message="Select owned records."
                )
        if isinstance(item, Expense | ExpenseRevision | ExpenseObservation):
            from docket.services.expenses import ExpenseService

            ExpenseService(self.session).head(
                item.ref_id if isinstance(item, Expense) else item.expense_ref
            )
        if isinstance(
            item,
            (
                FinancialDocument
                | FinancialDocumentRevision
                | WorkClaim
                | DocumentAllocation
                | FinancialDocumentObservation
                | CompensationEvaluation
            ),
        ):
            from docket.services.financial_documents import FinancialDocumentService

            if isinstance(item, CompensationEvaluation):
                from docket.agent_auth import require_principal

                if item.operator_ref != require_principal("read").operator_ref:
                    raise DocketError(
                        code="financial_basis_unavailable", message="Select owned work."
                    )
            else:
                FinancialDocumentService(self.session).head(
                    item.ref_id if isinstance(item, FinancialDocument) else item.document_ref
                )
        if isinstance(item, Engagement | WorkSession | EngagementRevision | WorkSessionRevision):
            from docket.services.work_attribution import WorkAttributionService

            if isinstance(item, EngagementRevision | WorkSessionRevision):
                WorkAttributionService(self.session).head(
                    Engagement if isinstance(item, EngagementRevision) else WorkSession,
                    item.engagement_ref if isinstance(item, EngagementRevision) else item.work_ref,
                )
            else:
                WorkAttributionService(self.session).head(type(item), item.ref_id)
        if isinstance(item, GoogleTaskList | TaskListPolicyRevision):
            from docket.services.work_attribution import read_operator

            if item.operator_ref != read_operator():
                raise DocketError(
                    code="task_basis_unavailable", message="Select owned task records."
                )
        elif isinstance(
            item,
            GoogleTaskObservation
            | TaskProviderBinding
            | TaskBindingRevision
            | TaskSyncConflict
            | TaskPolicyEffect,
        ):
            if isinstance(item, TaskPolicyEffect):
                if item.reminder_generation_ref is not None:
                    from docket.services.work_attribution import read_operator

                    authority = self.session.get(
                        TaskReminderAuthority, item.reminder_generation_ref
                    )
                    if authority is None or authority.operator_ref != read_operator():
                        raise DocketError(
                            code="task_basis_unavailable", message="Select owned reminder evidence."
                        )
                    return item
                policy = self.session.get(TaskListPolicyRevision, item.policy_ref)
                list_ref = policy.task_list_ref if policy else None
            elif isinstance(item, TaskSyncConflict):
                binding = self.session.get(TaskProviderBinding, item.binding_ref)
                list_ref = binding.task_list_ref if binding else None
            else:
                list_ref = item.task_list_ref
            listing = self.session.get(GoogleTaskList, list_ref) if list_ref else None
            from docket.services.work_attribution import read_operator

            if listing is None or listing.operator_ref != read_operator():
                raise DocketError(
                    code="task_basis_unavailable", message="Select owned task records."
                )
        return item

    def require_all(self, refs: Iterable[str]) -> list[Any]:
        seen: set[str] = set()
        items: list[Any] = []
        for ref_id in refs:
            if ref_id in seen:
                raise DocketError(
                    code="duplicate_provenance_ref",
                    message="A provenance reference list contains a duplicate.",
                    details={"ref": ref_id},
                )
            seen.add(ref_id)
            items.append(self.get(ref_id))
        return items

    def authority_utterance_refs(self, basis_refs: Iterable[str]) -> set[str]:
        return {ref for ref in self.authority_root_refs(basis_refs) if ref.startswith("utt_")}

    def authority_root_refs(self, basis_refs: Iterable[str]) -> set[str]:
        utterance_refs: set[str] = set()
        visited: set[str] = set()
        stack = list(basis_refs)
        while stack:
            ref_id = stack.pop()
            if ref_id in visited:
                continue
            visited.add(ref_id)
            item = self.get(ref_id)
            if isinstance(item, Engagement | WorkSession):
                stack.append(item.created_by_changeset_ref)
            elif isinstance(item, EngagementRevision | WorkSessionRevision):
                stack.extend(item.basis_refs)
                stack.append(item.created_by_changeset_ref)
            elif isinstance(item, TaskPolicyEffect):
                # This is a bounded service root. The authority engine separately
                # verifies its exact effects and current standing policy.
                utterance_refs.add(item.ref_id)
            elif isinstance(item, OperatorUtterance | AuthenticatedRequest):
                utterance_refs.add(item.ref_id)
            elif isinstance(item, InterpretedStatement):
                if item.authenticated_request_ref is not None:
                    stack.append(item.authenticated_request_ref)
                utterance_ref = self.session.scalar(
                    select(OperatorUtterance.ref_id).where(
                        OperatorUtterance.id == item.utterance_id
                    )
                )
                if utterance_ref is not None:
                    stack.append(utterance_ref)
            elif isinstance(item, Decision | ChangeSet | AgentResponse | AuditEvent | Operation):
                stack.extend(item.basis_refs)
            elif isinstance(item, Conflict):
                stack.extend(item.prior_statement_refs)
                stack.extend(item.incoming_statement_refs)
            elif isinstance(item, IntentSession):
                stack.extend(
                    ref
                    for ref in (
                        item.source_utterance_ref,
                        item.source_request_ref,
                        item.source_policy_effect_ref,
                    )
                    if ref
                )
            elif isinstance(item, IntentTurn):
                stack.extend(ref for ref in (item.utterance_ref, item.request_ref) if ref)
            elif isinstance(item, ToolInvocation):
                stack.extend(item.utterance_refs)
            elif isinstance(item, SemanticRequest):
                stack.extend(item.origin_utterance_refs)
                if item.authenticated_request_ref is not None:
                    stack.append(item.authenticated_request_ref)
            elif isinstance(
                item,
                Entity
                | IdentityHandle
                | Affiliation
                | Relationship
                | Fact
                | Interaction
                | Preference
                | CalendarLane
                | LaneRoutingDecision
                | CanonicalEvent
                | Item
                | Task
                | TemporalBinding
                | TemporalCalendarProjection
                | ReminderPlan,
            ):
                stack.extend(item.basis_refs)
        return utterance_refs
