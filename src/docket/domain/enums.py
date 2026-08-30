from enum import StrEnum


class Environment(StrEnum):
    SMOKE = "smoke"
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    FAILED = "failed"


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
