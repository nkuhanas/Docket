"""Bounded file transfer and deterministic financial extraction."""

import csv
import hashlib
import http.client
import io
import re
import socket
import ssl
from collections.abc import Callable
from contextlib import suppress
from datetime import date, datetime
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import PurePath
from typing import Any
from urllib.parse import urljoin, urlsplit

from pypdf import PdfReader
from pypdf.errors import PdfReadError

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
        for hop in range(4):
            parts = urlsplit(url)
            if hop == 0 and parts.scheme in ("", "sandbox", "file", "attachment", "file-service"):
                raise DocketError(
                    code="file_attachment_not_bound",
                    message="The client did not supply a downloadable original. Stop here.",
                )
            if (
                parts.scheme != "https"
                or not parts.hostname
                or not any(
                    parts.hostname == h or (h.startswith("*.") and parts.hostname.endswith(h[1:]))
                    for h in hosts
                )
                or parts.port not in (None, 443)
                or parts.username is not None
                or parts.password is not None
                or parts.fragment
            ):
                if hop:
                    raise DocketError(
                        code="file_redirect_not_approved",
                        message="The original redirected outside the approved HTTPS destinations. "
                        "Docket could not receive it.",
                    )
                raise DocketError(
                    code="file_host_not_approved",
                    message="The supplied link is outside the approved HTTPS file destinations. "
                    "Docket could not receive the original.",
                )
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
                    if response.status in (401, 403, 404, 410):
                        raise DocketError(
                            code="file_download_unavailable",
                            message="The file service could not provide the original attachment.",
                        )
                    break
                data = response.read(limit + 1)
                if not data or len(data) > limit:
                    raise rejected("invalid_file_size")
                return data
            finally:
                connection.close()
    except socket.gaierror:
        raise DocketError(
            code="file_download_dns_failed",
            message="Docket could not resolve the approved file host to download the original.",
        ) from None
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
        if hint in (None, "application/octet-stream"):
            first = data[:8192]
            if re.match(
                rb"(?i)^(?:from|to|received|date|return-path|subject|mime-version|message-id):",
                first,
            ):
                hint = "message/rfc822"
            elif b"," in first.split(b"\n", 1)[0]:
                hint = "text/csv"
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


def payroll(text: str, anchor: str) -> dict[str, Any]:
    """Extract only explicit, uniquely labelled amounts. Never retain free-form payroll text."""
    labels = {
        "gross pay": "gross",
        "gross wages": "gross",
        "nonemployee compensation": "gross",
        "taxable wages": "taxable_wages",
        "wages, tips, other compensation": "taxable_wages",
        "federal income tax withheld": "withheld",
        "withheld": "withheld",
        "net pay": "net",
    }
    found: dict[str, set[tuple[str, str]]] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"\s*([^:]{1,60}):\s*([A-Z]{3})\s+(\d{1,18}(?:\.\d{1,9})?)\s*", line)
        if match and (field := labels.get(match[1].strip().casefold())):
            found.setdefault(field, set()).add((match[2], match[3]))
    result: dict[str, Any] = {"anchor": anchor}
    currencies = {currency for values in found.values() for currency, _ in values}
    if len(currencies) == 1:
        result["currency"] = currencies.pop()
        for name, values in found.items():
            if len(values) == 1:
                result[name] = next(iter(values))[1]
    return result


TextFacts = Callable[[str, str], dict[str, Any]]


