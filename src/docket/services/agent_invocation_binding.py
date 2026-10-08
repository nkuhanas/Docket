"""Optional Hermes correlation; caller credentials remain the authority boundary."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from docket.agent_auth import AgentCallContext, require_principal
from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.models.base import utc_now

SIGNING_CONTEXT = b"docket-mcp-request-v3:"


class AgentInvocationBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    format: Literal[3]
    request_key: str = Field(min_length=1, max_length=512)
    execution_key: str = Field(min_length=1, max_length=255)
    operation_key: str = Field(min_length=1, max_length=255)
    tool_name: str = Field(min_length=1, max_length=128)
    argument_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: int
    expires_at: int


def request_context(token: Any, *, tool_name: str, argument_hash: str) -> AgentCallContext:
    principal = require_principal("read")
    try:
        if not isinstance(token, str) or not 1 <= len(token) <= 4096:
            raise ValueError("binding size")
        encoded, signature = token.split(".")
        expected = hmac.new(
            get_settings().hermes_to_docket_token().encode(),
            SIGNING_CONTEXT + encoded.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError("binding signature")
        binding = AgentInvocationBinding.model_validate(
            json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        )
        now = int(utc_now().timestamp())
        if (
            principal.role != "interactive"
            or binding.tool_name != tool_name
            or binding.argument_hash != argument_hash
            or not binding.issued_at <= now <= binding.expires_at
            or not 0 < binding.expires_at - binding.issued_at <= 900
        ):
            raise ValueError("binding mismatch")
    except (ValueError, TypeError, ValidationError, UnicodeError) as exc:
        raise DocketError(
            code="invalid_invocation_binding",
            message="The request correlation is invalid; resume the authenticated request.",
        ) from exc
    return AgentCallContext(
        principal,
        binding.request_key,
        binding.execution_key,
        binding.operation_key,
    )
