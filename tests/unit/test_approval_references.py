import uuid
from datetime import UTC, datetime, timedelta

from docket.security import (
    ReviewNavigationReference,
    decode_projection_decision_approval_token,
    decode_projection_local_action_token,
    decode_projection_proposal_control_token,
    decode_projection_review_navigation_token,
    issue_approval_token,
    issue_projection_approval_token,
    issue_projection_decision_approval_token,
    issue_projection_local_action_token,
    issue_projection_proposal_control_token,
    issue_projection_review_navigation_token,
    issue_short_code,
    short_code_sha256,
    verify_approval_token,
    verify_projection_approval_token,
    verify_projection_decision_approval_token,
    verify_projection_local_action_token,
    verify_projection_proposal_control_token,
    verify_projection_review_navigation_token,
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


def test_local_action_token_binds_revision_projection_version_and_expiry() -> None:
    revision_id = uuid.uuid4()
    projection_id = uuid.uuid4()
    expires_at = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)
    token = issue_projection_local_action_token(
        revision_id,
        projection_id,
        7,
        expires_at,
        b"test-signing-key",
    )

    assert len(f"dkt:l:{token}") <= 100
    assert decode_projection_local_action_token(token) == (
        revision_id,
        projection_id,
        7,
        expires_at,
    )
    assert verify_projection_local_action_token(
        token,
        action_revision_id=revision_id,
        projection_id=projection_id,
        queue_version=7,
        expires_at=expires_at,
        signing_key=b"test-signing-key",
    )
    assert not verify_projection_local_action_token(
        token,
        action_revision_id=revision_id,
        projection_id=projection_id,
        queue_version=8,
        expires_at=expires_at,
        signing_key=b"test-signing-key",
    )


def test_proposal_control_token_binds_field_and_fits_custom_id() -> None:
    revision_id = uuid.uuid4()
    projection_id = uuid.uuid4()
    expires_at = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=15)
    token = issue_projection_proposal_control_token(
        revision_id,
        projection_id,
        "priority",
        expires_at,
        b"test-signing-key",
    )

    assert len(f"dkt:p:{token}") <= 100
    assert decode_projection_proposal_control_token(token) == (
        revision_id,
        projection_id,
        "priority",
        expires_at,
    )
    assert verify_projection_proposal_control_token(
        token,
        action_revision_id=revision_id,
        projection_id=projection_id,
        field="priority",
        expires_at=expires_at,
        signing_key=b"test-signing-key",
    )
    assert not verify_projection_proposal_control_token(
        token,
        action_revision_id=revision_id,
        projection_id=projection_id,
        field="reminder_preset",
        expires_at=expires_at,
        signing_key=b"test-signing-key",
    )


def test_decision_approval_token_binds_projection_version_and_fits_custom_id() -> None:
    approval_id = uuid.uuid4()
    projection_id = uuid.uuid4()
    expires_at = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=15)
    token = issue_projection_decision_approval_token(
        approval_id,
        projection_id,
        9,
        expires_at,
        b"test-signing-key",
    )

    assert len(f"dkt:a:{token}") <= 100
    assert decode_projection_decision_approval_token(token) == (
        approval_id,
        projection_id,
        9,
        expires_at,
    )
    assert verify_projection_decision_approval_token(
        token,
        approval_id=approval_id,
        projection_id=projection_id,
        projection_version=9,
        expires_at=expires_at,
        signing_key=b"test-signing-key",
    )
    assert not verify_projection_decision_approval_token(
        token,
        approval_id=approval_id,
        projection_id=projection_id,
        projection_version=10,
        expires_at=expires_at,
        signing_key=b"test-signing-key",
    )


def test_review_navigation_token_binds_every_transition_dimension() -> None:
    reference = ReviewNavigationReference(
        action_revision_id=uuid.uuid4(),
        projection_id=uuid.uuid4(),
        projection_version=12,
        source_view="schedule_review",
        source_page=2,
        target_view="schedule_review",
        target_page=3,
        actor_id="325761533034496010",
        expires_at=datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=15),
    )
    token = issue_projection_review_navigation_token(
        action_revision_id=reference.action_revision_id,
        projection_id=reference.projection_id,
        projection_version=reference.projection_version,
        source_view=reference.source_view,
        source_page=reference.source_page,
        target_view=reference.target_view,
        target_page=reference.target_page,
        actor_id=reference.actor_id,
        expires_at=reference.expires_at,
        signing_key=b"test-signing-key",
    )

    assert len(f"dkt:n:{token}") <= 100
    assert decode_projection_review_navigation_token(token) == reference
    assert verify_projection_review_navigation_token(
        token,
        reference=reference,
        signing_key=b"test-signing-key",
    )
    assert not verify_projection_review_navigation_token(
        token,
        reference=ReviewNavigationReference(
            **{
                **reference.__dict__,
                "target_page": 4,
            }
        ),
        signing_key=b"test-signing-key",
    )
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert not verify_projection_review_navigation_token(
        tampered,
        reference=reference,
        signing_key=b"test-signing-key",
    )
