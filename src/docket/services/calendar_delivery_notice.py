"""Bounded presentation of one draft/commit's provider effects, not live delivery."""

from typing import Literal


def calendar_delivery_notice(
    operation_count: int,
    *,
    phase: Literal["draft", "committed"],
    has_errors: bool = False,
) -> str:
    if phase == "draft":
        if has_errors:
            return "Draft has errors; nothing has committed or been queued for Google Calendar."
        if operation_count:
            return (
                "This draft plans Google Calendar delivery after commit; "
                "nothing has been queued yet."
            )
        return "This draft saves in Docket only; it plans no Google Calendar delivery."
    if operation_count:
        return (
            "Committed in Docket; Google Calendar delivery was queued at commit, "
            "not confirmed. Follow this ChangeSet's delivery status if confirmation is needed."
        )
    return "Committed in Docket only; this ChangeSet queued no Google Calendar delivery."
