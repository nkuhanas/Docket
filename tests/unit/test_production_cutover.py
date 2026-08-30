from __future__ import annotations

from pathlib import Path

import pytest

from docket.domain.production_reset import production_reset_authorization_text
from docket.production_cutover import (
    RESET_PRESERVED_FAMILIES,
    RESET_REMOVED_TABLES,
    authorization_binding,
    build_reset_manifest,
    validate_reset_manifest,
    verify_manifest_artifacts,
)
from docket.tracked_context_readiness import _sha256, _synthetic_payload


def _provider_manifest() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "document_ref": "ONT-DELTA-2026-08-29-TRACKED-CONTEXT",
        "generated_at": "2026-08-30T01:00:00+00:00",
        "provider_mutation_authorized": False,
        "reset_blockers": {
            "operations": [],
            "operation_items": [],
            "execution_attempts": [],
        },
        "accounts": [],
        "targets": [
            {
                "target_sha256": "d" * 64,
                "target_kind": "google_calendar_event",
                "disposition": "leave_external_unmanaged",
            }
        ],
    }
    payload["manifest_sha256"] = _sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "generated_at"
        }
    )
    return payload


def _rehearsal_evidence(
    closure: dict[str, object], provider: dict[str, object]
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "document_ref": "ONT-DELTA-2026-08-29-TRACKED-CONTEXT",
        "rehearsed_at": "2026-08-30T02:00:00+00:00",
        "closure_sha256": closure["closure_sha256"],
        "provider_manifest_sha256": provider["manifest_sha256"],
        "backup_sha256": "e" * 64,
        "governance": {"verified_signoffs": 4},
        "attachment": {"plaintext_sha256": "f" * 64},
        "production_state_changed": False,
        "provider_mutation_performed": False,
    }
    payload["evidence_sha256"] = _sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "rehearsed_at"
        }
    )
    return payload


def _build(tmp_path: Path) -> tuple[dict[str, object], Path]:
    backup = tmp_path / "tracked-context-pre-reset.dump"
    backup.write_bytes(b"PGDMP\x01verified-fixture")
    matching_restore = tmp_path / "matching-image-restore.txt"
    matching_restore.write_text("2026_08_29_0042 (head)\n72\n4\n", encoding="utf-8")
    closure = _synthetic_payload()
    provider = _provider_manifest()
    rehearsal = _rehearsal_evidence(closure, provider)
    manifest = build_reset_manifest(
        backup_path=backup,
        backup_ref=backup.name,
        deployment_revision="a" * 40,
        matching_application_image="sha256:" + "b" * 64,
        matching_application_revision="c" * 40,
        matching_restore_evidence_path=matching_restore,
        closure_payload=closure,
        provider_manifest=provider,
        rehearsal_evidence=rehearsal,
    )
    return manifest, backup


def test_reset_manifest_binds_exact_backup_revision_and_reset_boundary(
    tmp_path: Path,
) -> None:
    manifest, backup = _build(tmp_path)

    validate_reset_manifest(manifest)
    verify_manifest_artifacts(
        manifest,
        backup_path=backup,
        deployment_revision="a" * 40,
    )
    assert tuple(manifest["removed_tables"]) == RESET_REMOVED_TABLES
    assert tuple(manifest["preserved_families"]) == RESET_PRESERVED_FAMILIES
    assert manifest["provider_target_dispositions"] == [
        {
            "target_sha256": "d" * 64,
            "target_kind": "google_calendar_event",
            "disposition": "leave_external_unmanaged",
        }
    ]
    authorization = production_reset_authorization_text(
        authorization_binding(manifest)
    )
    assert str(manifest["reset_manifest_sha256"]) in authorization
    assert backup.name in authorization
    assert "a" * 40 in authorization


def test_reset_manifest_tampering_and_wrong_artifacts_fail_closed(
    tmp_path: Path,
) -> None:
    manifest, backup = _build(tmp_path)
    manifest["removed_tables"] = list(RESET_REMOVED_TABLES[:-1])

    with pytest.raises(RuntimeError, match="removal boundary"):
        validate_reset_manifest(manifest)

    manifest, backup = _build(tmp_path)
    backup.write_bytes(b"PGDMP\x01changed")
    with pytest.raises(RuntimeError, match="hash does not match"):
        verify_manifest_artifacts(
            manifest,
            backup_path=backup,
            deployment_revision="a" * 40,
        )
    manifest, backup = _build(tmp_path)
    with pytest.raises(RuntimeError, match="revision does not match"):
        verify_manifest_artifacts(
            manifest,
            backup_path=tmp_path / "tracked-context-pre-reset.dump",
            deployment_revision="f" * 40,
        )


def test_reset_manifest_rejects_non_custom_backup(tmp_path: Path) -> None:
    backup = tmp_path / "tracked-context-pre-reset.dump"
    backup.write_text("not a dump", encoding="utf-8")
    matching_restore = tmp_path / "matching.txt"
    matching_restore.write_text("verified", encoding="utf-8")
    closure = _synthetic_payload()
    provider = _provider_manifest()

    with pytest.raises(RuntimeError, match="custom format"):
        build_reset_manifest(
            backup_path=backup,
            backup_ref=backup.name,
            deployment_revision="a" * 40,
            matching_application_image="sha256:" + "b" * 64,
            matching_application_revision="c" * 40,
            matching_restore_evidence_path=matching_restore,
            closure_payload=closure,
            provider_manifest=provider,
            rehearsal_evidence=_rehearsal_evidence(closure, provider),
        )
