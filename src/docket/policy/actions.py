from dataclasses import dataclass

from docket.domain.enums import ActionAvailability, RiskClass
from docket.domain.errors import ActionDisabled, DocketError


@dataclass(frozen=True, slots=True)
class ActionDefinition:
    action_type: str
    risk_class: RiskClass
    executor: str | None
    availability: ActionAvailability


ACTION_REGISTRY: dict[str, ActionDefinition] = {
    item.action_type: item
    for item in (
        ActionDefinition(
            "calendar_create_meeting",
            RiskClass.EXTERNAL_PRIVATE_WRITE,
            "google_calendar",
            ActionAvailability.ENABLED,
        ),
        ActionDefinition(
            "calendar_update_meeting",
            RiskClass.EXTERNAL_PRIVATE_WRITE,
            "google_calendar",
            ActionAvailability.ENABLED,
        ),
        ActionDefinition(
            "gmail_archive_message",
            RiskClass.EXTERNAL_PRIVATE_WRITE,
            "gmail",
            ActionAvailability.ENABLED,
        ),
        ActionDefinition(
            "gmail_mark_read",
            RiskClass.EXTERNAL_PRIVATE_WRITE,
            "gmail",
            ActionAvailability.ENABLED,
        ),
        ActionDefinition(
            "update_application_record",
            RiskClass.LOCAL_WRITE,
            "docket",
            ActionAvailability.ENABLED,
        ),
        ActionDefinition(
            "snooze_queue_item",
            RiskClass.LOCAL_WRITE,
            "docket",
            ActionAvailability.ENABLED,
        ),
        ActionDefinition(
            "ignore_queue_item",
            RiskClass.LOCAL_WRITE,
            "docket",
            ActionAvailability.ENABLED,
        ),
        ActionDefinition(
            "send_email",
            RiskClass.EXTERNAL_COMMUNICATION,
            None,
            ActionAvailability.DISABLED,
        ),
        ActionDefinition(
            "calendar_delete_event",
            RiskClass.DESTRUCTIVE,
            None,
            ActionAvailability.DISABLED,
        ),
    )
}


def get_action_definition(action_type: str, *, require_enabled: bool = True) -> ActionDefinition:
    try:
        definition = ACTION_REGISTRY[action_type]
    except KeyError as exc:
        raise DocketError(
            code="unknown_action_type",
            message="The action type is not registered.",
            details={"action_type": action_type},
        ) from exc
    if require_enabled and definition.availability is ActionAvailability.DISABLED:
        raise ActionDisabled(action_type)
    return definition
