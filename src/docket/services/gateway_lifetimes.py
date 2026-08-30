from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.models import (
    ConversationalToolTrace,
    DeferredIngress,
    DrainBarrier,
    ExecutionLease,
    GatewayLifetime,
    SemanticRequest,
    ToolInvocation,
)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


_GATEWAY_REGISTRATION_LOCK_ID = 873_420_827


def _serialize_registration(session: Session) -> None:
    """Serialize lease replay, fencing, and generation allocation on PostgreSQL."""
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _GATEWAY_REGISTRATION_LOCK_ID},
        )


class GatewayLifetimeService:
    """Lease and reconcile operational Discord gateway process lifetimes."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _database_now(self) -> datetime:
        value = self.session.scalar(select(func.current_timestamp()))
        if not isinstance(value, datetime):
            raise RuntimeError("database did not return its current timestamp")
        return _aware(value)

    @staticmethod
    def _projection(lifetime: GatewayLifetime, *, disposition: str) -> dict[str, Any]:
        return {
            "ok": True,
            "ref": lifetime.ref_id,
            "state": lifetime.status,
            "summary": "Gateway lifetime lease is durable.",
            "affected_refs": [lifetime.ref_id],
            "basis_refs": [],
            "next": None,
            "warnings": [],
            "disposition": disposition,
            "lease_generation": lifetime.lease_generation,
            "lease_expires_at": lifetime.lease_expires_at.isoformat(),
        }

    def register(
        self,
        *,
        registration_key: uuid.UUID,
        instance_kind: str,
    ) -> dict[str, Any]:
        _serialize_registration(self.session)
        self.expire_and_reconcile()
        existing = self.session.scalar(
            select(GatewayLifetime).where(
                GatewayLifetime.registration_key == registration_key
            )
        )
        if existing is not None:
            if existing.status not in {"active", "draining"}:
                raise DocketError(
                    code="gateway_lifetime_fenced",
                    message="An expired or closed gateway lifetime cannot be resurrected.",
                )
            return self._projection(existing, disposition="replayed_request")
        live = self.session.scalar(
            select(GatewayLifetime).where(
                GatewayLifetime.instance_kind == instance_kind,
                GatewayLifetime.status.in_(("active", "draining")),
            ).with_for_update()
        )
        replaced_gateway_ref: str | None = None
        if live is not None:
            barrier = self.session.scalar(
                select(DrainBarrier)
                .where(DrainBarrier.status.in_(("requested", "draining")))
                .order_by(DrainBarrier.requested_at.desc())
                .with_for_update()
            )
            drained = False
            if barrier is not None and instance_kind == "hermes_discord_gateway":
                # Imported locally to keep the two operational services from
                # acquiring a module-level dependency cycle.
                from docket.services.continuity import ContinuityService

                drained = bool(
                    ContinuityService(self.session).drain_status(barrier.ref_id)["drained"]
                )
            if not drained:
                raise DocketError(
                    code="gateway_lifetime_already_active",
                    message="A live gateway lifetime already owns this instance kind.",
                    details={"gateway_instance_ref": live.ref_id},
                )
            # A drained deployment has proven that the prior process owns no
            # in-flight semantic execution. Its container may not run Python
            # atexit handlers under s6/Docker replacement, so fence that
            # lifetime atomically with registration of the replacement.
            now = self._database_now()
            live.clean_shutdown_at = now
            live.heartbeat_at = now
            live.lease_expires_at = now
            live.status = "clean_shutdown"
            replaced_gateway_ref = live.ref_id
        generation = int(
            self.session.scalar(
                select(func.max(GatewayLifetime.lease_generation)).where(
                    GatewayLifetime.instance_kind == instance_kind
                )
            )
            or 0
        ) + 1
        now = self._database_now()
        lifetime = GatewayLifetime(
            registration_key=registration_key,
            instance_kind=instance_kind,
            lease_generation=generation,
            started_at=now,
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=get_settings().gateway_lease_seconds),
            status="active",
        )
        self.session.add(lifetime)
        self.session.flush()
        result = self._projection(lifetime, disposition="created")
        if replaced_gateway_ref is not None:
            result["replaced_gateway_ref"] = replaced_gateway_ref
        return result

    def require_live(self, gateway_instance_ref: str) -> GatewayLifetime:
        self.expire_and_reconcile()
        lifetime = self.session.scalar(
            select(GatewayLifetime).where(
                GatewayLifetime.ref_id == gateway_instance_ref
            )
        )
        if lifetime is None:
            raise DocketError(
                code="gateway_lifetime_not_found",
                message="Gateway lifetime reference does not exist.",
            )
        if lifetime.status not in {"active", "draining"}:
            raise DocketError(
                code="gateway_lifetime_fenced",
                message="Gateway lifetime is no longer allowed to update turns.",
            )
        return lifetime

    def current_live(self, instance_kind: str) -> GatewayLifetime | None:
        self.expire_and_reconcile()
        return self.session.scalar(
            select(GatewayLifetime).where(
                GatewayLifetime.instance_kind == instance_kind,
                GatewayLifetime.status.in_(("active", "draining")),
            )
        )

    def heartbeat(self, gateway_instance_ref: str, *, status: str) -> dict[str, Any]:
        lifetime = self.require_live(gateway_instance_ref)
        now = self._database_now()
        lifetime.heartbeat_at = now
        lifetime.lease_expires_at = now + timedelta(
            seconds=get_settings().gateway_lease_seconds
        )
        lifetime.status = status
        return self._projection(lifetime, disposition="updated")

    def clean_shutdown(self, gateway_instance_ref: str) -> dict[str, Any]:
        lifetime = self.require_live(gateway_instance_ref)
        now = self._database_now()
        lifetime.clean_shutdown_at = now
        lifetime.heartbeat_at = now
        lifetime.lease_expires_at = now
        lifetime.status = "clean_shutdown"
        return self._projection(lifetime, disposition="updated")

    def _reconcile_trace(self, trace: ConversationalToolTrace, now: datetime) -> None:
        calls = [dict(item) for item in trace.calls]
        for call in calls:
            invocation = self.session.scalar(
                select(ToolInvocation).where(
                    ToolInvocation.trace_ref == trace.ref_id,
                    ToolInvocation.trace_call_id == str(call.get("call_id", "")),
                )
            )
            if invocation is not None and invocation.transport_state == "running":
                semantic_request = (
                    self.session.scalar(
                        select(SemanticRequest).where(
                            SemanticRequest.ref_id == invocation.semantic_request_ref
                        )
                    )
                    if invocation.semantic_request_ref is not None
                    else None
                )
                if (
                    semantic_request is not None
                    and semantic_request.commit_state == "committed"
                    and semantic_request.committed_changeset_ref is not None
                ):
                    invocation.transport_state = "completed"
                    invocation.domain_state = "succeeded"
                    invocation.result_disposition = "committed"
                    invocation.result_refs = [
                        semantic_request.ref_id,
                        semantic_request.committed_changeset_ref,
                    ]
                    invocation.completed_at = now
                else:
                    invocation.transport_state = "timed_out"
                    invocation.domain_state = "unknown"
                    invocation.result_disposition = "unknown"
                    invocation.error_code = "gateway_interrupted"
                    invocation.completed_at = now
            if call.get("transport_state", call.get("state")) == "running":
                call["transport_state"] = "timed_out"
                call["transport_error_code"] = "gateway_interrupted"
                call.pop("state", None)
            call["domain_state"] = (
                invocation.domain_state if invocation is not None else "unknown"
            )
            call["disposition"] = (
                invocation.result_disposition if invocation is not None else "unknown"
            )
            call["domain_error_code"] = (
                invocation.error_code if invocation is not None else "gateway_interrupted"
            )
            call["tool_call_ref"] = invocation.ref_id if invocation is not None else None
        trace.calls = calls
        trace.status = "interrupted"
        trace.completed_at = now
        trace.version += 1

    def expire_and_reconcile(self) -> list[str]:
        now = self._database_now()
        expired = list(
            self.session.scalars(
                select(GatewayLifetime)
                .where(
                    GatewayLifetime.clean_shutdown_at.is_(None),
                    GatewayLifetime.status.in_(("active", "draining")),
                    GatewayLifetime.lease_expires_at < func.current_timestamp(),
                )
                .with_for_update()
            )
        )
        expired_refs: list[str] = []
        for lifetime in expired:
            lifetime.status = "expired"
            expired_refs.append(lifetime.ref_id)
            leases = list(
                self.session.scalars(
                    select(ExecutionLease)
                    .where(
                        ExecutionLease.gateway_instance_ref == lifetime.ref_id,
                        ExecutionLease.status == "active",
                    )
                    .with_for_update()
                )
            )
            for lease in leases:
                lease.status = "expired"
                lease.completed_at = now
                lease.metadata_json = {
                    **lease.metadata_json,
                    "error_code": "gateway_interrupted",
                }
            deferred = list(
                self.session.scalars(
                    select(DeferredIngress)
                    .where(
                        DeferredIngress.claimed_by_gateway_ref == lifetime.ref_id,
                        DeferredIngress.status == "claimed",
                    )
                    .with_for_update()
                )
            )
            for ingress in deferred:
                ingress.status = "pending"
                ingress.claimed_by_gateway_ref = None
                ingress.claim_token = None
                ingress.claimed_at = None
                ingress.last_error_code = "gateway_interrupted"
            traces = list(
                self.session.scalars(
                    select(ConversationalToolTrace)
                    .where(
                        ConversationalToolTrace.gateway_instance_ref == lifetime.ref_id,
                        ConversationalToolTrace.status == "running",
                    )
                    .with_for_update()
                )
            )
            for trace in traces:
                self._reconcile_trace(trace, now)
        return expired_refs


class GatewayLifetimeReconciler:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def run_once(self) -> list[str]:
        with self.session_factory.begin() as session:
            return GatewayLifetimeService(session).expire_and_reconcile()
