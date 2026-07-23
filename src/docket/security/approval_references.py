import base64
import hashlib
import hmac
from datetime import UTC, datetime
from uuid import UUID

_TOKEN_VERSION = 1
_PROJECTION_TOKEN_VERSION = 2
_LOCAL_ACTION_TOKEN_VERSION = 3
_PROPOSAL_CONTROL_TOKEN_VERSION = 4
_MAC_BYTES = 16
_SHORT_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_PROPOSAL_FIELDS = {
    "priority": 1,
    "reminder_preset": 2,
    "refresh": 3,
    "edit": 4,
    "review_page": 5,
    "snooze": 6,
}
_PROPOSAL_FIELD_NAMES = {value: key for key, value in _PROPOSAL_FIELDS.items()}


def _timestamp(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp())


def _token_payload(approval_id: UUID, expires_at: datetime) -> bytes:
    return bytes([_TOKEN_VERSION]) + approval_id.bytes + _timestamp(expires_at).to_bytes(8, "big")


def issue_approval_token(approval_id: UUID, expires_at: datetime, signing_key: bytes) -> str:
    payload = _token_payload(approval_id, expires_at)
    signature = hmac.digest(signing_key, b"docket-approval-token-v1\x00" + payload, "sha256")
    return base64.urlsafe_b64encode(payload + signature[:_MAC_BYTES]).rstrip(b"=").decode()


def _projection_token_payload(
    approval_id: UUID, projection_id: UUID, expires_at: datetime
) -> bytes:
    return (
        bytes([_PROJECTION_TOKEN_VERSION])
        + approval_id.bytes
        + projection_id.bytes
        + _timestamp(expires_at).to_bytes(8, "big")
    )


def issue_projection_approval_token(
    approval_id: UUID,
    projection_id: UUID,
    expires_at: datetime,
    signing_key: bytes,
) -> str:
    payload = _projection_token_payload(approval_id, projection_id, expires_at)
    signature = hmac.digest(
        signing_key, b"docket-projection-approval-token-v2\x00" + payload, "sha256"
    )
    return base64.urlsafe_b64encode(payload + signature[:_MAC_BYTES]).rstrip(b"=").decode()


def verify_projection_approval_token(
    token: str,
    *,
    approval_id: UUID,
    projection_id: UUID,
    expires_at: datetime,
    signing_key: bytes,
) -> bool:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, UnicodeEncodeError):
        return False
    canonical_token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    if not hmac.compare_digest(token, canonical_token):
        return False
    expected_payload = _projection_token_payload(approval_id, projection_id, expires_at)
    if len(raw) != len(expected_payload) + _MAC_BYTES:
        return False
    payload, supplied_mac = raw[:-_MAC_BYTES], raw[-_MAC_BYTES:]
    expected_mac = hmac.digest(
        signing_key,
        b"docket-projection-approval-token-v2\x00" + expected_payload,
        "sha256",
    )[:_MAC_BYTES]
    return hmac.compare_digest(payload, expected_payload) and hmac.compare_digest(
        supplied_mac, expected_mac
    )


def _local_action_token_payload(
    action_revision_id: UUID,
    projection_id: UUID,
    queue_version: int,
    expires_at: datetime,
) -> bytes:
    if queue_version < 1 or queue_version >= 2**32:
        raise ValueError("queue_version is outside the signed token range")
    return (
        bytes([_LOCAL_ACTION_TOKEN_VERSION])
        + action_revision_id.bytes
        + projection_id.bytes
        + queue_version.to_bytes(4, "big")
        + _timestamp(expires_at).to_bytes(8, "big")
    )


def issue_projection_local_action_token(
    action_revision_id: UUID,
    projection_id: UUID,
    queue_version: int,
    expires_at: datetime,
    signing_key: bytes,
) -> str:
    payload = _local_action_token_payload(
        action_revision_id, projection_id, queue_version, expires_at
    )
    signature = hmac.digest(
        signing_key, b"docket-projection-local-action-token-v1\x00" + payload, "sha256"
    )
    return base64.urlsafe_b64encode(payload + signature[:_MAC_BYTES]).rstrip(b"=").decode()


def decode_projection_local_action_token(
    token: str,
) -> tuple[UUID, UUID, int, datetime] | None:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, UnicodeEncodeError):
        return None
    canonical_token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    payload_length = 1 + 16 + 16 + 4 + 8
    if (
        not hmac.compare_digest(token, canonical_token)
        or len(raw) != payload_length + _MAC_BYTES
        or raw[0] != _LOCAL_ACTION_TOKEN_VERSION
    ):
        return None
    payload = raw[:payload_length]
    return (
        UUID(bytes=payload[1:17]),
        UUID(bytes=payload[17:33]),
        int.from_bytes(payload[33:37], "big"),
        datetime.fromtimestamp(int.from_bytes(payload[37:45], "big"), tz=UTC),
    )


