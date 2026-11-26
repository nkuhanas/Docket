"""Document identities within the original protected financial source."""

from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import Any

from pypdf.errors import PyPdfError

from docket.domain.errors import DocketError
from docket.services.financial_files import BillingText, extract, rejected


@dataclass(frozen=True)
class EvidenceDocument:
    anchor: str
    media: str
    data: bytes
    name: str = ""


def documents(raw: bytes, media: str) -> list[EvidenceDocument]:
    if media != "message/rfc822":
        return [EvidenceDocument("file", media, raw)]
    message = BytesParser(policy=policy.default).parsebytes(raw)
    body = message.get_body(preferencelist=("plain", "html"))
    result = []
    for index, part in enumerate(message.walk()):
        if index >= 32:
            raise rejected("message_part_limit")
        kind = part.get_content_type()
        if part.is_multipart():
            continue
        if kind in ("text/plain", "text/html") and part is not body and not part.is_attachment():
            continue
        data = part.get_payload(decode=True)
        if isinstance(data, bytes):
            if kind == "application/octet-stream":
                for signature, detected in (
                    (b"%PDF-", "application/pdf"),
                    (b"\x89PNG\r\n\x1a\n", "image/png"),
                    (b"\xff\xd8\xff", "image/jpeg"),
                ):
                    if data.startswith(signature):
                        kind = detected
                        break
            result.append(
                EvidenceDocument(f"mime/{index}", kind, data, str(part.get_filename() or "")[:180])
            )
    return result or [EvidenceDocument("file", media, raw)]


def document_text(doc: EvidenceDocument) -> str:
    if doc.media not in ("text/plain", "text/html"):
        return ""
    value = doc.data[:64000].decode("utf-8", errors="replace")
    if doc.media == "text/html":
        parser = BillingText()
        parser.feed(value)
        return "".join(parser.parts)
    return value


def document_facts(doc: EvidenceDocument) -> list[dict[str, Any]]:
    from docket.services.financial_files import purchase

    facts: dict[str, Any] = {}
    try:
        if doc.media in ("text/html", "text/plain"):
            facts = purchase(document_text(doc), doc.anchor)
        elif doc.media == "application/pdf":
            pages = extract(doc.data, doc.media)
            if len(pages) > 1 and all(p.get("document_id") for p in pages):
                ids = {(p.get("vendor"), p["document_id"]) for p in pages}
                if len(ids) == len(pages):
                    return [
                        {
                            **p,
                            "anchor": p["anchor"].replace("file", doc.anchor, 1),
                            "media_type": doc.media,
                        }
                        for p in pages
                    ]
            if len(pages) == 1:
                facts = pages[0]
    except (ValueError, RuntimeError, PyPdfError, DocketError, OSError, IndexError, KeyError):
        pass
    return [{**facts, "anchor": doc.anchor, "media_type": doc.media}]


def mail_subject(raw: bytes) -> str:
    message: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    return str(message.get("subject", ""))[:200]
