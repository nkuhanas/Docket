from __future__ import annotations

from typing import Any, Literal

from mcp.server.transport_security import TransportSecuritySettings

from docket.database import get_session_factory
from docket.domain.errors import DocketError
from docket.mcp.instrumented import ProvenanceFastMCP
from docket.providers.google.gmail_runtime import get_gmail_read_provider
from docket.schemas.common import PublicRef
from docket.schemas.intelligence import (
    CaseItemInput,
    CaseRef,
    ContextRef,
    SemanticClass,
    SourceRef,
    TriageAnalysisInput,
    TriageRunRef,
)
from docket.schemas.policy import PreferenceRef
from docket.services.intelligence import IntelligenceService

triage_mcp = ProvenanceFastMCP(
    "docket-triage",
    caller_profile="triage",
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["docket:8000", "127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=[
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ],
    ),
)


def _error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, DocketError):
        return exc.as_dict()
    return {
        "ok": False,
        "error": {
            "code": "internal_error",
            "message": "Docket encountered an internal processing failure.",
            "details": {},
        },
    }


def _service() -> IntelligenceService:
    return IntelligenceService(get_session_factory(), get_gmail_read_provider())


@triage_mcp.tool()
def docket_get_triage_context() -> dict[str, Any]:
    """Claim one source and return bounded trusted context plus untrusted evidence."""
    try:
        return _service().get_triage_context()
    except Exception as exc:
        return _error(exc)


@triage_mcp.tool()
def docket_submit_triage_analysis(
    triage_run_ref: TriageRunRef,
    context_ref: ContextRef,
    source_ref: SourceRef,
    claim_token: str,
    semantic_classes: list[SemanticClass],
    title: str,
    summary: str,
    explanation: str,
    priority: Literal["low", "normal", "high", "urgent"] = "normal",
    entity_candidate_refs: list[PublicRef] | None = None,
    case_items: list[CaseItemInput] | None = None,
) -> dict[str, Any]:
    """Compile non-authoritative intelligence; every new case item declares its role."""
    try:
        request = TriageAnalysisInput.model_validate(
            {
                "triage_run_ref": triage_run_ref,
                "context_ref": context_ref,
                "source_ref": source_ref,
                "claim_token": claim_token,
                "semantic_classes": semantic_classes,
                "title": title,
                "summary": summary,
                "priority": priority,
                "entity_candidate_refs": entity_candidate_refs or [],
                "case_items": case_items or [],
                "explanation": explanation,
            }
        )
        return _service().submit_analysis(request)
    except Exception as exc:
        return _error(exc)


@triage_mcp.tool()
def docket_get_attention_case(case_ref: CaseRef) -> dict[str, Any]:
    """Read one bounded durable AttentionCase without mutation authority."""
    try:
        return _service().get_case(case_ref)
    except Exception as exc:
        return _error(exc)


@triage_mcp.tool()
def docket_apply_existing_suppression(
    triage_run_ref: TriageRunRef,
    context_ref: ContextRef,
    source_ref: SourceRef,
    claim_token: str,
    preference_ref: PreferenceRef,
    semantic_classes: list[SemanticClass],
) -> dict[str, Any]:
    """Apply one already-active matching Preference; never create or modify policy."""
    try:
        return _service().apply_existing_suppression(
            triage_run_ref=triage_run_ref,
            context_ref=context_ref,
            source_ref=source_ref,
            claim_token=claim_token,
            preference_ref=preference_ref,
            semantic_classes=semantic_classes,
        )
    except Exception as exc:
        return _error(exc)
