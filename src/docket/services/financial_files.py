"""Bounded file transfer and deterministic financial extraction."""

import csv
import hashlib
import http.client
import io
import re
import ssl
from datetime import date
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from pathlib import PurePath
from typing import Any
from urllib.parse import urljoin, urlsplit

from pypdf import PdfReader

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError

MEDIA = {
    "pdf": "application/pdf",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "eml": "message/rfc822",
    "csv": "text/csv",
}


def rejected(code: str) -> DocketError:
    return DocketError(code=code, message=code.replace("_", " ") + ".")


def download_file(url: str, hosts: frozenset[str], limit: int) -> bytes:
    """Fetch short-lived capabilities without logging the URL or following arbitrary hosts."""
    try:
        for _ in range(4):
            parts = urlsplit(url)
            if (
                parts.scheme != "https"
                or not parts.hostname
                or not any(
                    parts.hostname == h or (h.startswith("*.") and parts.hostname.endswith(h[1:]))
                    for h in hosts
                )
                or parts.port not in (None, 443)
                or parts.username
                or parts.password
                or parts.fragment
            ):
                raise rejected("file_host_not_approved")
            connection = http.client.HTTPSConnection(
                parts.hostname, timeout=10, context=ssl.create_default_context()
            )
            try:
                path = parts.path or "/"
                connection.request("GET", path + ("?" + parts.query if parts.query else ""))
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        break
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    break
                data = response.read(limit + 1)
                if not data or len(data) > limit:
                    raise rejected("invalid_file_size")
                return data
            finally:
                connection.close()
    except (ValueError, OSError, http.client.HTTPException):
        pass
    raise rejected("file_transfer_failed")


def media_type(data: bytes, declared: str | None, name: str | None) -> str:
    if data.startswith(b"%PDF-"):
        actual = "application/pdf"
    elif data.startswith(b"\xff\xd8\xff"):
        actual = "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        actual = "image/png"
    else:
        hint = MEDIA.get(PurePath(name or "").suffix.lower().lstrip(".")) or declared
        if hint == "message/rfc822":
            message = BytesParser(policy=policy.default).parsebytes(data, headersonly=True)
            if not message.keys() or message.defects:
                raise rejected("invalid_message_headers")
        elif hint == "text/csv":
            try:
                header = next(csv.reader(io.StringIO(data.decode("utf-8-sig")), strict=True))
                if len(header) < 2 or b"\x00" in data:
                    raise ValueError
            except (ValueError, StopIteration, csv.Error) as exc:
                raise rejected("invalid_csv") from exc
        else:
            raise rejected("unsupported_financial_media")
        actual = str(hint)
    if declared and declared.split(";")[0] not in (actual, "application/octet-stream"):
        raise rejected("financial_media_mismatch")
    return actual


def purchase(text: str, anchor: str) -> dict[str, Any]:
    """Accept explicit billing labels and a currency-bearing billed total."""
    text = "\n".join(line.strip() for line in text.splitlines())
    facts: dict[str, Any] = {"anchor": anchor, "method": "labelled_receipt_v1"}
    if re.search(
        r"(?im)^\s*(?:quote|estimate|quotation|pro[ -]?forma|credit note|cancelled|void)\b", text
    ):
        facts["review_only"] = True
    for field, pattern in (
        ("vendor", r"(?im)^\s*(?:merchant|vendor|seller)\s*:\s*([^\r\n]{1,120})$"),
        (
            "document_id",
            r"(?im)^\s*(?:invoice|receipt|order)"
            r"\s*(?:no\.?|number|id|#)\s*:?\s*([\w.-]{1,80})\s*$",
        ),
        ("incurred_on", r"(?im)^\s*(?:date|invoice date)\s*:\s*(\d{4}-\d{2}-\d{2})\s*$"),
    ):
        matches = set(v.strip() for v in re.findall(pattern, text))
        if len(matches) == 1:
            value = matches.pop()
            if not re.search(r"(?:\d[ -]?){12}", value):
                if field == "incurred_on":
                    try:
                        date.fromisoformat(value)
                    except ValueError:
                        continue
                facts[field] = value
    totals = set(
        re.findall(
            r"(?im)^\s*(?:grand total|total(?: due| paid)?)\s*:\s*([A-Z]{3})"
            r"\s+([0-9]{1,15}(?:\.[0-9]{1,6})?)\s*$",
            text,
        )
    )
    if len(totals) == 1:
        currency, amount = totals.pop()
        if Decimal(amount) > 0:
            facts["currency"], facts["amount"] = currency.upper(), amount
    return facts


def purchase_key(facts: dict[str, Any]) -> str | None:
    if facts.get("review_only") or not all(
        facts.get(k) for k in ("vendor", "document_id", "amount", "currency")
    ):
        return None
    return sha256_json([facts["vendor"].casefold(), facts["document_id"]])


def pdf_text(blob: bytes, anchor: str) -> list[dict[str, Any]]:
    reader = PdfReader(io.BytesIO(blob), strict=True)
    if reader.is_encrypted or len(reader.pages) > 25:
        return []
    return [
        purchase((page.extract_text() or "")[:32000], f"{anchor}/page/{i}")
        for i, page in enumerate(reader.pages, 1)
    ]


def extract(blob: bytes, media: str) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    if media == "application/pdf":
        facts = pdf_text(blob, "file")
    if media == "message/rfc822":
        message = BytesParser(policy=policy.default).parsebytes(blob)
        size = 0
        for i, part in enumerate(message.walk()):
            if i >= 32:
                raise rejected("message_part_limit")
            data = part.get_payload(decode=True)
            if not isinstance(data, bytes):
                continue
            size += len(data)
            if size > len(blob) * 2:
                raise rejected("message_part_limit")
            anchor = f"mime/{i}"
            if part.get_content_type() == "text/plain":
                facts.append(purchase(data[:64000].decode("utf-8", errors="replace"), anchor))
            elif part.get_content_type() == "application/pdf" and data.startswith(b"%PDF-"):
                facts += pdf_text(data, anchor)
    # Keep competing assertions visible while combining identical representations.
    unique: dict[str, dict[str, Any]] = {}
    for item in facts:
        if "amount" not in item and "vendor" not in item:
            continue
        fingerprint = hashlib.sha256(
            str(sorted((k, v) for k, v in item.items() if k != "anchor")).encode()
        ).hexdigest()
        if fingerprint in unique:
            unique[fingerprint].setdefault("other_anchors", []).append(item["anchor"])
        else:
            unique[fingerprint] = item
    if len(unique) > 40:
        raise rejected("purchase_count_limit")
    return list(unique.values())
