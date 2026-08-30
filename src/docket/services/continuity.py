from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from docket.domain.errors import DocketError
from docket.models import DeferredIngress, DrainBarrier, ExecutionLease
from docket.models.base import utc_now

_DRAIN_LOCK_ID = 873_420_826
_ACTIVE_BARRIER_STATES = ("requested", "draining")


def _database_now(session: Session) -> datetime:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        value = session.scalar(select(func.clock_timestamp()))
        if isinstance(value, datetime):
            return value
        raise RuntimeError("PostgreSQL did not return database time")
    return utc_now()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _serialize_claim_boundary(session: Session) -> None:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": _DRAIN_LOCK_ID})


class ContinuityService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def active_barrier(self, *, lock: bool = False) -> DrainBarrier | None:
        query = (
            select(DrainBarrier)
            .where(DrainBarrier.status.in_(_ACTIVE_BARRIER_STATES))
            .order_by(DrainBarrier.requested_at.desc())
            .limit(1)
        )
        if lock:
            query = query.with_for_update()
        return self.session.scalar(query)

    def request_drain(self, *, requested_by: str, timeout_seconds: int) -> dict[str, Any]:
        _serialize_claim_boundary(self.session)
        now = _database_now(self.session)
        existing = self.active_barrier(lock=True)
        if existing is not None:
            return self._barrier_result(existing, disposition="replayed_request")
        barrier = DrainBarrier(
            requested_at=now,
            cutoff_at=now,
            timeout_at=now + timedelta(seconds=timeout_seconds),
            status="draining",
            requested_by=requested_by[:128],
        )
        self.session.add(barrier)
        self.session.flush()
        return self._barrier_result(barrier, disposition="created")

    def acquire_execution_lease(
        self,
        *,
        lease_key: str,
        lease_kind: str,
        subject_ref: str | None = None,
        gateway_instance_ref: str | None = None,
        lease_seconds: int = 1800,
    ) -> ExecutionLease:
        _serialize_claim_boundary(self.session)
        now = _database_now(self.session)
        barrier = self.active_barrier(lock=True)
        if barrier is not None:
            raise DocketError(
                code="deployment_drain_active",
                message="Execution is deferred while the deployment drain barrier is active.",
                details={"drain_ref": barrier.ref_id},
            )
        existing = self.session.scalar(
            select(ExecutionLease).where(ExecutionLease.lease_key == lease_key)
        )
        if existing is not None:
            if existing.status == "active" and _aware(existing.lease_expires_at) > now:
                return existing
            raise DocketError(
                code="execution_lease_terminal",
                message="This execution lineage already has a terminal lease.",
                details={"status": existing.status},
            )
        lease = ExecutionLease(
            lease_key=lease_key,
            lease_kind=lease_kind,
            subject_ref=subject_ref,
            gateway_instance_ref=gateway_instance_ref,
            status="active",
            claimed_at=now,
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
        )
        self.session.add(lease)
        self.session.flush()
        return lease

    def complete_execution_lease(
        self,
        completion_token: str,
        *,
        retain: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        lease = self.session.scalar(
            select(ExecutionLease)
            .where(ExecutionLease.completion_token == completion_token)
            .with_for_update()
        )
        if lease is None:
            raise DocketError(
                code="execution_lease_not_found",
                message="Execution lease does not exist.",
            )
        if lease.status != "active":
            return
        if not retain:
            self.session.delete(lease)
            return
        lease.status = "completed"
        lease.completed_at = _database_now(self.session)
        if metadata:
            lease.metadata_json = {**lease.metadata_json, **metadata}

    def heartbeat_execution_lease(
        self, completion_token: str, *, lease_seconds: int = 1800
    ) -> None:
        lease = self.session.scalar(
            select(ExecutionLease)
            .where(ExecutionLease.completion_token == completion_token)
            .with_for_update()
        )
        if lease is None or lease.status != "active":
            raise DocketError(
                code="execution_lease_not_active",
                message="Execution lease is not active.",
            )
        now = _database_now(self.session)
        lease.heartbeat_at = now
        lease.lease_expires_at = now + timedelta(seconds=lease_seconds)

    def drain_status(self, drain_ref: str) -> dict[str, Any]:
        barrier = self.session.scalar(select(DrainBarrier).where(DrainBarrier.ref_id == drain_ref))
        if barrier is None:
            raise DocketError(code="drain_not_found", message="Drain barrier does not exist.")
        now = _database_now(self.session)
        expired = list(
            self.session.scalars(
                select(ExecutionLease)
                .where(
                    ExecutionLease.status == "active",
                    ExecutionLease.lease_expires_at <= now,
                )
                .with_for_update(skip_locked=True)
            )
        )
        for lease in expired:
            lease.status = "expired"
            lease.completed_at = now
        active = list(
            self.session.scalars(
                select(ExecutionLease)
                .where(
                    ExecutionLease.status == "active",
                    ExecutionLease.claimed_at <= barrier.cutoff_at,
                )
                .order_by(ExecutionLease.claimed_at, ExecutionLease.id)
            )
        )
        return {
            **self._barrier_result(barrier, disposition="inspected"),
            "drained": not active,
            "active_lease_kinds": [lease.lease_kind for lease in active],
            "timed_out": now >= _aware(barrier.timeout_at) and bool(active),
        }

    def release_drain(self, drain_ref: str, *, aborted: bool = False) -> dict[str, Any]:
        _serialize_claim_boundary(self.session)
        barrier = self.session.scalar(
            select(DrainBarrier).where(DrainBarrier.ref_id == drain_ref).with_for_update()
        )
        if barrier is None:
            raise DocketError(code="drain_not_found", message="Drain barrier does not exist.")
        if barrier.status in {"released", "aborted"}:
            return self._barrier_result(barrier, disposition="replayed_request")
        if not aborted:
            status = self.drain_status(drain_ref)
            if not status["drained"]:
                raise DocketError(
                    code="drain_not_complete",
                    message="Pre-barrier execution leases are still active.",
                    details={"active_lease_count": len(status["active_lease_kinds"])},
                )
        barrier.status = "aborted" if aborted else "released"
        barrier.released_at = _database_now(self.session)
        barrier.last_error_code = "drain_timeout" if aborted else None
        return self._barrier_result(
            barrier,
            disposition="aborted" if aborted else "released",
        )

    def recover_deferred_claims(self) -> int:
        now = _database_now(self.session)
        expired_gateway_refs = select(ExecutionLease.gateway_instance_ref).where(
            ExecutionLease.status.in_(("expired", "cancelled")),
            ExecutionLease.gateway_instance_ref.is_not(None),
        )
        rows = list(
            self.session.scalars(
                select(DeferredIngress)
                .where(
                    DeferredIngress.status == "claimed",
                    DeferredIngress.claimed_by_gateway_ref.in_(expired_gateway_refs),
                )
                .with_for_update(skip_locked=True)
            )
        )
        for row in rows:
            row.status = "pending"
            row.claimed_by_gateway_ref = None
            row.claim_token = None
            row.claimed_at = None
            row.last_error_code = "gateway_execution_interrupted"
        # Remove completed no-op execution leases after their forensic usefulness
        # window; active/failed work is never deleted here.
        self.session.execute(
            delete(ExecutionLease).where(
                ExecutionLease.status == "completed",
                ExecutionLease.subject_ref.is_(None),
                ExecutionLease.completed_at < now - timedelta(days=1),
            )
        )
        return len(rows)

    @staticmethod
    def _barrier_result(barrier: DrainBarrier, *, disposition: str) -> dict[str, Any]:
        return {
            "ok": True,
            "ref": barrier.ref_id,
            "state": barrier.status,
            "cutoff_at": barrier.cutoff_at.isoformat(),
            "timeout_at": barrier.timeout_at.isoformat(),
            "disposition": disposition,
        }


class ExecutionLeaseCoordinator:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def acquire(
        self,
        *,
        lease_key: str,
        lease_kind: str,
        subject_ref: str | None = None,
        gateway_instance_ref: str | None = None,
        lease_seconds: int = 1800,
    ) -> str | None:
        with self.session_factory.begin() as session:
            try:
                lease = ContinuityService(session).acquire_execution_lease(
                    lease_key=lease_key,
                    lease_kind=lease_kind,
                    subject_ref=subject_ref,
                    gateway_instance_ref=gateway_instance_ref,
                    lease_seconds=lease_seconds,
                )
            except DocketError as exc:
                if exc.code == "deployment_drain_active":
                    return None
                raise
            return lease.completion_token

    def complete(
        self,
        completion_token: str,
        *,
        retain: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self.session_factory.begin() as session:
            ContinuityService(session).complete_execution_lease(
                completion_token,
                retain=retain,
                metadata=metadata,
            )
