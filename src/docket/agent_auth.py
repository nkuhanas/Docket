"""Server-established workload identity, separate from conversation evidence."""

from __future__ import annotations

import hmac
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from docket.config import Settings, get_settings
from docket.domain.errors import DocketError

AgentRole = Literal["interactive", "triage", "read_only"]
_PERMISSIONS = {
    "interactive": frozenset({"read", "stage", "commit", "resolve_conflict"}),
    "triage": frozenset({"read", "triage"}),
    "read_only": frozenset({"read"}),
}


@dataclass(frozen=True)
class AgentPrincipal:
    principal_ref: str
    operator_ref: str
    role: AgentRole
    permissions: frozenset[str]
    audience: str = "docket"
    expires_at: datetime | None = None
    enabled: bool = True

    def require(self, permission: str) -> None:
        expiry = self.expires_at
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        if (
            not self.enabled
            or self.audience != "docket"
            or self.operator_ref != f"operator:{get_settings().operator_discord_user_id}"
            or (expiry is not None and expiry <= datetime.now(UTC))
            or permission not in self.permissions
            or (
                permission in {"stage", "commit", "resolve_conflict"} and self.role != "interactive"
            )
        ):
            raise DocketError(
                code="agent_authority_denied",
                message="The authenticated caller lacks current permission.",
            )


@dataclass(frozen=True)
class AgentCallContext:
    principal: AgentPrincipal
    request_key: str | None = None
    execution_key: str | None = None
    operation_key: str | None = None


agent_call_context: ContextVar[AgentCallContext | None] = ContextVar(
    "docket_agent_call_context",
    default=None,
)


def require_principal(permission: str) -> AgentPrincipal:
    context = agent_call_context.get()
    if context is None:
        raise DocketError(
            code="agent_authentication_required",
            message="A server-authenticated caller is required.",
        )
    context.principal.require(permission)
    return context.principal


def authenticate_agent(
    supplied: str,
    *,
    role: AgentRole,
    settings: Settings,
) -> AgentPrincipal:
    """Credentials choose roles; tool arguments and message contents never do."""
    files = {
        "interactive": settings.docket_to_hermes_token_file,
        "triage": settings.docket_triage_token_file,
        "read_only": settings.docket_read_only_token_file,
    }
    expected_file = files[role]
    if expected_file is None:
        raise DocketError(
            code="agent_authentication_required", message="This caller role is not configured."
        )
    try:
        expected = settings.read_secret(expected_file)
        # Sharing a credential would collapse the foreground/background boundary.
        peers = [
            settings.read_secret(path)
            for other, path in files.items()
            if other != role and path is not None and path.is_file()
        ]
    except (OSError, ValueError) as exc:
        raise DocketError(
            code="agent_authentication_required", message="Caller authentication is unavailable."
        ) from exc
    if (
        not supplied
        or not expected
        or expected in peers
        or not hmac.compare_digest(
            supplied,
            expected,
        )
    ):
        raise DocketError(
            code="agent_authentication_required", message="Caller authentication failed."
        )
    principal = AgentPrincipal(
        principal_ref=f"agent:{role}",
        operator_ref=f"operator:{settings.operator_discord_user_id}",
        role=role,
        permissions=_PERMISSIONS[role],
        enabled=settings.interactive_agent_enabled if role == "interactive" else True,
        expires_at=settings.interactive_agent_expires_at if role == "interactive" else None,
    )
    principal.require("read")
    return principal
