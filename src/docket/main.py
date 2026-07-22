import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
import uvicorn
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text
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
from docket.providers.discord import HttpDiscordProjectionAdapter
from docket.providers.google import FakeCalendarProvider, FakeGoogleProvider
from docket.providers.google.calendar import GoogleCalendarProvider
from docket.services.accounts import AccountService
from docket.services.discord_projection import DiscordProjectionRunner
from docket.services.operations import OperationRunner
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
calendar_provider = (
    GoogleCalendarProvider(str(settings.google_oauth_token_file))
    if settings.external_calls_enabled
    else FakeCalendarProvider()
)
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
    OperationRunner(get_session_factory(), calendar_provider),
    operation_poll_seconds=settings.operation_poll_seconds,
    reconciliation_poll_seconds=settings.reconciliation_poll_seconds,
    stale_lease_poll_seconds=settings.stale_lease_poll_seconds,
    discord_projection_runner=discord_projection_runner,
    discord_projection_poll_seconds=settings.discord_projection_poll_seconds,
    rollover_service=RolloverService(get_session_factory(), settings),
    rollover_poll_seconds=settings.daily_rollover_poll_seconds,
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
    google_oauth = settings.google_oauth_status()
    if settings.external_calls_enabled and google_oauth != "configured":
        response.status_code = 503
    return {
        "status": "ok",
        "database": "ready",
        "worker": "ready" if worker.is_healthy() else "starting",
        "credential_mode": settings.credential_mode(),
        "google_oauth": google_oauth,
        "external_calls_enabled": settings.external_calls_enabled,
    }


@app.get("/health/smoke-provider")
def health_smoke_provider() -> dict[str, Any]:
    if settings.external_calls_enabled:
        return {"status": "disabled", "reason": "fake provider unavailable with external calls"}
    return {"status": "ok", **FakeGoogleProvider().smoke_status()}


def run() -> None:
    uvicorn.run("docket.main:app", host="0.0.0.0", port=8000, reload=False)
