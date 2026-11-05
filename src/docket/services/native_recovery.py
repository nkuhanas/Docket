"""Owner-scoped saved-work reads; no silent translation or execution of old drafts."""

import json
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import (
    AuthenticatedRequest,
    ChangeSet,
    ChangeSetRevision,
    IntentSession,
    PreparedCommand,
)


class NativeRecoveryService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def owned(self, ref: str) -> AuthenticatedRequest:
        principal = require_principal("read")
        row = self.session.scalar(
            select(AuthenticatedRequest).where(
                AuthenticatedRequest.ref_id == ref,
                AuthenticatedRequest.principal_ref == principal.principal_ref,
                AuthenticatedRequest.operator_ref == principal.operator_ref,
            )
        )
        if row is None:
            raise DocketError(code="saved_work_unavailable", message="Read your own saved request.")
        return row

    def list(self, limit: int, cursor: str | None) -> dict[str, Any]:
        principal = require_principal("read")
        query = select(AuthenticatedRequest).where(
            AuthenticatedRequest.principal_ref == principal.principal_ref,
            AuthenticatedRequest.operator_ref == principal.operator_ref,
            AuthenticatedRequest.state == "active",
        )
        if cursor:
            before = self.owned(cursor)
            query = query.where(
                or_(
                    AuthenticatedRequest.admitted_at < before.admitted_at,
                    and_(
                        AuthenticatedRequest.admitted_at == before.admitted_at,
                        AuthenticatedRequest.ref_id < before.ref_id,
                    ),
                )
            )
        rows = list(
            self.session.scalars(
                query.order_by(
                    AuthenticatedRequest.admitted_at.desc(),
                    AuthenticatedRequest.ref_id.desc(),
                ).limit(limit + 1)
            )
        )
        return {
            "ok": True,
            "requests": [
                {
                    "request_ref": row.ref_id,
                    "state": row.state,
                    "admitted_at": row.admitted_at.isoformat(),
                }
                for row in rows[:limit]
            ],
            "next_cursor": rows[limit - 1].ref_id if len(rows) > limit else None,
            "coverage": "Owned active requests, including rejected or optional-capture work.",
        }

    def read(self, request_ref: str, revision: int | None, offset: int) -> dict[str, Any]:
        root = self.owned(request_ref)
        drafts = list(
            self.session.scalars(
                select(ChangeSet)
                .join(
                    IntentSession,
                    ChangeSet.intent_session_id == IntentSession.id,
                )
                .where(IntentSession.source_request_ref == root.ref_id)
                .order_by(ChangeSet.created_at)
            )
        )
        if len(drafts) > 1:
            raise DocketError(
                code="saved_work_needs_audit",
                message="Use history for these saved drafts.",
                details={"changeset_refs": [draft.ref_id for draft in drafts[:25]]},
            )
        draft = drafts[0] if drafts else None
        proposals = list(
            self.session.scalars(
                select(PreparedCommand)
                .where(
                    PreparedCommand.request_ref == root.ref_id,
                )
                .order_by(PreparedCommand.created_at.desc())
                .limit(25)
            )
        )
        result: dict[str, Any] = {
            "ok": True,
            "request_ref": root.ref_id,
            "state": root.state,
            "proposals": [
                {"change_id": row.ref_id, "revision": row.revision, "command": row.command}
                for row in proposals
            ],
            "coverage": "Exact saved state; old compiler drafts are read-only recovery.",
        }
        if draft is None:
            return result
        selected = revision or draft.current_revision
        saved = self.session.scalar(
            select(ChangeSetRevision).where(
                ChangeSetRevision.change_set_id == draft.id,
                ChangeSetRevision.revision == selected,
            )
        )
        if saved is None:
            raise DocketError(
                code="saved_revision_unavailable", message="Choose a retained revision."
            )
        # Exact JSON text is chunked, rather than silently omitting large actions.
        payload = {
            key: getattr(saved, key)
            for key in (
                "revision",
                "basis_refs",
                "expected_versions",
                "staged_actions_json",
                "normalized_entries_json",
                "registry_changes",
                "preference_changes",
                "lane_changes",
                "event_changes",
                "tracked_context_changes",
                "resolution_changes",
                "provider_intents",
                "validation_errors_json",
            )
            if hasattr(saved, key)
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        end = min(len(encoded), offset + 2048)
        result.update(
            changeset_ref=draft.ref_id,
            revision=selected,
            current_revision=draft.current_revision,
            snapshot_hash=sha256_json(payload),
            saved_json_chunk=encoded[offset:end],
            total_characters=len(encoded),
            next_offset=end if end < len(encoded) else None,
        )
        return result
