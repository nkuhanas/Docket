from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.enums import ActionStatus, OperationStatus, QueueItemStatus
from docket.domain.errors import DocketError, IdempotencyConflict
from docket.models import (
    Account,
    Action,
    ActionRevision,
    AuditEvent,
    CalendarLane,
    CanonicalEvent,
    ChangeSet,
    Operation,
    ProviderEventBinding,
    QueueItem,
)
from docket.models.base import utc_now
from docket.policy.actions import get_action_definition
from docket.schemas.authority import ProviderIntentInput

_SUPPORTED_OPERATION_TYPES = frozenset(
    {
        "calendar_configure_lane",
        "calendar_delete_lane",
        "calendar_create_event",
        "calendar_update_event",
        "calendar_update_reminders",
        "calendar_cancel_event",
    }
)


class ProviderIntentService:
    """Materialize provider effects after canonical state commits in the same DB tx."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _account(self, binding: str) -> Account:
        value = binding
        account: Account | None = None
        if binding.startswith("account:"):
            try:
                account = self.session.get(Account, uuid.UUID(binding.removeprefix("account:")))
            except ValueError:
                account = None
        elif binding.startswith("google:"):
            value = binding.removeprefix("google:")
            account = self.session.scalar(
                select(Account).where(
                    Account.provider == "google",
                    Account.external_account_id == value,
                )
            )
        else:
            matches = list(
                self.session.scalars(
                    select(Account).where(
                        Account.provider == "google",
                        (Account.external_account_id == value)
                        | (Account.email_address == value),
                    )
                )
            )
            account = matches[0] if len(matches) == 1 else None
        if (
            account is None
            or not account.enabled
            or "google_calendar" not in account.capabilities
        ):
            raise DocketError(
                code="provider_target_unresolved",
                message="Provider intent does not resolve to one enabled Calendar account.",
                details={"provider_binding": binding},
            )
        return account

    def _target_refs(
        self,
        intent: ProviderIntentInput,
        refs_by_change_id: dict[str, list[str]],
    ) -> list[str]:
        refs = list(intent.canonical_target_refs)
        for change_id in intent.canonical_target_change_ids:
            resolved = refs_by_change_id.get(change_id)
            if resolved is None or len(resolved) != 1:
                raise DocketError(
                    code="provider_target_unresolved",
                    message="Provider target create reference did not resolve exactly once.",
                    details={"change_id": change_id},
                )
            if resolved[0] not in refs:
                refs.append(resolved[0])
        return refs

    def _targets(
        self, refs: list[str]
    ) -> tuple[CanonicalEvent | None, CalendarLane | None]:
        event = self.session.scalar(
            select(CanonicalEvent).where(CanonicalEvent.ref_id.in_(refs))
        )
        lane = self.session.scalar(
            select(CalendarLane).where(CalendarLane.ref_id.in_(refs))
        )
        if event is not None:
            event_lane = self.session.scalar(
                select(CalendarLane).where(CalendarLane.ref_id == event.lane_ref)
            )
            if lane is not None and event_lane is not None and lane.id != event_lane.id:
                raise DocketError(
                    code="provider_target_mismatch",
                    message="Provider intent event and lane targets do not match.",
                )
            lane = event_lane or lane
        return event, lane

    def _event_binding(
        self, event: CanonicalEvent, account: Account, lane: CalendarLane
    ) -> ProviderEventBinding:
        binding = self.session.scalar(
            select(ProviderEventBinding).where(
                ProviderEventBinding.canonical_event_id == event.id,
                ProviderEventBinding.account_id == account.id,
                ProviderEventBinding.calendar_id == lane.calendar_id,
            )
        )
        if binding is None:
            raise DocketError(
                code="provider_event_binding_required",
                message="Provider event update requires an exact existing provider binding.",
            )
        return binding

    def _parameters(
        self,
        operation_type: str,
        *,
        event: CanonicalEvent | None,
        lane: CalendarLane | None,
        account: Account,
        hints: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if operation_type in {"calendar_configure_lane", "calendar_delete_lane"}:
            if lane is None or lane.account_id != account.id:
                raise DocketError(
                    code="calendar_lane_unresolved",
                    message="Lane provider intent requires one matching CalendarLane.",
                )
            lane_parameters: dict[str, Any] = {
                "lane_id": str(lane.id),
                "lane_ref": lane.ref_id,
                "lane": lane.lane,
                "display_name": lane.display_name,
                "color_hex": lane.color_hex,
                "timezone": get_settings().timezone,
                "calendar_id": lane.calendar_id,
                "lane_version": lane.version,
            }
            lane_preview: dict[str, Any] = {
                "action_type": operation_type,
                "lane": {
                    "ref": lane.ref_id,
                    "name": lane.lane,
                    "display_name": lane.display_name,
                    "calendar_id": lane.calendar_id,
                },
            }
            return lane_parameters, lane_preview
        if event is None or lane is None or lane.account_id != account.id:
            raise DocketError(
                code="canonical_event_target_required",
                message="Calendar event provider intent requires matching event and lane targets.",
            )
        event_payload = dict(event.event_spec)
        reminder_plan = event.reminder_plan
        event_payload["reminder_plan"] = None
        event_parameters: dict[str, Any] = {
            "calendar_id": lane.calendar_id,
            "lane_ref": lane.ref_id,
            "calendar_lane": lane.lane,
            "logical_key": f"canonical:{event.ref_id}",
            "event": event_payload,
            "reminder_plan": reminder_plan,
            "reminder_plan_sha256": (
                sha256_json(reminder_plan) if reminder_plan is not None else None
            ),
            "priority": event_payload.get("priority", "normal"),
            "priority_basis": "explicit_operator",
            "target_scope": "event",
            "canonical_event_id": str(event.id),
            "canonical_event_ref": event.ref_id,
            "entity_refs": list(event.entity_refs),
            "context_labels": list(event.context_labels),
            "conflict_resolution": "not_applicable",
        }
        if operation_type != "calendar_create_event":
            binding = self._event_binding(event, account, lane)
            event_parameters.update(
                {
                    "external_event_id": binding.provider_event_id,
                    "provider_etag": binding.provider_etag,
                    "provider_before": dict(binding.provider_snapshot),
                }
            )
        if operation_type == "calendar_cancel_event" and hints.get("reason") is not None:
            event_parameters["reason"] = str(hints["reason"])
        classification = {
            "recurrence_kind": "recurring" if event_payload.get("recurrence") else "one_time",
            "system_tags": [
                "recurring" if event_payload.get("recurrence") else "one_time",
                "all_day"
                if isinstance(event_payload.get("timing"), dict)
                and event_payload["timing"].get("kind") == "all_day"
                else "timed",
                "standalone",
            ],
            "operator_tags": list(event_payload.get("operator_tags", [])),
            "priority": event_parameters["priority"],
            "priority_basis": "explicit_operator",
        }
        event_preview: dict[str, Any] = {
            "action_type": operation_type,
            "target": {
                "event_ref": event.ref_id,
                "lane_ref": lane.ref_id,
                "calendar_id": lane.calendar_id,
            },
            "event": event_payload,
            "classification": classification,
        }
        return event_parameters, event_preview

    def _predecessor(self, changeset: ChangeSet, lane: CalendarLane | None) -> Operation | None:
        if lane is None or lane.calendar_id is not None:
            return None
        candidates = list(
            self.session.scalars(
                select(Operation).where(
                    Operation.originating_changeset_ref == changeset.ref_id,
                    Operation.operation_type == "calendar_configure_lane",
                )
            )
        )
        return next(
            (item for item in candidates if lane.ref_id in item.canonical_target_refs),
            None,
        )

    def materialize(
        self,
        _session: Session,
        changeset: ChangeSet,
        intent: ProviderIntentInput,
        refs_by_change_id: dict[str, list[str]],
    ) -> list[str]:
        if intent.operation_type not in _SUPPORTED_OPERATION_TYPES:
            raise DocketError(
                code="provider_operation_not_supported",
                message="Provider operation is outside the ChangeSet authority profile.",
                details={"operation_type": intent.operation_type},
            )
        if intent.account_ref is not None or intent.provider_binding is None:
            raise DocketError(
                code="provider_target_unresolved",
                message="Current provider identities require an exact provider_binding.",
            )
        existing = self.session.scalar(
            select(Operation).where(Operation.idempotency_key == intent.idempotency_key)
        )
        if existing is not None:
            if existing.originating_changeset_ref != changeset.ref_id:
                raise IdempotencyConflict(intent.idempotency_key)
            return [existing.ref_id]
        target_refs = self._target_refs(intent, refs_by_change_id)
        event, lane = self._targets(target_refs)
        account = self._account(intent.provider_binding)
        if lane is not None and lane.account_id != account.id:
            raise DocketError(
                code="provider_target_mismatch",
                message="Provider binding does not match the canonical lane account.",
            )
        parameters, preview = self._parameters(
            intent.operation_type,
            event=event,
            lane=lane,
            account=account,
            hints=intent.parameters,
        )
        definition = get_action_definition(intent.operation_type)
        queue_item = QueueItem(
            deduplication_key=f"changeset-provider:{changeset.ref_id}:{intent.intent_id}",
            material_fingerprint=sha256_json(
                {"operation_type": intent.operation_type, "parameters": parameters}
            ),
            category="provider_operation",
            title=f"Provider operation · {intent.operation_type}"[:512],
            summary="Project committed canonical Docket state to its provider.",
            status=QueueItemStatus.EXECUTING.value,
            priority="normal",
            presentation="suppressed",
            received_at=utc_now(),
        )
        self.session.add(queue_item)
        self.session.flush()
        action = Action(
            queue_item_id=queue_item.id,
            action_type=intent.operation_type,
            status=ActionStatus.READY.value,
            current_revision=1,
        )
        self.session.add(action)
        self.session.flush()
        revision = ActionRevision(
            action_id=action.id,
            revision=1,
            action_type=intent.operation_type,
            account_id=account.id,
            parameters=parameters,
            parameters_sha256=sha256_json(parameters),
            preview=preview,
            preview_sha256=sha256_json(preview),
            risk_class=definition.risk_class.value,
            authority="explicit_user",
            target_versions={
                "queue_item": {"id": str(queue_item.id), "version": queue_item.version},
                "canonical_refs": target_refs,
            },
            created_by_actor_type="operator",
            created_by_actor_id=get_settings().operator_discord_user_id,
        )
        self.session.add(revision)
        self.session.flush()
        predecessor = self._predecessor(changeset, lane)
        if (
            intent.operation_type
            in {
                "calendar_create_event",
                "calendar_update_event",
                "calendar_update_reminders",
                "calendar_cancel_event",
            }
            and lane is not None
            and lane.calendar_id is None
            and predecessor is None
        ):
            raise DocketError(
                code="calendar_lane_provider_binding_unresolved",
                message=(
                    "An unprovisioned event lane requires a configure-lane provider "
                    "intent in the same ChangeSet."
                ),
                details={"lane_ref": lane.ref_id},
            )
        operation_id = uuid.uuid4()
        operation = Operation(
            id=operation_id,
            originating_changeset_ref=changeset.ref_id,
            basis_refs=list(intent.basis_refs),
            canonical_target_refs=target_refs,
            provenance_status="complete",
            action_revision_id=revision.id,
            approval_id=None,
            predecessor_operation_id=predecessor.id if predecessor is not None else None,
            idempotency_key=intent.idempotency_key,
            operation_type=intent.operation_type,
            account_id=account.id,
            status=OperationStatus.PENDING.value,
            provider_correlation=str(operation_id),
            next_attempt_at=utc_now(),
        )
        self.session.add(operation)
        self.session.flush()
        self.session.add(
            AuditEvent(
                event_type="operation.created",
                entity_type="operation",
                entity_id=operation.id,
                actor_type="operator",
                actor_id=get_settings().operator_discord_user_id,
                primary_ref=operation.ref_id,
                affected_refs=[operation.ref_id, *target_refs],
                basis_refs=list(intent.basis_refs),
                data={
                    "changeset_ref": changeset.ref_id,
                    "intent_id": intent.intent_id,
                    "operation_type": intent.operation_type,
                },
            )
        )
        return [operation.ref_id]
