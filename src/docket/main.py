import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import structlog
import uvicorn
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from starlette.requests import Request

from docket.config import Settings, get_settings
from docket.database import (
    configure_database,
    create_schema_for_smoke,
    get_session_factory,
    session_scope,
)
from docket.internal_api import router as internal_router
from docket.mcp import mcp, triage_mcp
from docket.models import BackupRun, CalendarSyncState, ConnectorCheckpoint
from docket.providers.discord import HttpDiscordProjectionAdapter
from docket.providers.google import FakeGoogleProvider
from docket.providers.google.factory import (
    build_calendar_read_provider,
    build_calendar_write_provider,
    build_gmail_mutation_provider,
    build_gmail_read_provider,
)
from docket.providers.google.gmail_runtime import configure_gmail_read_provider
from docket.providers.google.runtime import configure_calendar_read_provider
from docket.services.accounts import AccountService
from docket.services.backups import BackupService
from docket.services.briefs import DailyBriefService
from docket.services.calendar_sync import CalendarSyncService
from docket.services.discord_projection import DiscordProjectionRunner
from docket.services.events import SemanticCandidateCompiler
from docket.services.gmail_ingestion import GmailIngestionService
from docket.services.operations import OperationRunner
from docket.services.reminders import ReminderDispatcher
from docket.services.retention import RetentionService
from docket.services.rollover import RolloverService
from docket.worker import WorkerRuntime


def configure_logging(settings: Settings) -> None:
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(settings.log_level),
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
    )


settings = get_settings()
configure_logging(settings)
configure_database(settings.database_url)
calendar_write_provider = build_calendar_write_provider(settings)
calendar_read_provider = build_calendar_read_provider(settings)
configure_calendar_read_provider(calendar_read_provider)
calendar_sync_service = CalendarSyncService(get_session_factory(), calendar_read_provider, settings)
gmail_read_provider = build_gmail_read_provider(settings)
gmail_mutation_provider = build_gmail_mutation_provider(settings)
if gmail_read_provider is not None:
    configure_gmail_read_provider(gmail_read_provider)
