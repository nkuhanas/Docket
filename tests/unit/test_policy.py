import pytest

from docket.domain.enums import RiskClass
from docket.domain.errors import ActionDisabled, DocketError
from docket.policy import get_action_definition


def test_server_derives_external_write_risk() -> None:
    definition = get_action_definition("gmail_archive_message", require_enabled=False)
    assert definition.risk_class is RiskClass.EXTERNAL_PRIVATE_WRITE


def test_gmail_writes_remain_disabled_until_milestone_gate() -> None:
    with pytest.raises(ActionDisabled):
        get_action_definition("gmail_archive_message")


def test_outbound_communication_is_disabled() -> None:
    with pytest.raises(ActionDisabled):
        get_action_definition("send_email")


def test_unknown_action_is_rejected() -> None:
    with pytest.raises(DocketError) as raised:
        get_action_definition("email_everyone")
    assert raised.value.code == "unknown_action_type"
