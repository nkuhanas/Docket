"""An allowlisted public ASGI boundary, mounted privately under /plugin."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.orm import Session, sessionmaker
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from docket.agent_auth import AgentCallContext, agent_call_context
from docket.config import Settings
from docket.database import get_session_factory
from docket.domain.errors import DocketError
from docket.plugin.server import PluginMCP
from docket.providers.webhooks import WebhookPoster
from docket.services.oauth import OAuthVerifier


class PluginAuthentication(BaseHTTPMiddleware):
    def __init__(self, app: Any, *, settings: Settings, verifier: OAuthVerifier,
                 factory: sessionmaker[Session] | None = None) -> None:
        super().__init__(app)
        self.settings, self.verifier, self.factory = settings, verifier, factory

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path.removeprefix(request.scope.get("root_path", ""))
        if not self.settings.plugin_enabled or path not in (
            "/", "/mcp", "/mcp/", "/.well-known/oauth-protected-resource",
        ):
            return JSONResponse({"error": "not_found"}, status_code=404)
        if request.method in ("GET", "HEAD") and path in (
            "/", "/.well-known/oauth-protected-resource",
        ):
            return await call_next(request)
        authorization = request.headers.get("authorization", "")
        try:
            if not authorization.startswith("Bearer "):
                raise DocketError(code="plugin_authentication_required", message="Link Docket.")
            principal = await asyncio.to_thread(
                self.verifier.authenticate, authorization[7:],
                self.factory or get_session_factory(),
            )
        except DocketError:
            metadata = self.settings.plugin_public_origin.rstrip("/")
            return JSONResponse(
                {"error": "plugin_authentication_required"}, status_code=401,
                headers={"WWW-Authenticate": (
                    f'Bearer resource_metadata="{metadata}/.well-known/oauth-protected-resource", '
                    f'scope="{self.settings.plugin_read_scope}", error="invalid_token", '
                    'error_description="Link an approved Docket account"'
                ), "Cache-Control": "no-store"},
            )
        # Model-supplied metadata and legacy headers are not foreground authority.
        token = agent_call_context.set(AgentCallContext(principal))
        try:
            return await call_next(request)
        finally:
            agent_call_context.reset(token)


def create_plugin_app(settings: Settings, *, verifier: OAuthVerifier | None = None,
                      factory: sessionmaker[Session] | None = None,
                      poster: WebhookPoster | None = None) -> Starlette:
    verifier = verifier or OAuthVerifier(settings)
    server = PluginMCP(settings, poster)
    transport = server.streamable_http_app(max_request_body_size=131072)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            async with server.session_manager.run():
                yield
        finally:
            verifier.client.close()

    async def landing(_request: Request) -> JSONResponse:
        return JSONResponse({"service": "Docket", "connection": "read and notifications"})

    async def resource(_request: Request) -> JSONResponse:
        return JSONResponse({
            "resource": settings.plugin_public_origin.rstrip("/"),
            "authorization_servers": [settings.plugin_oauth_issuer],
            "scopes_supported": [settings.plugin_read_scope, settings.plugin_events_scope],
            "bearer_methods_supported": ["header"],
        })

    app = Starlette(
        routes=[Route("/", landing), Route("/.well-known/oauth-protected-resource", resource),
                Mount("/mcp", transport)],
        middleware=[Middleware(PluginAuthentication, settings=settings, verifier=verifier,
                               factory=factory)],
        lifespan=lifespan,
    )
    app.state.mcp = server
    return app