discord_projection_runner = (
    DiscordProjectionRunner(
        get_session_factory(),
        HttpDiscordProjectionAdapter(
            settings.discord_projection_url, settings.docket_to_hermes_token()
        ),
        settings,
        lease_seconds=settings.discord_projection_lease_seconds,
    )
    if settings.discord_projection_enabled
    else None
)
worker = WorkerRuntime(
    settings.worker_heartbeat_seconds,
    OperationRunner(
        get_session_factory(),
        calendar_write_provider,
        gmail_provider=gmail_mutation_provider,
        execution_enabled=settings.calendar_write_mode() != "disabled",
        gmail_execution_enabled=settings.gmail_writes_enabled,
    ),
    operation_poll_seconds=settings.operation_poll_seconds,
    reconciliation_poll_seconds=settings.reconciliation_poll_seconds,
    stale_lease_poll_seconds=settings.stale_lease_poll_seconds,
    discord_projection_runner=discord_projection_runner,
    discord_projection_poll_seconds=settings.discord_projection_poll_seconds,
    rollover_service=RolloverService(get_session_factory(), settings),
    rollover_poll_seconds=settings.daily_rollover_poll_seconds,
    calendar_sync_service=(calendar_sync_service if settings.calendar_reads_enabled else None),
    calendar_sync_poll_seconds=settings.calendar_sync_poll_seconds,
    reminder_dispatcher=ReminderDispatcher(get_session_factory(), settings),
    reminder_dispatch_poll_seconds=settings.reminder_dispatch_interval_seconds,
    backup_service=(
        BackupService(get_session_factory(), settings) if settings.backup_enabled else None
    ),
    backup_poll_seconds=settings.backup_poll_seconds,
    gmail_ingestion_service=(
        GmailIngestionService(
            get_session_factory(),
            gmail_read_provider,
            settings,
        )
        if gmail_read_provider is not None
        else None
    ),
    gmail_scan_poll_seconds=settings.gmail_scan_poll_seconds,
    semantic_candidate_compiler=(
        SemanticCandidateCompiler(get_session_factory(), settings)
        if settings.gmail_ingestion_enabled
        else None
    ),
    semantic_candidate_poll_seconds=settings.semantic_candidate_poll_seconds,
    daily_brief_service=(
        DailyBriefService(get_session_factory(), settings)
        if settings.gmail_ingestion_enabled
        else None
    ),
    daily_brief_poll_seconds=settings.daily_brief_poll_seconds,
    retention_service=(
        RetentionService(get_session_factory(), settings) if settings.retention_enabled else None
    ),
    retention_poll_seconds=settings.retention_poll_seconds,
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    if settings.auto_create_schema:
        create_schema_for_smoke()
    with session_scope() as session:
        AccountService(session).ensure_configured_google(settings)
    await worker.start()
    async with mcp.session_manager.run(), triage_mcp.session_manager.run():
        yield
    await worker.stop()


app = FastAPI(
    title="Docket",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.state.wake_discord_projection = worker.wake_discord_projection
app.include_router(internal_router)
app.mount("/mcp", mcp.streamable_http_app())
app.mount("/triage-mcp", triage_mcp.streamable_http_app())


@app.middleware("http")
async def protect_mcp(request: Request, call_next: Any) -> Any:
    if request.url.path.startswith(("/mcp", "/triage-mcp")):
        authorization = request.headers.get("authorization", "")
        supplied = authorization.removeprefix("Bearer ").strip()
        if not authorization.startswith("Bearer ") or not hmac.compare_digest(
            supplied, settings.docket_to_hermes_token()
        ):
            return JSONResponse(
                status_code=401,
                content={"error": {"code": "unauthorized", "message": "Invalid MCP token"}},
            )
    return await call_next(request)


@app.get("/health/live")
def health_live() -> dict[str, Any]:
    return {"status": "ok" if worker.is_healthy() else "starting", "worker": worker.is_healthy()}


@app.get("/health/ready")
def health_ready(response: Response) -> dict[str, Any]:
    with session_scope() as session:
        session.execute(text("SELECT 1"))
        sync_states = session.scalars(
            select(CalendarSyncState).order_by(
                CalendarSyncState.account_id, CalendarSyncState.calendar_id
            )
        ).all()
        latest_backup = session.scalar(
            select(BackupRun).order_by(BackupRun.local_date.desc()).limit(1)
        )
        gmail_checkpoints = session.scalars(
            select(ConnectorCheckpoint)
            .where(ConnectorCheckpoint.stream == "gmail:inbox")
            .order_by(ConnectorCheckpoint.account_id)
        ).all()
    google_oauth = settings.google_oauth_status()
    if (
        settings.external_writes_enabled
        or settings.calendar_reads_enabled
        or settings.gmail_ingestion_enabled
    ) and google_oauth != "configured":
        response.status_code = 503
    now = datetime.now(UTC)
    sync_detail = [
        {
            "account_id": str(state.account_id),
            "calendar_id": state.calendar_id,
            "status": state.status,
            "window_start": state.window_start.isoformat(),
            "window_end": state.window_end.isoformat(),
            "last_attempt_at": (
                state.last_attempt_at.isoformat() if state.last_attempt_at else None
            ),
            "last_success_at": (
                state.last_success_at.isoformat() if state.last_success_at else None
            ),
            "stale": (
                state.last_success_at is None
                or (
                    now
                    - (
                        state.last_success_at.replace(tzinfo=UTC)
                        if state.last_success_at.tzinfo is None
                        else state.last_success_at.astimezone(UTC)
                    )
                ).total_seconds()
                > settings.calendar_stale_seconds
                or state.status != "current"
            ),
            "last_error_code": state.last_error_code,
        }
        for state in sync_states
    ]
    calendar_degraded = settings.calendar_reads_enabled and (
        not sync_detail or any(bool(item["stale"]) for item in sync_detail)
    )
    gmail_detail = [
        {
            "account_id": str(checkpoint.account_id),
            "cursor_mode": str(checkpoint.cursor.get("mode") or "recovery"),
            "last_attempt_at": (
                checkpoint.last_attempt_at.isoformat()
                if checkpoint.last_attempt_at is not None
                else None
            ),
            "last_success_at": (
                checkpoint.last_success_at.isoformat()
                if checkpoint.last_success_at is not None
                else None
            ),
            "last_error_code": checkpoint.last_error_code,
            "stale": (
                checkpoint.last_success_at is None
                or (
                    now
                    - (
                        checkpoint.last_success_at.replace(tzinfo=UTC)
                        if checkpoint.last_success_at.tzinfo is None
                        else checkpoint.last_success_at.astimezone(UTC)
                    )
                ).total_seconds()
                > settings.gmail_stale_seconds
            ),
        }
        for checkpoint in gmail_checkpoints
    ]
    gmail_degraded = settings.gmail_ingestion_enabled and (
        not gmail_detail or any(bool(item["stale"]) for item in gmail_detail)
    )
    local_now = now.astimezone(ZoneInfo(settings.timezone))
    backup_degraded = settings.backup_enabled and (
        (latest_backup is not None and latest_backup.status == "failed")
        or (
            local_now.hour >= settings.backup_hour
            and (
                latest_backup is None
                or latest_backup.local_date != local_now.date()
                or latest_backup.status != "succeeded"
            )
        )
    )
    return {
        "status": ("degraded" if calendar_degraded or gmail_degraded or backup_degraded else "ok"),
        "database": "ready",
        "worker": "ready" if worker.is_healthy() else "starting",
        "credential_mode": settings.credential_mode(),
        "google_oauth": google_oauth,
        "calendar_reads_enabled": settings.calendar_reads_enabled,
        "external_writes_enabled": settings.external_writes_enabled,
        "gmail_ingestion_enabled": settings.gmail_ingestion_enabled,
        "gmail_writes_enabled": settings.gmail_writes_enabled,
        "gmail_triage_source_allowlist_count": len(settings.gmail_triage_source_allowlist),
        "gmail_provider_mode": settings.gmail_provider_mode(),
        "calendar_write_mode": settings.calendar_write_mode(),
        "encrypted_backup": {
            "enabled": settings.backup_enabled,
            "status": (
                latest_backup.status
                if latest_backup is not None
                else ("not_due" if settings.backup_enabled else "disabled")
            ),
            "local_date": (
                latest_backup.local_date.isoformat() if latest_backup is not None else None
            ),
            "completed_at": (
                latest_backup.completed_at.isoformat()
                if latest_backup is not None and latest_backup.completed_at is not None
                else None
            ),
            "error_code": (latest_backup.error_code if latest_backup is not None else None),
            "degraded": backup_degraded,
        },
        "calendar_sync": sync_detail,
        "gmail_sync": gmail_detail,
    }


@app.get("/health/smoke-provider")
def health_smoke_provider() -> dict[str, Any]:
    if settings.calendar_write_mode() != "fake":
        return {
            "status": "disabled",
            "reason": "fake provider unavailable in the current Calendar write mode",
        }
    return {"status": "ok", **FakeGoogleProvider().smoke_status()}


def run() -> None:
    uvicorn.run("docket.main:app", host="0.0.0.0", port=8000, reload=False)
