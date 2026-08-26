import base64
import json

import httpx
import pytest

from docket.providers.google.gmail import (
    GmailMutationRequest,
    GmailUnknownOutcome,
    GoogleGmailProvider,
    _extract_content,
)


def _response(status: int, body: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "x-request-id": "gmail-request-1",
        },
        request=httpx.Request("GET", "https://gmail.googleapis.test"),
    )


def _metadata(*, history_id: str, labels: list[str]) -> dict[str, object]:
    return {
        "id": "message-1",
        "threadId": "thread-1",
        "historyId": history_id,
        "internalDate": "1785060000000",
        "labelIds": labels,
        "sizeEstimate": 42,
        "payload": {"headers": []},
    }


def _request() -> GmailMutationRequest:
    return GmailMutationRequest(
        message_id="message-1",
        source_version="10",
        remove_label_id="INBOX",
        provider_correlation="operation-1",
    )


def test_claimed_body_is_bounded_for_isolated_triage_context() -> None:
    body = "x" * 25000
    encoded = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")

    extracted, attachments = _extract_content(
        {
            "mimeType": "text/plain",
            "body": {"data": encoded, "size": len(body)},
        }
    )

    assert extracted == "x" * 20000
    assert attachments == ()


def test_modify_refetches_full_metadata_when_response_omits_history_id(
    monkeypatch,
) -> None:
    provider = GoogleGmailProvider("unused-token-file")
    monkeypatch.setattr(provider, "_authorization_header", lambda: "Bearer test")
    responses = iter(
        (
            _response(200, _metadata(history_id="10", labels=["INBOX", "UNREAD"])),
            _response(
                200,
                {
                    "id": "message-1",
                    "threadId": "thread-1",
                    "labelIds": ["UNREAD"],
                },
            ),
            _response(200, _metadata(history_id="11", labels=["UNREAD"])),
        )
    )
    monkeypatch.setattr(httpx, "request", lambda *_args, **_kwargs: next(responses))

    result = provider.mutate_message(_request())

    assert result.source_version == "11"
    assert result.label_ids == ("UNREAD",)
    assert result.disposition == "modified"
    assert result.provider_request_id == "gmail-request-1"


def test_modify_treats_post_write_refetch_failure_as_unknown(
    monkeypatch,
) -> None:
    provider = GoogleGmailProvider("unused-token-file")
    monkeypatch.setattr(provider, "_authorization_header", lambda: "Bearer test")
    responses = iter(
        (
            _response(200, _metadata(history_id="10", labels=["INBOX", "UNREAD"])),
            _response(200, {"id": "message-1", "labelIds": ["UNREAD"]}),
            _response(500, {"error": {"code": 500}}),
        )
    )
    monkeypatch.setattr(httpx, "request", lambda *_args, **_kwargs: next(responses))

    with pytest.raises(GmailUnknownOutcome):
        provider.mutate_message(_request())
