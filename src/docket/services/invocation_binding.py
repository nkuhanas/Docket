"""Authenticate transport correlation without granting semantic authority.

The trusted gateway signs a short-lived, argument-bound envelope. No evidence
or canonical effect is authorized by this envelope; normal service checks still
apply. Matching arguments or nearby timestamps are never correlation evidence.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.models import ConversationalToolTrace, OperatorUtterance, ToolInvocation
from docket.models.base import utc_now
from docket.services.gateway_lifetimes import GatewayLifetimeService
from docket.tool_contracts import CONTRACT_VERSION, contract_hash

BINDING_ARGUMENT = "invocation_binding"
SIGNING_CONTEXT = b"docket-mcp-invocation-v1:"


class InvocationBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    format: Literal[1]
    trace_ref: str = Field(pattern=r"^trace_[0-9A-HJKMNP-TV-Z]{26}$")
    call_id: str = Field(min_length=1, max_length=255)
    ordinal: int = Field(ge=1, le=100)
    utterance_ref: str = Field(pattern=r"^utt_[0-9A-HJKMNP-TV-Z]{26}$")
    gateway_instance_ref: str | None = Field(pattern=r"^gwy_[0-9A-HJKMNP-TV-Z]{26}$")
    tool_name: str = Field(min_length=1, max_length=128)
    argument_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_version: str = Field(min_length=1, max_length=128)
    contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: int
    expires_at: int


def _invalid() -> DocketError:
    return DocketError(
        code="invalid_invocation_binding",
        message="The trusted invocation binding is invalid; resume the authenticated request.",
        details={"next_action": "resume_authenticated_request", "canonical_effects": "none"},
    )


def bind_invocation(
    session: Session, invocation: ToolInvocation, token: Any, *, arguments: dict[str, Any]
) -> None:
    """Called only after the authenticated MCP request has its durable call_."""
    if not isinstance(token, str) or not 1 <= len(token) <= 4096:
        raise _invalid()
    try:
        encoded, signature = token.split(".")
        expected = hmac.new(
            get_settings().hermes_to_docket_token().encode(),
            SIGNING_CONTEXT + encoded.encode("ascii"), hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise _invalid()
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if not isinstance(payload, dict) or type(payload.get("format")) is not int:
            raise _invalid()
        binding = InvocationBinding.model_validate(payload)
    except (ValueError, TypeError, ValidationError) as exc:
        raise _invalid() from exc
    now = int(utc_now().timestamp())
    if (
        invocation.caller_profile != "interactive"
        or binding.tool_name != invocation.tool_name
        or binding.argument_hash != invocation.received_argument_hash
        or binding.contract_version != CONTRACT_VERSION
        or binding.contract_hash != contract_hash("interactive")
        or not binding.issued_at <= now <= binding.expires_at
        or not 0 < binding.expires_at - binding.issued_at <= 900
    ):
        raise _invalid()
    utterance = session.scalar(select(OperatorUtterance).where(
        OperatorUtterance.ref_id == binding.utterance_ref
    ).with_for_update())
    if utterance is None or utterance.actor_ref != (
        f"discord_user:{get_settings().operator_discord_user_id}"
    ) or utterance.transport != "discord":
        raise _invalid()
    for name in ("utterance_ref", "operator_utterance_ref"):
        if arguments.get(name) is not None and arguments[name] != utterance.ref_id:
            raise _invalid()
    if binding.gateway_instance_ref is not None:
        GatewayLifetimeService(session).require_live(binding.gateway_instance_ref)
    trace = session.scalar(select(ConversationalToolTrace).where(
        ConversationalToolTrace.ref_id == binding.trace_ref
    ))
    if trace is not None and (
        utterance.source_message_ref != (
            f"discord_message:{trace.guild_id}:{trace.source_channel_id}:{trace.source_message_id}"
        )
        or trace.gateway_instance_ref != binding.gateway_instance_ref
    ):
        raise _invalid()
    # The callback may arrive after MCP. Correlation is still exact and survives
    # lost tool responses; it never depends on finding a recent same-hash call.
    existing = session.scalar(select(ToolInvocation).where(
        ToolInvocation.trace_ref == binding.trace_ref,
        ToolInvocation.trace_call_id == binding.call_id,
    ))
    if existing is not None and (
        existing.tool_name != invocation.tool_name
        or existing.received_argument_hash != invocation.received_argument_hash
        or existing.trace_ordinal != binding.ordinal
        or existing.utterance_refs != [utterance.ref_id]
        or existing.gateway_instance_ref != binding.gateway_instance_ref
    ):
        raise _invalid()
    invocation.trace_ref = binding.trace_ref
    # The original upstream call owns its unique call_id. A transport retry is
    # another authenticated invocation under that exact signed ordinal and
    # original binding. Leave deduplication/replay of effects to the durable
    # assembly/ChangeSet service; correlation must not block receipt recovery.
    invocation.trace_call_id = binding.call_id if existing is None else None
    invocation.trace_ordinal = binding.ordinal
    invocation.gateway_instance_ref = binding.gateway_instance_ref
    invocation.actor_ref = utterance.actor_ref
    invocation.utterance_refs = [utterance.ref_id]
