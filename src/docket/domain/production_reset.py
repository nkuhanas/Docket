from __future__ import annotations

from dataclasses import dataclass

TRACKED_CONTEXT_DOCUMENT_REF = "ONT-DELTA-2026-08-29-TRACKED-CONTEXT"
TRACKED_CONTEXT_FROZEN_ARTIFACT_HASH = (
    "830c33c9d78485a6a6a8f872b6dfad996869f8a7eaea9a5f7d39d52e9357cf48"
)


@dataclass(frozen=True, slots=True)
class ProductionResetAuthorityBinding:
    document_ref: str
    frozen_artifact_hash: str
    reset_manifest_sha256: str
    verified_backup_ref: str
    verified_backup_sha256: str
    deployment_revision: str


def production_reset_authorization_text(binding: ProductionResetAuthorityBinding) -> str:
    return (
        f"I authorize execution of the production reset for `{binding.document_ref}` "
        f"frozen at SHA-256 `{binding.frozen_artifact_hash}`, bound to reset manifest "
        f"SHA-256 `{binding.reset_manifest_sha256}`, verified backup artifact "
        f"`{binding.verified_backup_ref}` at SHA-256 "
        f"`{binding.verified_backup_sha256}`, and deployment revision "
        f"`{binding.deployment_revision}`."
    )
