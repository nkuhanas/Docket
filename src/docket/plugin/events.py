"""OpenAI's webhook Events extension on the modern, authenticated MCP transport."""

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.extension import Extension, MethodBinding
from mcp.shared.exceptions import MCPError
from mcp.types import RequestParams
from pydantic import ConfigDict, Field, SecretStr

from docket.agent_auth import require_principal
from docket.config import Settings
from docket.database import get_session_factory
from docket.domain.errors import DocketError
from docket.providers.webhooks import WebhookPoster
from docket.schemas.common import PublicRef, StrictModel
from docket.services.event_subscriptions import EventSubscriptionService
from docket.services.notifications import EVENTS


class EventArguments(StrictModel):
    subject_ref: PublicRef | None = None


class Callback(StrictModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    mode: Literal["webhook"]
    url: str = Field(min_length=1, max_length=2048)


class SignedCallback(Callback):
    secret: SecretStr = Field(min_length=38, max_length=94)


class EventParams(RequestParams):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: str = Field(min_length=1, max_length=64)
    arguments: EventArguments = Field(default_factory=EventArguments)
    delivery: Callback


class SubscribeParams(EventParams):
    delivery: SignedCallback
    cursor: str | None = Field(default=None, max_length=256)
    ttl_ms: int | None = Field(default=None, alias="ttlMs", ge=0)


async def advertise_events(
    ctx: ServerRequestContext[Any, Any],
    call_next: CallNext,
) -> HandlerResult:
    result = await call_next(ctx)
    if ctx.method == "server/discover" and isinstance(result, Mapping):
        result = dict(result)
        result["capabilities"] = {**result.get("capabilities", {}), "events": {}}
    return result


class EventMethods(Extension):
    identifier = "you.docket/events"

    def __init__(self, settings: Settings, poster: WebhookPoster | None = None) -> None:
        self.config, self.poster = settings, poster

    def methods(self) -> Sequence[MethodBinding]:
        return (
            MethodBinding(
                "events/list", RequestParams, self.list, protocol_versions=frozenset({"2026-07-28"})
            ),
            MethodBinding(
                "events/subscribe",
                SubscribeParams,
                self.subscribe,
                protocol_versions=frozenset({"2026-07-28"}),
            ),
            MethodBinding(
                "events/unsubscribe",
                EventParams,
                self.unsubscribe,
                protocol_versions=frozenset({"2026-07-28"}),
            ),
        )

    async def list(self, _ctx: ServerRequestContext[Any, Any], _params: Any) -> dict[str, Any]:
        require_principal("read")
        return {
            "events": [
                {
                    "name": name,
                    "description": description,
                    "delivery": ["webhook"],
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "subject_ref": {
                                "type": "string",
                                "description": "Optional exact Docket subject.",
                            },
                        },
                        "additionalProperties": False,
                    },
                    "payloadSchema": {
                        "type": "object",
                        "properties": {
                            "notification_ref": {"type": "string"},
                            "subject_ref": {"type": "string"},
                            "summary": {"type": "string", "maxLength": 512},
                        },
                        "required": ["notification_ref", "subject_ref", "summary"],
                        "additionalProperties": False,
                    },
                }
                for name, description in EVENTS.items()
            ]
        }

    async def subscribe(self, _ctx: ServerRequestContext[Any, Any], params: Any) -> dict[str, Any]:
        assert isinstance(params, SubscribeParams)
        service = EventSubscriptionService(get_session_factory(), self.config, self.poster)
        try:
            return await asyncio.to_thread(
                service.subscribe,
                name=params.name,
                arguments=params.arguments.model_dump(exclude_none=True),
                url=params.delivery.url,
                secret=params.delivery.secret.get_secret_value(),
                ttl_ms=params.ttl_ms,
                cursor=params.cursor,
            )
        except DocketError as exc:
            callback = exc.code == "callback_verification_failed"
            raise MCPError(
                code=-32015 if callback else -32602,
                message="Callback verification failed" if callback else "Subscription rejected",
                data={"reason": "challenge_failed" if callback else exc.code},
            ) from None

    async def unsubscribe(
        self,
        _ctx: ServerRequestContext[Any, Any],
        params: Any,
    ) -> dict[str, Any]:
        assert isinstance(params, EventParams)
        service = EventSubscriptionService(get_session_factory(), self.config, self.poster)
        try:
            return await asyncio.to_thread(
                service.unsubscribe,
                name=params.name,
                arguments=params.arguments.model_dump(exclude_none=True),
                url=params.delivery.url,
            )
        except DocketError as exc:
            raise MCPError(
                code=-32602, message="Subscription rejected", data={"reason": exc.code}
            ) from None
