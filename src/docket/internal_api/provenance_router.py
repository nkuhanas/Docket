from __future__ import annotations

from datetime import datetime
from typing import Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status

from docket.database import session_scope
from docket.domain.errors import DocketError
from docket.internal_api.auth import require_hermes_service
from docket.services.history import HistoryService

router = APIRouter(
    prefix="/internal/v1/provenance",
    tags=["trusted-provenance"],
    dependencies=[Depends(require_hermes_service)],
)


def _raise_history_error(exc: DocketError) -> NoReturn:
    error_status = (
        status.HTTP_404_NOT_FOUND
        if exc.code in {"history_entry_not_found", "history_type_not_available"}
        else status.HTTP_422_UNPROCESSABLE_ENTITY
    )
    raise HTTPException(
        status_code=error_status,
        detail=exc.as_dict()["error"],
    ) from exc


@router.get("/history/{ref_id}")
def get_history_entry(
    ref_id: str,
    view: Literal["summary", "audit"] = "summary",
    text_offset: int = Query(default=0, ge=0),
    text_limit: int = Query(default=32_768, ge=1, le=32_768),
) -> dict[str, object]:
    try:
        with session_scope() as session:
            return HistoryService(session).get_entry(
                ref_id,
                view=view,
                text_offset=text_offset,
                text_limit=text_limit,
            )
    except DocketError as exc:
        _raise_history_error(exc)


@router.get("/history")
def search_history(
    object_type: str | None = Query(default=None, max_length=64),
    ref_id: str | None = Query(default=None, max_length=40),
    conversation_ref: str | None = Query(default=None, max_length=512),
    related_ref: str | None = Query(default=None, max_length=40),
    tool_name: str | None = Query(default=None, max_length=128),
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=25, ge=1, le=100),
) -> dict[str, object]:
    try:
        with session_scope() as session:
            return HistoryService(session).search(
                object_type=object_type,
                ref_id=ref_id,
                conversation_ref=conversation_ref,
                related_ref=related_ref,
                tool_name=tool_name,
                occurred_from=occurred_from,
                occurred_to=occurred_to,
                cursor=cursor,
                limit=limit,
            )
    except DocketError as exc:
        _raise_history_error(exc)


@router.get("/conversations")
def reconstruct_conversation(
    conversation_ref: str = Query(min_length=1, max_length=512),
    view: Literal["summary", "audit"] = "summary",
    limit: int = Query(default=100, ge=1, le=100),
) -> dict[str, object]:
    with session_scope() as session:
        return HistoryService(session).conversation(
            conversation_ref,
            view=view,
            limit=limit,
        )
