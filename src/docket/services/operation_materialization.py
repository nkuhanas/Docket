from __future__ import annotations

from docket.domain.errors import DocketError
from docket.models import ActionRevision


def operation_idempotency_key(revision: ActionRevision) -> str:
    """Derive the provider-operation identity from one immutable formulation."""
    parameters = revision.parameters
    if revision.action_type == "calendar_create_event":
        return (
            f"calendar:create-event:{revision.account_id}:"
            f"{parameters['logical_key']}:{revision.parameters_sha256}"
        )
    if revision.action_type == "calendar_update_event":
        return (
            f"calendar:update-event:{revision.account_id}:"
            f"{parameters['external_event_id']}:{parameters.get('provider_etag')}:"
            f"{revision.preview_sha256}"
        )
    if revision.action_type == "calendar_update_reminders":
        return (
            f"calendar:update-reminders:{revision.account_id}:"
            f"{parameters['external_event_id']}:{parameters.get('provider_etag')}:"
            f"{parameters['reminder_plan_sha256']}"
        )
    if revision.action_type == "calendar_cancel_event":
        return (
            f"calendar:cancel-event:{revision.account_id}:"
            f"{parameters['external_event_id']}:{parameters.get('provider_etag')}"
        )
    if revision.action_type == "calendar_configure_lane":
        return (
            f"calendar:configure-lane:{revision.account_id}:"
            f"{parameters['lane']}:{revision.parameters_sha256}"
        )
    if revision.action_type in {
        "calendar_reconcile_course",
        "calendar_drop_course",
    }:
        return (
            f"calendar:{parameters['mode']}-course:{revision.account_id}:"
            f"{parameters['record_id']}:{parameters['record_version']}:"
            f"{revision.parameters_sha256}"
        )
    if revision.action_type == "gmail_archive_message":
        return (
            f"gmail:archive:{revision.account_id}:"
            f"{parameters['message_id']}:{parameters['source_version']}"
        )
    if revision.action_type == "gmail_mark_read":
        return (
            f"gmail:mark_read:{revision.account_id}:"
            f"{parameters['message_id']}:{parameters['source_version']}"
        )
    raise DocketError(
        code="invalid_action_state",
        message="The action has no external operation handler.",
    )
