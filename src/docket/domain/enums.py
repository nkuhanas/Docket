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
