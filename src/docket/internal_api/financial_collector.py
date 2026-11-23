"""Private, submit-only financial collection. No mail credential crosses this boundary."""

import hashlib
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.database import session_scope
from docket.domain.errors import DocketError
from docket.models.financial_intake import FinancialCollector
from docket.services.financial_intake import FinancialIntakeService

router = APIRouter(prefix="/internal/v1/financial-collector")


def authorize(session: Session, request: Request) -> FinancialCollector:
    header = request.headers.get("Authorization", "")
    if not re.fullmatch(r"Bearer [A-Za-z0-9_-]{43}", header):
        raise HTTPException(401)
    digest = hashlib.sha256(header[7:].encode()).hexdigest()
    grant = session.scalar(
        select(FinancialCollector)
        .where(FinancialCollector.credential_digest == digest, FinancialCollector.enabled.is_(True))
        .with_for_update()
    )
    if not grant:
        raise HTTPException(403)
    return grant


@router.get("/policy")
def policy(request: Request) -> dict[str, Any]:
    with session_scope() as session:
        grant = authorize(session, request)
        return {"source": grant.ref_id, "revision": grant.revision, **grant.policy}


@router.post("/messages")
async def accept(request: Request) -> dict[str, Any]:
    settings = get_settings()
    with session_scope() as session:
        authorize(session, request)
    delivery = request.headers.get("X-Delivery-ID", "")
    if not re.fullmatch(r"[1-9][0-9]{0,9}:[1-9][0-9]{0,9}", delivery):
        raise HTTPException(422, "Invalid mailbox delivery identity")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > settings.attachment_max_bytes:
            raise HTTPException(413)
        data.extend(chunk)
    try:
        with session_scope() as session:
            grant = authorize(session, request)
            if request.headers.get("X-Policy-Revision") != str(grant.revision):
                raise HTTPException(409, "Refresh source policy")
            service = FinancialIntakeService(session, settings)
            row = service.admit(
                bytes(data),
                delivery=delivery,
                input_hash=hashlib.sha256(data).hexdigest(),
                declared="message/rfc822",
                filename=None,
                declarations={},
                collector=grant,
            )
            return {
                "source_accepted": True,
                "source_ref": row.source_ref,
                "original_sha256": row.digest,
                "extraction": row.status,
            }
    except DocketError as exc:
        raise HTTPException(422, exc.code) from None
