"""Approved account bindings. OAuth login alone never enrolls an Operator."""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, utc_now


class OAuthPrincipal(Base):
    __tablename__ = "oauth_principals"
    __table_args__ = (UniqueConstraint("issuer", "subject", name="uq_oauth_principal_owner"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("prn")
    )
    issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    valid_after: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


@event.listens_for(OAuthPrincipal, "before_update")
def _retain_identity(_mapper: object, _connection: object, target: OAuthPrincipal) -> None:
    if any(inspect(target).attrs[key].history.has_changes() for key in (
        "id", "ref_id", "issuer", "subject", "operator_ref", "created_at",
    )):
        raise ValueError("OAuth principal identity is immutable")


@event.listens_for(OAuthPrincipal, "before_delete")
def _retain_principal(_mapper: object, _connection: object, _target: OAuthPrincipal) -> None:
    raise ValueError("OAuth principal is retained; revoke its access instead")
