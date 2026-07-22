import uuid
from datetime import UTC, datetime, timedelta

from docket.security import (
    issue_approval_token,
    issue_projection_approval_token,
    issue_short_code,
    short_code_sha256,
    verify_approval_token,
    verify_projection_approval_token,
)


def test_compact_approval_token_is_bound_to_id_expiry_and_key() -> None:
    approval_id = uuid.uuid4()
    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    token = issue_approval_token(approval_id, expires_at, b"test-signing-key")

    assert len(token) < 100
    assert verify_approval_token(
        token,
        approval_id=approval_id,
        expires_at=expires_at,
        signing_key=b"test-signing-key",
    )
    assert not verify_approval_token(
        token,
        approval_id=uuid.uuid4(),
        expires_at=expires_at,
        signing_key=b"test-signing-key",
    )
    assert not verify_approval_token(
        token[:-1] + ("A" if token[-1] != "A" else "B"),
        approval_id=approval_id,
        expires_at=expires_at,
        signing_key=b"test-signing-key",
    )


def test_short_code_is_deterministic_and_case_insensitive_for_lookup() -> None:
    approval_id = uuid.uuid4()
    expires_at = datetime.now(UTC) + timedelta(minutes=15)

    first = issue_short_code(approval_id, expires_at, b"test-signing-key")
    second = issue_short_code(approval_id, expires_at, b"test-signing-key")

    assert first == second
    assert len(first) == 11
    assert short_code_sha256(first) == short_code_sha256(first.lower())


def test_projection_approval_token_binds_projection_and_fits_custom_id() -> None:
    approval_id = uuid.uuid4()
    projection_id = uuid.uuid4()
    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    token = issue_projection_approval_token(
        approval_id, projection_id, expires_at, b"test-signing-key"
    )

    assert len(f"dkt:a:{token}") <= 100
    assert verify_projection_approval_token(
        token,
        approval_id=approval_id,
        projection_id=projection_id,
        expires_at=expires_at,
        signing_key=b"test-signing-key",
    )
    assert not verify_projection_approval_token(
        token,
        approval_id=approval_id,
        projection_id=uuid.uuid4(),
        expires_at=expires_at,
        signing_key=b"test-signing-key",
    )
