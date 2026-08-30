from __future__ import annotations

import base64
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

_TOKEN_VERSION = 8
_MAC_BYTES = 12


@dataclass(frozen=True)
class SemanticOptionReference:
    option_row_id: UUID
    actor_id: str
    expires_at: datetime


def _timestamp(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp())


def _payload(
    *,
    option_row_id: UUID,
    actor_id: str,
    expires_at: datetime,
) -> bytes:
    try:
        actor = int(actor_id)
    except ValueError as exc:
        raise ValueError("actor_id is not a Discord snowflake") from exc
    expiry = _timestamp(expires_at)
    if actor < 0 or actor >= 2**64 or expiry < 0 or expiry >= 2**32:
        raise ValueError("semantic option binding is outside the signed token range")
    return (
        bytes([_TOKEN_VERSION])
        + option_row_id.bytes
        + actor.to_bytes(8, "big")
        + expiry.to_bytes(4, "big")
    )


def issue_semantic_option_token(
    *,
    option_row_id: UUID,
    actor_id: str,
    expires_at: datetime,
    signing_key: bytes,
) -> str:
    payload = _payload(
        option_row_id=option_row_id,
        actor_id=actor_id,
        expires_at=expires_at,
    )
    signature = hmac.digest(
        signing_key,
        b"docket-semantic-option-token-v1\x00" + payload,
        "sha256",
    )
    return base64.urlsafe_b64encode(payload + signature[:_MAC_BYTES]).rstrip(b"=").decode()


def decode_semantic_option_token(token: str) -> SemanticOptionReference | None:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, UnicodeEncodeError):
        return None
    canonical = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    payload_length = 1 + 16 + 8 + 4
    if (
        not hmac.compare_digest(token, canonical)
        or len(raw) != payload_length + _MAC_BYTES
        or raw[0] != _TOKEN_VERSION
    ):
        return None
    return SemanticOptionReference(
        option_row_id=UUID(bytes=raw[1:17]),
        actor_id=str(int.from_bytes(raw[17:25], "big")),
        expires_at=datetime.fromtimestamp(int.from_bytes(raw[25:29], "big"), tz=UTC),
    )


def verify_semantic_option_token(
    token: str,
    *,
    reference: SemanticOptionReference,
    signing_key: bytes,
) -> bool:
    decoded = decode_semantic_option_token(token)
    if decoded is None or decoded != reference:
        return False
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    payload = _payload(
        option_row_id=reference.option_row_id,
        actor_id=reference.actor_id,
        expires_at=reference.expires_at,
    )
    expected_mac = hmac.digest(
        signing_key,
        b"docket-semantic-option-token-v1\x00" + payload,
        "sha256",
    )[:_MAC_BYTES]
    return hmac.compare_digest(raw[:-_MAC_BYTES], payload) and hmac.compare_digest(
        raw[-_MAC_BYTES:], expected_mac
    )
