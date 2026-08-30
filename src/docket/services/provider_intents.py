from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.enums import OperationStatus
from docket.domain.errors import DocketError, IdempotencyConflict
from docket.models import (
    AuditEvent,
    CalendarLane,
    CanonicalEvent,
    ChangeSet,
    Operation,
    OperationTarget,
    ProviderAccount,
    ProviderEventBinding,
)
from docket.models.base import utc_now
from docket.schemas.authority import ProviderIntentInput

_LANE_OPERATION_TYPES = frozenset(
    {"calendar_configure_lane", "calendar_delete_lane"}
)
_EVENT_OPERATION_TYPES = frozenset(
    {
        "calendar_create_event",
        "calendar_update_event",
        "calendar_update_reminders",
        "calendar_cancel_event",
    }
)
_SUPPORTED_OPERATION_TYPES = _LANE_OPERATION_TYPES | _EVENT_OPERATION_TYPES


class ProviderIntentService:
    """Compile provider effects directly from committed canonical targets."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _account(self, account_ref: str) -> ProviderAccount:
        account = self.session.scalar(
            select(ProviderAccount).where(ProviderAccount.ref_id == account_ref)
        )
        if (
            account is None
            or not account.enabled
            or "google_calendar" not in account.capabilities
        ):
            raise DocketError(
                code="provider_target_unresolved",
                message="Provider intent requires one enabled Calendar account.",
                details={"account_ref": account_ref},
            )
        return account

    @staticmethod
    def _target_refs(
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
        self,
        refs: list[str],
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
        self,
        event: CanonicalEvent,
        account: ProviderAccount,
        lane: CalendarLane,
    ) -> ProviderEventBinding:
        binding = self.session.scalar(
            select(ProviderEventBinding).where(
                ProviderEventBinding.canonical_target_ref == event.ref_id,
                ProviderEventBinding.target_kind == "event",
                ProviderEventBinding.account_id == account.id,
                ProviderEventBinding.calendar_id == lane.calendar_id,
                ProviderEventBinding.status == "active",
            )
        )
        if binding is None:
            raise DocketError(
                code="provider_event_binding_required",
                message="Provider event update requires an exact active binding.",
            )
        return binding

    def _parameters(
        self,
        operation_type: str,
        *,
        event: CanonicalEvent | None,
        lane: CalendarLane | None,
        account: ProviderAccount,
        hints: dict[str, Any],
    ) -> tuple[dict[str, Any], str, str]:
        if operation_type in _LANE_OPERATION_TYPES:
            if lane is None or lane.account_id != account.id:
                raise DocketError(
                    code="calendar_lane_unresolved",
                    message="Lane provider intent requires one matching CalendarLane.",
                )
            return (
                {
                    "lane_ref": lane.ref_id,
                    "lane": lane.lane,
                    "display_name": lane.display_name,
                    "color_hex": lane.color_hex,
                    "timezone": get_settings().timezone,
                    "calendar_id": lane.calendar_id,
                    "lane_version": lane.version,
                },
                lane.ref_id,
                "calendar_lane",
            )
        if event is None or lane is None or lane.account_id != account.id:
            raise DocketError(
                code="canonical_event_target_required",
                message="Calendar event provider intent requires one event and matching lane.",
            )
        event_payload = dict(event.event_spec)
        event_payload["title"] = event.title
        reminder_plan = event_payload.pop("reminder_plan", None)
        parameters: dict[str, Any] = {
            "calendar_id": lane.calendar_id,
            "lane_ref": lane.ref_id,
            "logical_key": f"canonical:{event.ref_id}",
            "event": event_payload,
            "reminder_plan": reminder_plan,
            "reminder_plan_sha256": (
                sha256_json(reminder_plan) if isinstance(reminder_plan, dict) else None
            ),
            "priority": event_payload.get("priority", "normal"),
            "priority_basis": "explicit_operator",
            "canonical_target_ref": event.ref_id,
            "target_kind": "event",
        }
        if operation_type != "calendar_create_event":
            binding = self._event_binding(event, account, lane)
            parameters.update(
                {
                    "external_event_id": binding.provider_event_id,
                    "provider_etag": binding.provider_etag,
                    "provider_before": dict(binding.provider_snapshot),
                }
            )
        if operation_type == "calendar_cancel_event" and hints.get("reason") is not None:
            parameters["reason"] = str(hints["reason"])
        return parameters, event.ref_id, "event"

    def _predecessor(
        self,
        changeset: ChangeSet,
        lane: CalendarLane | None,
    ) -> Operation | None:
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
                message="Provider operation is outside the ChangeSet profile.",
                details={"operation_type": intent.operation_type},
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
        account = self._account(intent.account_ref)
        if lane is not None and lane.account_id != account.id:
            raise DocketError(
                code="provider_target_mismatch",
                message="Provider account does not match the canonical lane account.",
            )
        parameters, primary_ref, target_kind = self._parameters(
            intent.operation_type,
            event=event,
            lane=lane,
            account=account,
            hints=intent.parameters,
        )
        predecessor = self._predecessor(changeset, lane)
        if (
            intent.operation_type in _EVENT_OPERATION_TYPES
            and lane is not None
            and lane.calendar_id is None
            and predecessor is None
        ):
            raise DocketError(
                code="calendar_lane_provider_binding_unresolved",
                message="An unprovisioned event lane requires same-ChangeSet provisioning.",
                details={"lane_ref": lane.ref_id},
            )

        operation_id = uuid.uuid4()
        operation = Operation(
            id=operation_id,
            originating_changeset_ref=changeset.ref_id,
            basis_refs=list(intent.basis_refs),
            canonical_target_refs=target_refs,
            predecessor_operation_id=(predecessor.id if predecessor is not None else None),
            idempotency_key=intent.idempotency_key,
            operation_type=intent.operation_type,
            account_id=account.id,
            status=OperationStatus.PENDING.value,
            provider_correlation=str(operation_id),
            next_attempt_at=utc_now(),
        )
        self.session.add(operation)
        self.session.flush()
        target = OperationTarget(
            operation_id=operation.id,
            target_key=primary_ref,
            canonical_target_ref=primary_ref,
            target_kind=target_kind,
            idempotency_key=f"{intent.idempotency_key}:target:{primary_ref}",
            parameters=parameters,
            parameters_sha256=sha256_json(parameters),
            status="pending",
            next_attempt_at=utc_now(),
        )
        self.session.add(target)
        if intent.operation_type == "calendar_configure_lane" and lane is not None:
            lane.status = "provisioning"
            lane.version += 1
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
                    "target_ref": primary_ref,
                },
            )
        )
        return [operation.ref_id]
