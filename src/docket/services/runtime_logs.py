from __future__ import annotations

from typing import Any, Literal

from sqlalchemy.orm import Session

from docket.domain.public_refs import is_public_ref
from docket.models import RuntimeLogEntry

RuntimeSeverity = Literal["debug", "info", "warning", "error", "critical"]


class RuntimeLogService:
    """Append retention-safe operational diagnostics outside semantic provenance."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def append(
        self,
        *,
        severity: RuntimeSeverity,
        component: str,
        event_code: str,
        message: str,
        related_refs: list[str] | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> RuntimeLogEntry:
        refs = list(dict.fromkeys(related_refs or []))
        if len(refs) > 100 or any(not is_public_ref(ref) for ref in refs):
            raise ValueError("RuntimeLogEntry related_refs must be bounded public references")
        entry = RuntimeLogEntry(
            severity=severity,
            component=component[:128],
            event_code=event_code[:128],
            message=message[:1000],
            related_refs=refs,
            metadata_json=dict(metadata_json or {}),
        )
        self.session.add(entry)
        self.session.flush()
        return entry