def tabular_invoice(layout: str, labelled: dict[str, Any]) -> dict[str, Any]:
    """Read a single issuer/bill-to invoice with an explicitly denominated total."""
    lines = [line.strip() for line in layout.splitlines() if line.strip()]
    if len(layout) > 32000 or lines.count("Invoice") != 1 or labelled.get("review_only"):
        return labelled

    # A right-hand Bill to heading identifies the seller on its left. A standalone
    # company name, billing address or customer label does not identify the issuer.
    sellers = [m[1] for line in lines if (m := re.fullmatch(r"(.{1,120}?) {2,}Bill to", line))]
    totals = [line for line in lines if re.match(r"Total\b", line)]
    balances = [line for line in lines if re.match(r"Amount due\b", line)]
    dates = [line for line in lines if re.match(r"Date of issue\b", line)]
    if any(len(values) != 1 for values in (sellers, totals, balances, dates)):
        return labelled
    seller = sellers[0].strip()
    if (
        not any(c.isalpha() for c in seller)
        or "  " in seller
        or "@" in seller
        or not seller.isprintable()
        or re.search(r"(?:\d[ -]?){12}", seller)
        or seller.casefold() in {"from", "seller", "vendor", "ship to", "bill from", "bill to"}
    ):
        return labelled
    amount = r"((?:[0-9]{1,3}(?:,[0-9]{3}){1,4}|[0-9]{1,15})(?:\.[0-9]{1,6})?)"
    total = re.fullmatch(r"Total\s+([$£€]?)" + amount, totals[0])
    balance = re.fullmatch(r"Amount due\s+([$£€]?)" + amount + r"\s+([A-Z]{3})", balances[0])
    if not total or not balance:
        return labelled
    currency = balance[3]
    allowed = {"$": {"USD", "CAD", "AUD", "NZD", "SGD", "HKD"}, "€": {"EUR"}, "£": {"GBP"}}
    if any(symbol and currency not in allowed[symbol] for symbol in (total[1], balance[1])):
        return labelled
    value = total[2].replace(",", "")
    if Decimal(value) <= 0 or Decimal(value) != Decimal(balance[2].replace(",", "")):
        return labelled
    try:
        issued = datetime.strptime(dates[0].removeprefix("Date of issue").strip(), "%B %d, %Y")
    except ValueError:
        return labelled
    fields = dict(
        vendor=seller, currency=currency, amount=value, incurred_on=issued.date().isoformat()
    )
    if any(key in labelled and labelled[key] != val for key, val in fields.items()):
        return labelled
    return {**labelled, **fields, "method": "tabular_invoice_v1"}


def pdf_text(blob: bytes, anchor: str, parse: TextFacts = purchase) -> list[dict[str, Any]]:
    reader = PdfReader(io.BytesIO(blob), strict=True)
    if reader.is_encrypted or len(reader.pages) > 25:
        return []
    facts = []
    for i, page in enumerate(reader.pages, 1):
        plain = page.extract_text() or ""
        item = parse(plain[:32000], f"{anchor}/page/{i}")
        if (
            parse is purchase
            and len(reader.pages) == 1
            and len(plain) <= 32000
            and not item.get("review_only")
            and not {"vendor", "amount"} <= item.keys()
            and "Bill to" in plain
            and "Amount due" in plain
            and "Date of issue" in plain
        ):
            # Some PDFs provide plain text while layout reconstruction fails.
            with suppress(PdfReadError, ValueError, IndexError, KeyError):
                item = tabular_invoice(page.extract_text(extraction_mode="layout") or "", item)
        facts.append(item)
    return facts


class BillingText(HTMLParser):
    """Collect visible billing text without resolving links, styles or remote resources."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"style", "script", "head"}:
            self.hidden += 1
        if tag in {"p", "tr", "div", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"style", "script", "head"} and self.hidden:
            self.hidden -= 1
        if tag in {"td", "th"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def extract(blob: bytes, media: str, *, income: bool = False) -> list[dict[str, Any]]:
    parse = payroll if income else purchase
    facts: list[dict[str, Any]] = []
    if media == "application/pdf":
        facts = pdf_text(blob, "file", parse)
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
                facts.append(parse(data[:64000].decode("utf-8", errors="replace"), anchor))
            elif part.get_content_type() == "text/html":
                parser = BillingText()
                parser.feed(data[:64000].decode("utf-8", errors="replace"))
                facts.append(parse("".join(parser.parts), anchor))
            elif part.get_content_type() == "application/pdf" and data.startswith(b"%PDF-"):
                facts += pdf_text(data, anchor, parse)
    # Keep competing assertions visible while combining identical representations.
    unique: dict[str, dict[str, Any]] = {}
    for item in facts:
        if not (
            set(item)
            & ({"gross", "taxable_wages", "withheld", "net"} if income else {"amount", "vendor"})
        ):
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
