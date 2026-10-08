import hmac
from collections.abc import AsyncIterator

from fastapi import Header, HTTPException, status

from docket.agent_auth import AgentCallContext, AgentPrincipal, agent_call_context
from docket.config import get_settings
from docket.domain.errors import DocketError


async def require_hermes_service(
    authorization: str | None = Header(default=None),
) -> AsyncIterator[None]:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing internal service authorization",
        )
    supplied = authorization.removeprefix("Bearer ").strip()
    settings = get_settings()
    expected = settings.hermes_to_docket_token()
    paths = (
        settings.docket_to_hermes_token_file,
        settings.docket_triage_token_file,
        settings.docket_read_only_token_file,
    )
    if any(
        path is not None and path.is_file() and settings.read_secret(path) == expected
        for path in paths
    ):
        raise HTTPException(status_code=401, detail="Workload credentials must be distinct")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal service authorization",
        )
    principal = AgentPrincipal(
        principal_ref="agent:interactive",
        role="interactive",
        operator_ref=f"operator:{settings.operator_discord_user_id}",
        permissions=frozenset({"read", "stage", "commit", "resolve_conflict"}),
        enabled=settings.interactive_agent_enabled,
        expires_at=settings.interactive_agent_expires_at,
    )
    try:
        principal.require("read")
    except DocketError as exc:
        raise HTTPException(status_code=403, detail=exc.as_dict()["error"]) from exc
    token = agent_call_context.set(AgentCallContext(principal))
    try:
        yield
    finally:
        agent_call_context.reset(token)
