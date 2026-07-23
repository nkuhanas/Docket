from enum import StrEnum


class Environment(StrEnum):
    SMOKE = "smoke"
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class RecordStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class CommandStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    FAILED = "failed"


class RiskClass(StrEnum):
    READ_ONLY = "read_only"
    LOCAL_WRITE = "local_write"
    EXTERNAL_PRIVATE_WRITE = "external_private_write"
    EXTERNAL_COMMUNICATION = "external_communication"
    DESTRUCTIVE = "destructive"
    BULK = "bulk"


class ActionAvailability(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"


class QueueItemStatus(StrEnum):
    PENDING = "pending"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    SNOOZED = "snoozed"
    IGNORED = "ignored"


class ActionStatus(StrEnum):
    AVAILABLE = "available"
    APPROVAL_PENDING = "approval_pending"
    READY = "ready"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    PARTIAL_FAILED = "partial_failed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    CONSUMED = "consumed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class OperationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL_FAILED = "partial_failed"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class AttemptKind(StrEnum):
    EXECUTE = "execute"
    RECONCILE = "reconcile"


class AttemptStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
