"""Bounded financial page rendering from an owned, encrypted original."""

import base64
import io
import json
import math
import re
import subprocess
from typing import Any

from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.config import Settings
from docket.models.financial_intake import FinancialIntake
from docket.services.financial_files import rejected
from docket.services.funding import attachment_store
from docket.services.receipt_documents import document_text, documents

MAX_PAGES = 25
MAX_EDGE = 1600
MAX_BYTES = 800_000


def pdf_page(data: bytes, page: int) -> tuple[bytes, int]:
    def run(args: list[str]) -> bytes:
        try:
            result = subprocess.run(
                ["prlimit", "--as=536870912", "--cpu=5", "--", *args],
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise rejected("financial_page_unreadable") from exc
        return result.stdout

    info = run(["pdfinfo", "-"])
    pages = re.search(rb"(?m)^Pages:\s+(\d+)$", info)
    if not pages or re.search(rb"(?m)^Encrypted:\s+yes", info):
        raise rejected("financial_page_unreadable")
    count = int(pages[1])
    if page > count or count > MAX_PAGES:
        raise rejected("financial_page_limit")
    pixels = run(
        [
            "pdftoppm",
            "-f",
            str(page),
            "-l",
            str(page),
            "-singlefile",
            "-scale-to",
            str(MAX_EDGE),
            "-jpeg",
            "-",
        ]
    )
    return pixels, count


def read_visual(
    session: Session, settings: Settings, source_ref: str, part: str, page: int
) -> CallToolResult:
    owner = require_principal("read").operator_ref
    row = session.get(FinancialIntake, source_ref)
    if row is None or row.operator_ref != owner:
        raise rejected("financial_source_unavailable")
    raw = attachment_store(session, settings).plaintext(source_ref)
    doc = next((d for d in documents(raw, row.media_type) if d.anchor == part), None)
    if doc is not None and doc.media in ("text/plain", "text/html"):
        text = document_text(doc)
        count = max(1, math.ceil(len(text) / 1600))
        if page > count or count > MAX_PAGES:
            raise rejected("financial_page_limit")
        details = dict(
            ok=True,
            source_ref=source_ref,
            part=part,
            page=page,
            pages=count,
            untrusted_text=text[(page - 1) * 1600 : page * 1600],
        )
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(details))], structured_content=details
        )
    if doc is None or doc.media not in ("application/pdf", "image/jpeg", "image/png"):
        raise rejected("financial_visual_unavailable")
    data, count = doc.data, 1
    if doc.media == "application/pdf":
        data, count = pdf_page(data, page)
    elif page != 1:
        raise rejected("financial_page_limit")
    try:
        with Image.open(io.BytesIO(data), formats=("JPEG", "PNG")) as picture:
            if picture.width * picture.height > 16_000_000:
                raise rejected("financial_page_limit")
            picture.thumbnail((MAX_EDGE, MAX_EDGE))
            image = ImageOps.exif_transpose(picture).convert("RGB")
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=84)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise rejected("financial_page_unreadable") from exc
    if output.tell() > MAX_BYTES:
        raise rejected("financial_page_limit")
    metadata: dict[str, Any] = dict(
        ok=True,
        source_ref=source_ref,
        part=part,
        page=page,
        pages=count,
        width=image.width,
        height=image.height,
    )
    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps(metadata)),
            ImageContent(
                type="image",
                mime_type="image/jpeg",
                data=base64.b64encode(output.getvalue()).decode("ascii"),
            ),
        ],
        structured_content=metadata,
    )
