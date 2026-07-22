import base64
import hashlib
import hmac
from datetime import UTC, datetime
from uuid import UUID

_TOKEN_VERSION = 1
_MAC_BYTES = 16
_SHORT_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


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