def verify_projection_local_action_token(
    token: str,
    *,
    action_revision_id: UUID,
    projection_id: UUID,
    queue_version: int,
    expires_at: datetime,
    signing_key: bytes,
) -> bool:
    decoded = decode_projection_local_action_token(token)
    if decoded is None:
        return False
    decoded_revision, decoded_projection, decoded_version, decoded_expiry = decoded
    expected_payload = _local_action_token_payload(
        action_revision_id, projection_id, queue_version, expires_at
    )
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    payload, supplied_mac = raw[:-_MAC_BYTES], raw[-_MAC_BYTES:]
    expected_mac = hmac.digest(
        signing_key,
        b"docket-projection-local-action-token-v1\x00" + expected_payload,
        "sha256",
    )[:_MAC_BYTES]
    return (
        decoded_revision == action_revision_id
        and decoded_projection == projection_id
        and decoded_version == queue_version
        and decoded_expiry == expires_at.astimezone(UTC).replace(microsecond=0)
        and hmac.compare_digest(payload, expected_payload)
        and hmac.compare_digest(supplied_mac, expected_mac)
    )


def _proposal_control_token_payload(
    action_revision_id: UUID,
    projection_id: UUID,
    field: str,
    expires_at: datetime,
) -> bytes:
    try:
        field_code = _PROPOSAL_FIELDS[field]
    except KeyError as exc:
        raise ValueError("unknown proposal control field") from exc
    return (
        bytes([_PROPOSAL_CONTROL_TOKEN_VERSION])
        + action_revision_id.bytes
        + projection_id.bytes
        + bytes([field_code])
        + _timestamp(expires_at).to_bytes(8, "big")
    )


def issue_projection_proposal_control_token(
    action_revision_id: UUID,
    projection_id: UUID,
    field: str,
    expires_at: datetime,
    signing_key: bytes,
) -> str:
    payload = _proposal_control_token_payload(
        action_revision_id,
        projection_id,
        field,
        expires_at,
    )
    signature = hmac.digest(
        signing_key,
        b"docket-projection-proposal-control-token-v1\x00" + payload,
        "sha256",
    )
    return base64.urlsafe_b64encode(payload + signature[:_MAC_BYTES]).rstrip(
        b"="
    ).decode()


def decode_projection_proposal_control_token(
    token: str,
) -> tuple[UUID, UUID, str, datetime] | None:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, UnicodeEncodeError):
        return None
    canonical_token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    payload_length = 1 + 16 + 16 + 1 + 8
    if (
        not hmac.compare_digest(token, canonical_token)
        or len(raw) != payload_length + _MAC_BYTES
        or raw[0] != _PROPOSAL_CONTROL_TOKEN_VERSION
    ):
        return None
    field = _PROPOSAL_FIELD_NAMES.get(raw[33])
    if field is None:
        return None
    payload = raw[:payload_length]
    return (
        UUID(bytes=payload[1:17]),
        UUID(bytes=payload[17:33]),
        field,
        datetime.fromtimestamp(int.from_bytes(payload[34:42], "big"), tz=UTC),
    )


def verify_projection_proposal_control_token(
    token: str,
    *,
    action_revision_id: UUID,
    projection_id: UUID,
    field: str,
    expires_at: datetime,
    signing_key: bytes,
) -> bool:
    decoded = decode_projection_proposal_control_token(token)
    if decoded is None:
        return False
    decoded_revision, decoded_projection, decoded_field, decoded_expiry = decoded
    expected_payload = _proposal_control_token_payload(
        action_revision_id,
        projection_id,
        field,
        expires_at,
    )
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    payload, supplied_mac = raw[:-_MAC_BYTES], raw[-_MAC_BYTES:]
    expected_mac = hmac.digest(
        signing_key,
        b"docket-projection-proposal-control-token-v1\x00" + expected_payload,
        "sha256",
    )[:_MAC_BYTES]
    return (
        decoded_revision == action_revision_id
        and decoded_projection == projection_id
        and decoded_field == field
        and decoded_expiry == expires_at.astimezone(UTC).replace(microsecond=0)
        and hmac.compare_digest(payload, expected_payload)
        and hmac.compare_digest(supplied_mac, expected_mac)
    )


def verify_approval_token(
    token: str,
    *,
    approval_id: UUID,
    expires_at: datetime,
    signing_key: bytes,
) -> bool:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, UnicodeEncodeError):
        return False
    canonical_token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    if not hmac.compare_digest(token, canonical_token):
        return False
    expected_payload = _token_payload(approval_id, expires_at)
    if len(raw) != len(expected_payload) + _MAC_BYTES:
        return False
    payload, supplied_mac = raw[:-_MAC_BYTES], raw[-_MAC_BYTES:]
    expected_mac = hmac.digest(
        signing_key, b"docket-approval-token-v1\x00" + expected_payload, "sha256"
    )[:_MAC_BYTES]
    return hmac.compare_digest(payload, expected_payload) and hmac.compare_digest(
        supplied_mac, expected_mac
    )


def issue_short_code(approval_id: UUID, expires_at: datetime, signing_key: bytes) -> str:
    digest = hmac.digest(
        signing_key,
        b"docket-approval-short-code-v1\x00" + _token_payload(approval_id, expires_at),
        "sha256",
    )
    number = int.from_bytes(digest[:8], "big")
    characters: list[str] = []
    for _ in range(10):
        number, index = divmod(number, len(_SHORT_ALPHABET))
        characters.append(_SHORT_ALPHABET[index])
    code = "".join(characters)
    return f"{code[:5]}-{code[5:]}"


def normalize_short_code(value: str) -> str:
    return value.strip().upper()


def short_code_sha256(value: str) -> str:
    return hashlib.sha256(normalize_short_code(value).encode()).hexdigest()
