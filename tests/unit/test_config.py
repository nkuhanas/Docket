import pytest
from pydantic import ValidationError

from docket.config import Settings


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("DOCKET_CHAT_CHANNEL_ID", "DOCKET_QUEUE_CHANNEL_ID"),
        ("DOCKET_CHAT_CHANNEL_ID", "DOCKET_SYSTEM_CHANNEL_ID"),
        ("DOCKET_QUEUE_CHANNEL_ID", "DOCKET_SYSTEM_CHANNEL_ID"),
    ],
)
def test_channel_lanes_must_be_pairwise_distinct(left: str, right: str) -> None:
    values = {
        "DOCKET_CHAT_CHANNEL_ID": "111111111111111111",
        "DOCKET_QUEUE_CHANNEL_ID": "222222222222222222",
        "DOCKET_SYSTEM_CHANNEL_ID": "333333333333333333",
    }
    values[right] = values[left]

    with pytest.raises(ValidationError, match="must be distinct"):
        Settings(**values)  # type: ignore[arg-type]
