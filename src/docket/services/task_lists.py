"""Selected-list policy changes through the canonical authority engine."""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.domain.errors import DocketError
from docket.models import (
    AuditEvent,
    CalendarLane,
    ChangeSet,
    GoogleTaskList,
    IntentSession,
    TaskListPolicyRevision,
    TaskSyncState,
)
from docket.schemas.native_tasks import TaskListPolicySpec
from docket.services.agent_requests import AgentRequestService
from docket.services.notifications import identity_lock


class TaskListService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def owned(self, ref: str, *, lock: bool = False) -> GoogleTaskList:
        principal = require_principal("read")
        query = select(GoogleTaskList).where(
            GoogleTaskList.ref_id == ref,
            GoogleTaskList.operator_ref == principal.operator_ref,
        )
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        row = self.session.scalar(query)
        if row is None:
            raise DocketError(
                code="task_list_unavailable", message="Select one of your task lists."
            )
        return row

    @staticmethod
    def lifecycle(mode: str) -> str:
        return (
            "Google completion completes the Task. Confirmed Google deletion retracts an "
            "unchanged linked Task while retaining history; local edits produce a conflict."
            if mode == "two_way"
            else "Observations do not change canonical Tasks."
            if mode == "observe"
            else "Preserve canonical Tasks and Google records; synchronization is suspended."
        )

    def apply(self, _session: Session, changeset: ChangeSet, change: Any) -> list[str]:
        principal = require_principal("commit")
        identity_lock(self.session, "task-list-default:" + principal.operator_ref)
        listing = self.owned(change.object_ref, lock=True)
        spec = TaskListPolicySpec.model_validate(change.payload)
        fields = spec.model_fields_set
        default = spec.is_default if "is_default" in fields else listing.is_default
        if default and spec.mode != "two_way":
            if spec.is_default:
                raise DocketError(
                    code="task_default_requires_two_way",
                    message="The default for new tasks must use two-way synchronization.",
                    details={"field": "is_default"},
                )
            default = False
        calendar_ref = (
            spec.reminder_calendar_ref
            if "reminder_calendar_ref" in fields
            else listing.reminder_calendar_ref
        )
        if calendar_ref:
            calendar = self.session.scalar(
                select(CalendarLane).where(
                    CalendarLane.ref_id == calendar_ref,
                    CalendarLane.status == "active",
                    CalendarLane.enabled.is_(True),
                )
            )
            if calendar is None:
                raise DocketError(
                    code="reminder_calendar_unavailable",
                    message="Select an active authorized calendar.",
                    details={"field": "reminder_calendar_ref"},
                )
        intent = self.session.get(IntentSession, changeset.intent_session_id)
        if intent is None or intent.source_request_ref is None:
            raise DocketError(
                code="task_policy_requires_authenticated_request",
                message="Configure task lists through the authenticated native connection.",
            )
        request = AgentRequestService(self.session).require(
            intent.source_request_ref, permission="commit"
        )
        affected = [listing.ref_id]
        if default:
            previous = self.session.scalars(
                select(GoogleTaskList)
                .where(
                    GoogleTaskList.operator_ref == principal.operator_ref,
                    GoogleTaskList.is_default.is_(True),
                    GoogleTaskList.ref_id != listing.ref_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
            for other in previous:
                other.is_default = False
                other.version += 1
                affected.append(other.ref_id)
                self.session.add(
                    AuditEvent(
                        event_type="task_list.default_relinquished",
                        primary_ref=other.ref_id,
                        actor_type="agent",
                        actor_id=principal.principal_ref,
                        affected_refs=[other.ref_id, listing.ref_id],
                        basis_refs=list(change.basis_refs),
                        data={
                            "version": other.version,
                            "created_by_changeset_ref": changeset.ref_id,
                        },
                    )
                )
            # The previous list keeps its standing policy/principal. Changing the
            # default must not reauthorize a revoked connection's background scope.
            self.session.flush()
        revision = 1 + (
            self.session.scalar(
                select(func.max(TaskListPolicyRevision.revision)).where(
                    TaskListPolicyRevision.task_list_ref == listing.ref_id,
                )
            )
            or 0
        )
        policy = TaskListPolicyRevision(
            task_list_ref=listing.ref_id,
            revision=revision,
            operator_ref=principal.operator_ref,
            mode=spec.mode,
            propagate_google_deletion=spec.mode == "two_way",
            is_default=bool(default),
            reminder_calendar_ref=calendar_ref,
            authorized_request_ref=request.ref_id,
            principal_ref=principal.principal_ref,
            grant_epoch=principal.grant_epoch,
            created_by_changeset_ref=changeset.ref_id,
        )
        self.session.add(policy)
        self.session.flush()
        listing.policy_ref = policy.ref_id
        listing.policy_mode = spec.mode
        listing.is_default = bool(default)
        listing.reminder_calendar_ref = calendar_ref
        listing.version += 1
        sync = self.session.get(TaskSyncState, listing.ref_id)
        if sync is None:
            sync = TaskSyncState(task_list_ref=listing.ref_id)
            self.session.add(sync)
        sync.requested = spec.mode != "disabled"
        sync.next_attempt_at = None
        # Already-sent effects retain their own delivery receipts. A policy
        # revision discards only an in-flight read's old pagination admission.
        sync.generation, sync.page_token = None, None
        sync.lease_token, sync.leased_until = None, None
        sync.status = "disabled" if spec.mode == "disabled" else "pending"
        self.session.add(
            AuditEvent(
                event_type="task_list.policy_changed",
                actor_type="agent",
                actor_id=principal.principal_ref,
                primary_ref=listing.ref_id,
                affected_refs=[*affected, policy.ref_id],
                basis_refs=list(change.basis_refs),
                data={
                    "version": listing.version,
                    "mode": spec.mode,
                    "is_default": bool(default),
                    "reminder_calendar_ref": calendar_ref,
                    "policy_ref": policy.ref_id,
                    "created_by_changeset_ref": changeset.ref_id,
                    "lifecycle": self.lifecycle(spec.mode),
                },
            )
        )
        return affected
