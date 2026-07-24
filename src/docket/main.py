import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import structlog
import uvicorn
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from starlette.requests import Request

from docket.config import Settings, get_settings
from docket.database import (
    configure_database,
    create_schema_for_smoke,
    get_session_factory,
    session_scope,
)
from docket.internal_api import router as internal_router
from docket.mcp import mcp
from docket.models import CalendarSyncState, ReminderRule
from docket.providers.discord import HttpDiscordProjectionAdapter
from docket.providers.google import FakeGoogleProvider
from docket.providers.google.factory import (
    build_calendar_read_provider,
    build_calendar_write_provider,
)
from docket.providers.google.runtime import configure_calendar_read_provider
from docket.services.accounts import AccountService
from docket.services.calendar_sync import CalendarSyncService
from docket.services.discord_projection import DiscordProjectionRunner
from docket.services.operations import OperationRunner
from docket.services.reminders import ReminderDispatcher
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
    OperationRunner(get_session_factory(), calendar_write_provider),
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
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    if settings.auto_create_schema:
        create_schema_for_smoke()
    with session_scope() as session:
        AccountService(session).ensure_configured_google(settings)
    await worker.start()
    async with mcp.session_manager.run():
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


@app.middleware("http")
async def protect_mcp(request: Request, call_next: Any) -> Any:
    if request.url.path.startswith("/mcp"):
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
        enabled_legacy_rule_count = int(
            session.scalar(
                select(func.count())
                .select_from(ReminderRule)
                .where(
                    ReminderRule.source_kind == "legacy_explicit",
                    ReminderRule.enabled.is_(True),
                )
            )
            or 0
        )
    google_oauth = settings.google_oauth_status()
    if (
        settings.external_writes_enabled or settings.calendar_reads_enabled
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
    legacy_rule_gate_blocked = (
        settings.environment.value == "production" and enabled_legacy_rule_count > 0
    )
    if legacy_rule_gate_blocked:
        response.status_code = 503
    return {
        "status": ("degraded" if calendar_degraded or legacy_rule_gate_blocked else "ok"),
        "database": "ready",
        "worker": "ready" if worker.is_healthy() else "starting",
        "credential_mode": settings.credential_mode(),
        "google_oauth": google_oauth,
        "calendar_reads_enabled": settings.calendar_reads_enabled,
        "external_writes_enabled": settings.external_writes_enabled,
        "enabled_legacy_reminder_rules": enabled_legacy_rule_count,
        "legacy_reminder_gate": ("blocked" if legacy_rule_gate_blocked else "clear"),
        "calendar_sync": sync_detail,
    }


@app.get("/health/smoke-provider")
def health_smoke_provider() -> dict[str, Any]:
    if settings.external_writes_enabled:
        return {"status": "disabled", "reason": "fake provider unavailable with external writes"}
    return {"status": "ok", **FakeGoogleProvider().smoke_status()}


def run() -> None:
    uvicorn.run("docket.main:app", host="0.0.0.0", port=8000, reload=False)
