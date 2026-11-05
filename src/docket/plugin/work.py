"""Opaque client workflow identity; never proof of authority or human presence."""

import hashlib

from pydantic import Field

from docket.agent_auth import AgentCallContext, AgentPrincipal
from docket.schemas.common import StrictModel


class NativeWork(StrictModel):
    request_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$",
        description="Opaque identity for one delegated task; reuse for its repairs and retries.",
    )
    execution_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$",
        description="One agent run; reuse across stage/review/commit. A new run reviews first.",
    )
    operation_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$",
        description="One tool operation; retry with the same id and unchanged arguments.",
    )

    def bind(self, principal: AgentPrincipal) -> AgentCallContext:
        def identity(value: str) -> str:
            return "native:" + hashlib.sha256(value.encode()).hexdigest()

        return AgentCallContext(
            principal, identity(self.request_id),
            identity(self.execution_id), identity(self.operation_id),
        )
