"""DNS-pinned outbound HTTPS. Never follows redirects or uses proxy environment."""

import base64
import hashlib
import hmac
import http.client
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit


def signing_key(secret: str) -> bytes:
    if not secret.startswith("whsec_"):
        raise ValueError("A Standard Webhooks secret is required")
    key = base64.b64decode(secret[6:], validate=True)
    if not 24 <= len(key) <= 64:
        raise ValueError("The webhook signing key must contain 24-64 bytes")
    return key


def signed_headers(
    event_id: str,
    body: bytes,
    secret: str,
    subscription_ref: str,
    previous_secret: str | None = None,
) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signed = event_id.encode() + b"." + timestamp.encode() + b"." + body
    signatures = []
    for value in (secret, previous_secret):
        if value is not None:
            digest = hmac.new(signing_key(value), signed, hashlib.sha256).digest()
            signatures.append("v1," + base64.b64encode(digest).decode())
    return {
        "Content-Type": "application/json",
        "webhook-id": event_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": " ".join(signatures),
        "X-MCP-Subscription-Id": subscription_ref,
    }


def validate_callback_url(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    if (
        len(url) > 2048
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.port not in (None, 443)
        or any(ord(c) < 33 for c in url)
    ):
        raise ValueError("A public HTTPS callback on port 443 is required")
    host = parsed.hostname.encode("idna").decode("ascii")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("Local and private callbacks are prohibited")
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    return host, target


@dataclass(frozen=True)
class WebhookResponse:
    status: int
    body: bytes = b""


class WebhookPoster(Protocol):
    def post(self, url: str, body: bytes, headers: dict[str, str]) -> WebhookResponse: ...


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str) -> None:
        super().__init__(host, port=443, timeout=10, context=ssl.create_default_context())
        self.address = address

    def connect(self) -> None:
        sock = socket.create_connection((self.address, 443), timeout=10)
        try:
            self.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class PinnedWebhookPoster:
    def post(self, url: str, body: bytes, headers: dict[str, str]) -> WebhookResponse:
        host, target = validate_callback_url(url)
        if len(body) > 16384:
            raise ValueError("Docket webhook payload exceeds its bounded limit")
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(str(answer[4][0]) for answer in answers))
        if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
            raise ValueError("Callback DNS contains a private or invalid address")
        connection = _PinnedHTTPSConnection(host, addresses[0])
        try:
            connection.request("POST", target, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(8193)
            if len(payload) > 8192:
                raise ValueError("Callback response exceeds limit")
            # Redirect status is returned as-is; no Location is followed.
            return WebhookResponse(response.status, payload)
        finally:
            connection.close()
