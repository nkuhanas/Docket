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
    BriefEntry,
    CalendarLane,
    CanonicalEvent,
    CaseItem,
    ChangeSet,
    Conflict,
    ContextPacket,
    DailyBrief,
    Decision,
    Entity,
    Fact,
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
    PersistedSemanticOption,
    Preference,
    Relationship,
    ReminderPlan,
    SemanticRequest,
    SemanticRequestAttempt,
    Source,
    Task,
    TemporalBinding,
    TemporalCalendarProjection,
    ToolInvocation,
    TriageRun,
)

PROVENANCE_PREFIXES = frozenset(
    {
        "utt",
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
        utterance_refs: set[str] = set()
        visited: set[str] = set()
        stack = list(basis_refs)
        while stack:
            ref_id = stack.pop()
            if ref_id in visited:
                continue
            visited.add(ref_id)
            item = self.get(ref_id)
            if isinstance(item, OperatorUtterance):
                utterance_refs.add(item.ref_id)
            elif isinstance(item, InterpretedStatement):
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
                stack.append(item.source_utterance_ref)
            elif isinstance(item, IntentTurn):
                stack.append(item.utterance_ref)
            elif isinstance(item, ToolInvocation):
                stack.extend(item.utterance_refs)
            elif isinstance(item, SemanticRequest):
                stack.extend(item.origin_utterance_refs)
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
