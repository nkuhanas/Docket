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


def test_encrypted_backup_requires_age_recipient() -> None:
    with pytest.raises(ValidationError, match="DOCKET_BACKUP_AGE_RECIPIENT"):
        Settings(DOCKET_BACKUP_ENABLED=True)

    settings = Settings(
        DOCKET_BACKUP_ENABLED=True,
        DOCKET_BACKUP_AGE_RECIPIENT="age1configured",
    )
    assert settings.backup_enabled


def test_gmail_writes_require_both_ingestion_and_global_write_gate() -> None:
    with pytest.raises(ValidationError, match="Gmail writes require"):
        Settings(
            DOCKET_GMAIL_WRITES_ENABLED=True,
            DOCKET_GMAIL_INGESTION_ENABLED=False,
            DOCKET_EXTERNAL_WRITES_ENABLED=True,
        )
    with pytest.raises(ValidationError, match="Gmail writes require"):
        Settings(
            DOCKET_GMAIL_WRITES_ENABLED=True,
            DOCKET_GMAIL_INGESTION_ENABLED=True,
            DOCKET_EXTERNAL_WRITES_ENABLED=False,
        )

    settings = Settings(
        DOCKET_GMAIL_WRITES_ENABLED=True,
        DOCKET_GMAIL_INGESTION_ENABLED=True,
        DOCKET_EXTERNAL_WRITES_ENABLED=True,
    )
    assert settings.gmail_writes_enabled
