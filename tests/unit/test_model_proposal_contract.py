import uuid
from datetime import UTC, datetime

from docket.mcp.server import _model_proposal_result
from docket.schemas.actions import ProposalResult


def test_model_proposal_result_is_button_first_and_omits_short_code() -> None:
    result = ProposalResult(
        request_id=uuid.uuid4(),
        disposition="proposed",
        queue_item_id=uuid.uuid4(),
        action_id=uuid.uuid4(),
        action_revision_id=uuid.uuid4(),
        approval_id=uuid.uuid4(),
        short_code="RECOVERY-CODE",
        expires_at=datetime(2026, 7, 22, 9, tzinfo=UTC),
        preview={"action_type": "calendar_create_meeting"},
    )

    payload = _model_proposal_result(result)

    assert "short_code" not in payload
    assert payload["approval_surface"] == {
        "kind": "discord_button_card",
        "location": "today's ISO-dated thread under the configured Docket queue",
        "delivery_status": "pending",
        "operator_instruction": (
            "Use the Approve or Reject button on the projected Docket card."
        ),
        "typed_code_policy": "break_glass_only_not_for_agent_guidance",
    }
