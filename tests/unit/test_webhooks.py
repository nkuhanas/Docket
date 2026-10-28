import base64
import hashlib
import hmac
from unittest.mock import Mock

import pytest

from docket.providers import webhooks


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://169.254.169.254/",
        "https://user:pass@example.com/",
        "https://example.com:8443/",
        "https://example.com/#fragment",
        "https://exam\nple.com/",
    ],
)
def test_callback_rejects_non_public_or_ambiguous_urls(url):
    with pytest.raises(ValueError):
        webhooks.validate_callback_url(url)


def test_dns_is_revalidated_and_all_answers_must_be_public(monkeypatch):
    monkeypatch.setattr(
        webhooks.socket,
        "getaddrinfo",
        lambda *a, **k: [
            (2, 1, 6, "", ("8.8.8.8", 443)),
            (2, 1, 6, "", ("10.0.0.1", 443)),
        ],
    )
    connection = Mock()
    monkeypatch.setattr(webhooks, "_PinnedHTTPSConnection", connection)
    with pytest.raises(ValueError, match="private"):
        webhooks.PinnedWebhookPoster().post("https://receiver.example.com/", b"{}", {})
    connection.assert_not_called()


def test_public_connection_is_pinned_and_redirects_are_returned_without_following(monkeypatch):
    monkeypatch.setattr(
        webhooks.socket,
        "getaddrinfo",
        lambda *a, **k: [
            (2, 1, 6, "", ("8.8.8.8", 443)),
        ],
    )
    connection = Mock()
    connection.return_value.getresponse.return_value.status = 302
    connection.return_value.getresponse.return_value.read.return_value = b""
    monkeypatch.setattr(webhooks, "_PinnedHTTPSConnection", connection)
    result = webhooks.PinnedWebhookPoster().post("https://receiver.example.com/a?q=1", b"{}", {})
    connection.assert_called_once_with("receiver.example.com", "8.8.8.8")
    connection.return_value.request.assert_called_once_with(
        "POST", "/a?q=1", body=b"{}", headers={}
    )
    assert result.status == 302


def test_pinned_socket_preserves_original_hostname_for_tls(monkeypatch):
    sock, tls = Mock(), Mock()
    connect = Mock(return_value=sock)
    monkeypatch.setattr(webhooks.socket, "create_connection", connect)
    monkeypatch.setattr(webhooks.ssl, "create_default_context", lambda: tls)
    connection = webhooks._PinnedHTTPSConnection("receiver.example.com", "8.8.8.8")
    connection.connect()
    connect.assert_called_once_with(("8.8.8.8", 443), timeout=10)
    tls.wrap_socket.assert_called_once_with(sock, server_hostname="receiver.example.com")


def test_standard_webhooks_signs_exact_bytes_and_both_rotation_keys(monkeypatch):
    monkeypatch.setattr(webhooks.time, "time", lambda: 1800000000)
    keys = (b"a" * 32, b"b" * 32)
    secrets = ["whsec_" + base64.b64encode(key).decode() for key in keys]
    body = b'{"data":"unchanged"}'
    headers = webhooks.signed_headers("notice_test", body, secrets[0], "sub_test", secrets[1])
    expected = [
        "v1,"
        + base64.b64encode(
            hmac.new(
                key,
                b"notice_test.1800000000." + body,
                hashlib.sha256,
            ).digest()
        ).decode()
        for key in keys
    ]
    assert headers["webhook-signature"].split() == expected
    assert headers["X-MCP-Subscription-Id"] == "sub_test"
