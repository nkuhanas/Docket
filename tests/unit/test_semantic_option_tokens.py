from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from docket.security import (
    SemanticOptionReference,
    decode_semantic_option_token,
    issue_semantic_option_token,
    verify_semantic_option_token,
)


def test_semantic_option_token_binds_option_actor_and_expiry() -> None:
    reference = SemanticOptionReference(
        option_row_id=uuid.uuid4(),
        actor_id="325761533034496010",
        expires_at=datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=15),
    )
    token = issue_semantic_option_token(
        option_row_id=reference.option_row_id,
        actor_id=reference.actor_id,
        expires_at=reference.expires_at,
        signing_key=b"test-signing-key",
    )

    assert len(f"dkt:s:{token}") <= 100
    assert decode_semantic_option_token(token) == reference
    assert verify_semantic_option_token(
        token,
        reference=reference,
        signing_key=b"test-signing-key",
    )
    assert not verify_semantic_option_token(
        token,
        reference=SemanticOptionReference(
            option_row_id=uuid.uuid4(),
            actor_id=reference.actor_id,
            expires_at=reference.expires_at,
        ),
        signing_key=b"test-signing-key",
    )
    tampered = token[:-4] + ("A" if token[-4] != "A" else "B") + token[-3:]
    assert not verify_semantic_option_token(
        tampered,
        reference=reference,
        signing_key=b"test-signing-key",
    )
