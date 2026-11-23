"""Versioned non-secret identities and immutable financial activity observations."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, utc_now


class FundingIdentity(Base):
    __tablename__ = "funding_identities"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(String(40), unique=True)
    operator_ref: Mapped[str] = mapped_column(String(255), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class FundingRevision(Base):
    __tablename__ = "funding_revisions"
    __table_args__ = (UniqueConstraint("identity_ref", "version"),)
    ref_id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_public_ref("furev")
    )
    identity_ref: Mapped[str] = mapped_column(ForeignKey("funding_identities.ref_id"))
    version: Mapped[int]
    fields: Mapped[dict[str, Any]] = mapped_column(JSON)
    changeset_ref: Mapped[str] = mapped_column(ForeignKey("change_sets.ref_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AccountActivity(Base):
    __tablename__ = "funding_activity"
    __table_args__ = (UniqueConstraint("account_ref", "row_key"),)
    ref_id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_public_ref("actv")
    )
    operator_ref: Mapped[str] = mapped_column(String(255), index=True)
    account_ref: Mapped[str] = mapped_column(ForeignKey("funding_identities.ref_id"))
    account_revision_ref: Mapped[str] = mapped_column(ForeignKey("funding_revisions.ref_id"))
    instrument_revision_ref: Mapped[str | None] = mapped_column(
        ForeignKey("funding_revisions.ref_id")
    )
    row_key: Mapped[str] = mapped_column(String(64))
    fields: Mapped[dict[str, Any]] = mapped_column(JSON)
    changeset_ref: Mapped[str] = mapped_column(ForeignKey("change_sets.ref_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class StatementImport(Base):
    __tablename__ = "funding_statements"
    __table_args__ = (UniqueConstraint("account_ref", "source_ref"),)
    ref_id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_public_ref("fstmt")
    )
    account_ref: Mapped[str] = mapped_column(ForeignKey("funding_identities.ref_id"))
    source_ref: Mapped[str] = mapped_column(ForeignKey("sources.ref_id"))
    changeset_ref: Mapped[str] = mapped_column(ForeignKey("change_sets.ref_id"))
    basis: Mapped[dict[str, Any]] = mapped_column(JSON)
    rows: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
