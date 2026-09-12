from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Integer, String, event
from sqlalchemy.orm import Mapped, mapped_column, relationship

from docket.models.authority import ChangeSetRevision
from docket.models.base import Base, utc_now


class SemanticRequestSpecification(Base):
    """Immutable proposal version, addressed by its sreq_ and version pair.

    This record alone never proves that its interpretation is authorized.
    A source digest establishes evidence identity, not semantic equivalence.
    """

    __tablename__ = "semantic_request_specifications"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_request_specifications_version"),
        CheckConstraint("schema_version = 1", name="ck_request_specifications_schema"),
        CheckConstraint(
            "interpretation_state = 'pending_evidence_validation'",
            name="ck_request_specifications_interpretation",
        ),
    )

    semantic_request_ref: Mapped[str] = mapped_column(
        ForeignKey("semantic_requests.ref_id", ondelete="RESTRICT"), primary_key=True,
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    change_set_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("change_set_revisions.id", ondelete="RESTRICT"), unique=True, nullable=False,
    )
    change_set_revision: Mapped[ChangeSetRevision] = relationship()
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    interpretation_state: Mapped[str] = mapped_column(String(40), nullable=False)
    specification_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    specification_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False,
    )


class RequestAssemblyAdoption(Base):
    """One immutable proof linking an original direct revision to assembly.

    Internal provenance, addressed through the existing sreq_/chg_ chain; it
    does not replace the original request binding or introduce a public alias.
    """

    __tablename__ = "request_assembly_adoptions"

    semantic_request_ref: Mapped[str] = mapped_column(
        ForeignKey("semantic_requests.ref_id", ondelete="RESTRICT"), primary_key=True,
    )
    change_set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("change_sets.id", ondelete="RESTRICT"), unique=True, nullable=False,
    )
    original_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("change_set_revisions.id", ondelete="RESTRICT"), unique=True, nullable=False,
    )
    adopted_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("change_set_revisions.id", ondelete="RESTRICT"), unique=True, nullable=False,
    )
    proof_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    proof_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False,
    )


def _immutable(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("SemanticRequestSpecification is immutable")


event.listen(SemanticRequestSpecification, "before_update", _immutable)
event.listen(SemanticRequestSpecification, "before_delete", _immutable)


def _immutable_adoption(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("RequestAssemblyAdoption is immutable")


event.listen(RequestAssemblyAdoption, "before_update", _immutable_adoption)
event.listen(RequestAssemblyAdoption, "before_delete", _immutable_adoption)
