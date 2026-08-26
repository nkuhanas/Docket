import uuid

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


def test_gmail_triage_source_allowlist_parses_exact_uuid_scope() -> None:
    source_id = uuid.uuid4()

    settings = Settings(
        DOCKET_GMAIL_TRIAGE_SOURCE_ALLOWLIST=[str(source_id)],
    )

    assert settings.gmail_triage_source_allowlist == [source_id]


def test_isolated_triage_defaults_fit_one_live_runner_pass() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.gmail_claim_batch_size == 5
    assert settings.gmail_triage_lease_seconds == 300


def test_waking_window_is_one_ordered_local_day_interval() -> None:
    with pytest.raises(ValidationError, match="must start before it ends"):
        Settings(
            DOCKET_WAKING_WINDOW_START_HOUR=22,
            DOCKET_WAKING_WINDOW_END_HOUR=7,
        )

    settings = Settings(
        DOCKET_WAKING_WINDOW_START_HOUR=6,
        DOCKET_WAKING_WINDOW_END_HOUR=23,
    )
    assert settings.waking_window_start_hour == 6
    assert settings.waking_window_end_hour == 23
