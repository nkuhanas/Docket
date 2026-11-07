"""Admit clock/delete changes from one genuine, owned Calendar companion."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import Settings, get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.models import (
    AuthenticatedRequest,
    ChangeSet,
    IntentSession,
    OAuthPrincipal,
    ProviderAccount,
    ProviderEventBinding,
    Task,
    TaskPolicyEffect,
    TaskReminderAuthority,
    TaskReminderGeneration,
)
from docket.providers.google.calendar import CalendarSnapshotEvent
from docket.providers.google.oauth import CALENDAR_EVENTS_SCOPE, authorized_user_file_status
from docket.schemas.authority import ChangeSetCommit, ChangeSetContent, ChangeSetPrepare
from docket.services.change_sets import _content_payload
from docket.services.task_policy_authority import PLANNER_VERSION, TaskPolicyAuthority
from docket.services.task_reminder_origins import task_reminder_origins


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def retain_authority(
    session: Session,
    generation: TaskReminderGeneration,
    changeset: ChangeSet,
    previous: TaskReminderGeneration | None,
) -> None:
    intent = session.get(IntentSession, changeset.intent_session_id)
    request = (
        session.scalar(
            select(AuthenticatedRequest).where(
                AuthenticatedRequest.ref_id == intent.source_request_ref,
            )
        )
        if intent is not None and intent.source_request_ref
        else None
    )
    if request is not None:
        principal = (
            session.scalar(
                select(OAuthPrincipal).where(
                    OAuthPrincipal.ref_id == request.principal_ref,
                )
            )
            if request.principal_ref.startswith("prn_")
            else None
        )
        authority = TaskReminderAuthority(
            generation_ref=generation.ref_id,
            operator_ref=request.operator_ref,
            principal_ref=request.principal_ref,
            authorized_request_ref=request.ref_id,
            grant_epoch=principal.valid_after if principal is not None else None,
        )
    else:
        old = session.get(TaskReminderAuthority, previous.ref_id) if previous else None
        if old is None:
            raise TaskPolicyAuthority.denied()
        authority = TaskReminderAuthority(
            generation_ref=generation.ref_id,
            operator_ref=old.operator_ref,
            principal_ref=old.principal_ref,
            authorized_request_ref=old.authorized_request_ref,
            grant_epoch=old.grant_epoch,
        )
    session.add(authority)
    session.flush()


class TaskReminderInbound:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session, self.settings = session, settings or get_settings()

    def grant(
        self,
        generation: TaskReminderGeneration,
        *,
        lock: bool = False,
    ) -> TaskReminderAuthority:
        authority = self.session.get(TaskReminderAuthority, generation.ref_id)
        if authority is None or authority.operator_ref != self.settings.canonical_operator_ref:
            raise TaskPolicyAuthority.denied()
        request = self.session.scalar(
            select(AuthenticatedRequest).where(
                AuthenticatedRequest.ref_id == authority.authorized_request_ref,
            )
        )
        if (
            request is None
            or request.state == "cancelled"
            or "commit" not in request.permissions
            or request.operator_ref != authority.operator_ref
            or request.principal_ref != authority.principal_ref
        ):
            raise TaskPolicyAuthority.denied()
        if authority.principal_ref.startswith("prn_"):
            query = select(OAuthPrincipal).where(
                OAuthPrincipal.ref_id == authority.principal_ref,
            )
            if lock:
                query = query.with_for_update().execution_options(populate_existing=True)
            principal = self.session.scalar(query)
            if (
                principal is None
                or not principal.enabled
                or principal.operator_ref != authority.operator_ref
                or principal.issuer != self.settings.plugin_oauth_issuer
                or principal.valid_after != authority.grant_epoch
                or self.settings.plugin_write_scope not in principal.scopes
            ):
                raise TaskPolicyAuthority.denied()
        elif authority.principal_ref == "agent:interactive":
            expires = self.settings.interactive_agent_expires_at
            if not self.settings.interactive_agent_enabled or (
                expires is not None and aware(expires) <= datetime.now(UTC)
            ):
                raise TaskPolicyAuthority.denied()
        else:
            raise TaskPolicyAuthority.denied()
        if (
            self.settings.environment.value == "production"
            and authorized_user_file_status(
                self.settings.google_oauth_token_file,
                required_scopes=[CALENDAR_EVENTS_SCOPE],
            )
            != "configured"
        ):
            raise TaskPolicyAuthority.denied()
        return authority

    def require(self, effect: TaskPolicyEffect, content: Any, *, lock: bool = False) -> None:
        generation = self.session.get(TaskReminderGeneration, effect.reminder_generation_ref)
        if generation is None:
            raise TaskPolicyAuthority.denied()
        self.grant(generation)
        snapshot = effect.reminder_observation or {}
        binding = (
            self.session.scalar(
                select(ProviderEventBinding)
                .where(
                    ProviderEventBinding.canonical_target_ref == generation.projection_ref,
                    ProviderEventBinding.target_kind == "temporal_projection",
                )
                .with_for_update()
            )
            if lock
            else self.session.scalar(
                select(ProviderEventBinding).where(
                    ProviderEventBinding.canonical_target_ref == generation.projection_ref,
                    ProviderEventBinding.target_kind == "temporal_projection",
                )
            )
        )
        task = self.session.scalar(select(Task).where(Task.ref_id == generation.task_ref))
        account_query = (
            select(ProviderAccount).where(
                ProviderAccount.id == binding.account_id,
            )
            if binding
            else None
        )
        if account_query is not None and lock:
            account_query = account_query.with_for_update().execution_options(
                populate_existing=True
            )
        account = self.session.scalar(account_query) if account_query is not None else None
        if lock:
            self.grant(generation, lock=True)
        if (
            task is None
            or task.canonical_status != "active"
            or task.remind_at is None
            or task.reminder_generation != generation.generation
            or task.task_state in {"completed", "cancelled"}
            or binding is None
            or binding.version != effect.expected_binding_version
            or binding.provider_etag != snapshot.get("etag")
            or binding.calendar_id != snapshot.get("calendar_id")
            or binding.provider_event_id != snapshot.get("event_id")
            or str(binding.account_id) != snapshot.get("account_id")
            or account is None
            or not account.enabled
            or account.provider != "google"
            or "google_calendar" not in account.capabilities
        ):
            raise TaskPolicyAuthority.denied()
        origin = task_reminder_origins(self.session, binding.account_id, binding.calendar_id).get(
            binding.provider_event_id,
        )
        if origin is None or origin.projection_ref != generation.projection_ref:
            raise TaskPolicyAuthority.denied()
        if content is None:
            return
        if (
            _content_payload(content) != effect.content
            or sha256_json(effect.content) != effect.effect_hash
        ):
            raise TaskPolicyAuthority.denied()
        if (
            any(
                getattr(content, key)
                for key in (
                    "registry_changes",
                    "preference_changes",
                    "lane_changes",
                    "event_changes",
                    "resolution_changes",
                    "provider_intents",
                )
            )
            or len(content.tracked_context_changes) != 1
        ):
            raise TaskPolicyAuthority.denied()
        change = content.tracked_context_changes[0]
        if (
            change.object_type != "task"
            or change.action != "update"
            or change.object_ref != generation.task_ref
            or change.payload.model_fields_set != {"remind_at", "reminder_timezone"}
        ):
            raise TaskPolicyAuthority.denied()
        expected_at = snapshot.get("start_at") if snapshot.get("status") != "cancelled" else None
        actual_at = (
            aware(change.payload.remind_at).isoformat() if change.payload.remind_at else None
        )
        if actual_at != expected_at or change.payload.reminder_timezone != (
            task.reminder_timezone if expected_at is not None else None
        ):
            raise TaskPolicyAuthority.denied()

    def reconcile(
        self,
        generation: TaskReminderGeneration,
        event: CalendarSnapshotEvent,
        binding: ProviderEventBinding,
        *,
        read_started_at: datetime,
    ) -> str:
        task = self.session.scalar(select(Task).where(Task.ref_id == generation.task_ref))
        if (
            task is None
            or task.remind_at is None
            or task.reminder_generation != generation.generation
            or task.canonical_status != "active"
            or task.task_state in {"completed", "cancelled"}
            or aware(generation.created_at) > aware(read_started_at)
        ):
            return "retired"
        if event.provider_etag is None:
            return "unverifiable"
        if event.status != "cancelled" and (
            event.is_all_day
            or event.start_at is None
            or event.end_at is None
            or event.recurrence_kind != "one_time"
        ):
            return "unsupported_edit"
        at = aware(event.start_at) if event.start_at and event.status != "cancelled" else None
        repair = event.status != "cancelled" and (
            event.visibility != "private"
            or event.transparency != "transparent"
            or event.provider_reminders
            != {"useDefault": False, "overrides": [{"method": "popup", "minutes": 0}]}
            or event.end_at is None
            or at is None
            or (aware(event.end_at) - at).total_seconds() != 60
        )
        if at == aware(task.remind_at) and not repair:
            return "unchanged"
        try:
            self.grant(generation)
        except DocketError:
            return "authority_unavailable"
        ref = new_public_ref("teff")
        basis = [ref, generation.created_by_changeset_ref]
        content = ChangeSetContent.model_validate(
            {
                "basis_refs": basis,
                "expected_versions": {task.ref_id: task.version},
                "tracked_context_changes": [
                    {
                        "mutation_type": "task_modify",
                        "change_id": "observed-reminder",
                        "object_type": "task",
                        "action": "update",
                        "object_ref": task.ref_id,
                        "basis_refs": basis,
                        "affected_fields": ["remind_at", "reminder_timezone"],
                        "payload": {
                            "remind_at": at.isoformat() if at else None,
                            "reminder_timezone": task.reminder_timezone if at else None,
                        },
                    }
                ],
            }
        )
        payload = _content_payload(content)
        effect = TaskPolicyEffect(
            ref_id=ref,
            reminder_generation_ref=generation.ref_id,
            reminder_observation={
                "account_id": str(binding.account_id),
                "calendar_id": binding.calendar_id,
                "event_id": event.provider_event_id,
                "etag": event.provider_etag,
                "status": event.status,
                "start_at": at.isoformat() if at else None,
                "read_started_at": aware(read_started_at).isoformat(),
                "repair_required": repair,
            },
            effect_hash=sha256_json(payload),
            content=payload,
            planner_version=PLANNER_VERSION,
            expected_binding_version=binding.version,
        )
        self.session.add(effect)
        self.session.flush()
        intent = IntentSession(
            conversation_ref="task-reminder:" + generation.task_ref,
            source_policy_effect_ref=effect.ref_id,
            semantic_state="ready",
        )
        self.session.add(intent)
        self.session.flush()
        from docket.services.interactive_authority import InteractiveAuthorityService

        engine = InteractiveAuthorityService(self.session).changesets
        prepared, _ = engine.prepare(
            ChangeSetPrepare(
                intent_session_ref=intent.ref_id,
                expected_session_version=intent.version,
                idempotency_key="task-reminder:" + effect.ref_id,
                content=content,
            )
        )
        if prepared.state != "validated":
            raise DocketError(
                code="task_reminder_import_failed",
                message="The owned reminder change needs reconciliation.",
                details={"reasons": [error["code"] for error in prepared.validation_errors]},
            )
        engine.commit(
            ChangeSetCommit(
                changeset_ref=prepared.ref_id,
                expected_version=prepared.version,
                idempotency_key=prepared.idempotency_key,
                authority_policy_effect_ref=effect.ref_id,
            )
        )
        return "reconciled"
