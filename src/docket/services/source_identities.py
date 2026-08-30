from __future__ import annotations

from email.utils import parseaddr
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.models import Entity, GmailSource, IdentityHandle, SenderIdentityEmail
from docket.services.entity_resolution import DeterministicIdentityResolutionService
from docket.services.registry import normalize_identity_value


def _gmail_address(sender: object) -> tuple[str | None, str] | None:
    display_name, address = parseaddr(str(sender or ""))
    normalized = normalize_identity_value("email", address)
    if (
        len(normalized) > 320
        or normalized.count("@") != 1
        or any(character.isspace() for character in normalized)
    ):
        return None
    local, domain = normalized.rsplit("@", 1)
    if not local or not domain or "." not in domain:
        return None
    compact_display = " ".join(display_name.split())
    return (compact_display[:512] or None, normalized)


def sender_handles_for_email(
    session: Session,
    email_handle: IdentityHandle,
) -> list[dict[str, Any]]:
    """Return active operator-authored sender indexes for one exact email."""

    if email_handle.handle_type != "email":
        return []
    associations = list(
        session.scalars(
            select(SenderIdentityEmail)
            .where(
                SenderIdentityEmail.email_identity_handle_id == email_handle.id,
                SenderIdentityEmail.status == "active",
            )
            .order_by(SenderIdentityEmail.created_at, SenderIdentityEmail.id)
            .limit(25)
        )
    )
    result: list[dict[str, Any]] = []
    for association in associations:
        sender = session.get(IdentityHandle, association.sender_identity_handle_id)
        if (
            sender is None
            or sender.handle_type != "sender_label"
            or sender.status not in {"unbound", "bound"}
        ):
            continue
        result.append(
            {
                "identity_ref": sender.ref_id,
                "label": sender.value,
                "status": sender.status,
            }
        )
    return result


def associated_sender_emails(
    session: Session,
    sender_handle: IdentityHandle,
    *,
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    """Return the bounded exact-email table for one sender-label handle."""

    if sender_handle.handle_type != "sender_label":
        return []
    statement = select(SenderIdentityEmail).where(
        SenderIdentityEmail.sender_identity_handle_id == sender_handle.id
    )
    if not include_inactive:
        statement = statement.where(SenderIdentityEmail.status == "active")
    statement = statement.order_by(SenderIdentityEmail.created_at, SenderIdentityEmail.id).limit(25)
    result: list[dict[str, Any]] = []
    for association in session.scalars(statement):
        email = session.get(IdentityHandle, association.email_identity_handle_id)
        if email is None or email.handle_type != "email":
            continue
        result.append(
            {
                "identity_ref": email.ref_id,
                "value": email.value,
                "identity_status": email.status,
                "association_status": association.status,
                "valid_from": association.valid_from.isoformat(),
                "valid_to": (
                    association.valid_to.isoformat() if association.valid_to is not None else None
                ),
            }
        )
    return result


def gmail_sender_identity(
    session: Session,
    source: GmailSource,
    *,
    materialize: bool,
) -> dict[str, Any] | None:
    """Project one exact sender address without treating its label as identity."""

    if source.provider != "gmail":
        return None
    parsed = _gmail_address(source.minimal_headers.get("sender"))
    if parsed is None:
        return None
    display_label, address = parsed
    handle = session.scalar(
        select(IdentityHandle).where(
            IdentityHandle.handle_type == "email",
            IdentityHandle.normalized_value == address,
        )
    )
    if handle is None and materialize:
        handle = DeterministicIdentityResolutionService(session).observe_unbound_handle(
            handle_type="email",
            value=address,
            source_refs=[source.ref_id],
        )
    entity_ref = (
        session.scalar(select(Entity.ref_id).where(Entity.id == handle.entity_id))
        if handle is not None and handle.entity_id is not None
        else None
    )
    sender_handles = sender_handles_for_email(session, handle) if handle is not None else []
    return {
        "trust": "untrusted_provider_metadata",
        "source_ref": source.ref_id,
        "identity_ref": handle.ref_id if handle is not None else None,
        "handle_type": "email",
        "value": address,
        "display_label": display_label,
        "binding_state": handle.status if handle is not None else "unmaterialized",
        "entity_ref": entity_ref,
        "sender_handles": sender_handles,
        "basis_refs": [source.ref_id],
    }
