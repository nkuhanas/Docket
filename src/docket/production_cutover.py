from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from docket.domain.production_reset import (
    TRACKED_CONTEXT_DOCUMENT_REF,
    TRACKED_CONTEXT_FROZEN_ARTIFACT_HASH,
    ProductionResetAuthorityBinding,
    production_reset_authorization_text,
)
from docket.domain.public_refs import new_public_ref
from docket.tracked_context_readiness import (
    _sha256,
    _validate_closure_payload,
    _validate_provider_manifest,
    _write_private_json,
    provider_disposition_inventory,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_BACKUP_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")

RESET_REMOVED_TABLES = (
    "accounts",
    "actions",
    "action_revisions",
    "affiliations",
    "agent_response_projections",
    "agent_responses",
    "approvals",
    "attention_case_revisions",
    "attention_cases",
    "audit_events",
    "calendar_event_cache",
    "calendar_lanes",
    "calendar_links",
    "calendar_profiles",
    "calendar_reminder_plans",
    "calendar_sync_states",
    "canonical_events",
    "case_items",
    "case_sources",
    "change_set_revisions",
    "change_sets",
    "command_requests",
    "conflicts",
    "connector_checkpoints",
    "context_packets",
    "daily_brief_case_items",
    "daily_brief_items",
    "daily_briefs",
    "decisions",
    "deferred_ingress",
    "discord_daily_threads",
    "discord_mcp_traces",
    "discord_projections",
    "drain_barriers",
    "entities",
    "entity_aliases",
    "entity_relations",
    "entity_resolutions",
    "event_observations",
    "execution_attempts",
    "execution_leases",
    "facts",
    "gateway_lifetimes",
    "identity_bindings",
    "identity_handles",
    "intent_sessions",
    "intent_turns",
    "interaction_participants",
    "interactions",
    "interpreted_statements",
    "lane_routing_decisions",
    "operation_bundles",
    "operation_items",
    "operations",
    "operator_utterances",
    "organization_profiles",
    "outbox_events",
    "persisted_semantic_options",
    "person_profiles",
    "preferences",
    "provenance_sources",
    "provider_event_bindings",
    "queue_item_sources",
    "queue_items",
    "records",
    "record_sources",
    "relationships",
    "reminder_rules",
    "runtime_log_entries",
    "scheduled_notifications",
    "semantic_candidates",
    "semantic_prompt_projections",
    "semantic_request_attempts",
    "semantic_requests",
    "sender_identity_emails",
    "source_items",
    "statement_relations",
    "tool_invocations",
    "triage_brief_entries",
    "triage_runs",
    "triage_window_memberships",
    "triage_windows",
)

RESET_PRESERVED_FAMILIES = (
    "transitive_governance_authority_closure",
    "specification_artifact_identity_and_hashes",
    "provider_account_configuration",
    "credential_and_secret_references",
    "verified_pre_reset_backup",
    "hash_signed_reset_manifest",
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{path.name} must contain one JSON object")
    return value


def _manifest_sha256(payload: Mapping[str, Any]) -> str:
    return _sha256(
        {
            key: value
            for key, value in payload.items()
            if key not in {"generated_at", "reset_manifest_sha256"}
        }
    )


def _validate_rehearsal_evidence(
    payload: Mapping[str, Any],
    *,
    closure_sha256: str,
    provider_manifest_sha256: str,
) -> None:
    if payload.get("document_ref") != TRACKED_CONTEXT_DOCUMENT_REF:
        raise RuntimeError("clean rehearsal evidence names the wrong amendment")
    if payload.get("closure_sha256") != closure_sha256:
        raise RuntimeError("clean rehearsal evidence does not bind the governance closure")
    if payload.get("provider_manifest_sha256") != provider_manifest_sha256:
        raise RuntimeError("clean rehearsal evidence does not bind the provider manifest")
    if payload.get("production_state_changed") is not False:
        raise RuntimeError("clean rehearsal evidence reports a production state change")
    if payload.get("provider_mutation_performed") is not False:
        raise RuntimeError("clean rehearsal evidence reports a provider mutation")
    evidence_hash = payload.get("evidence_sha256")
    expected_hash = _sha256(
        {
            key: value
            for key, value in payload.items()
            if key not in {"rehearsed_at", "evidence_sha256"}
        }
    )
    if evidence_hash != expected_hash:
        raise RuntimeError("clean rehearsal evidence hash mismatch")


def build_reset_manifest(
    *,
    backup_path: Path,
    backup_ref: str,
    deployment_revision: str,
    matching_application_image: str,
    matching_application_revision: str,
    matching_restore_evidence_path: Path,
    closure_payload: Mapping[str, Any],
    provider_manifest: Mapping[str, Any],
    rehearsal_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    if _BACKUP_REF.fullmatch(backup_ref) is None:
        raise ValueError("backup_ref must be one opaque artifact name")
    if _REVISION.fullmatch(deployment_revision) is None:
        raise ValueError("deployment_revision must be one full Git revision")
    if _REVISION.fullmatch(matching_application_revision) is None:
        raise ValueError("matching application revision must be one full Git revision")
    if not backup_path.is_file() or backup_path.stat().st_size == 0:
        raise RuntimeError("verified pre-reset backup is missing or empty")
    with backup_path.open("rb") as backup_handle:
        backup_header = backup_handle.read(5)
    if backup_header != b"PGDMP":
        raise RuntimeError("verified pre-reset backup is not PostgreSQL custom format")
    if not matching_application_image.strip():
        raise ValueError("matching application image must be nonempty")
    if (
        not matching_restore_evidence_path.is_file()
        or matching_restore_evidence_path.stat().st_size == 0
    ):
        raise RuntimeError("matching-image restore evidence is missing or empty")
    _validate_closure_payload(closure_payload)
    _validate_provider_manifest(provider_manifest)
    closure_sha256 = str(closure_payload["closure_sha256"])
    provider_manifest_sha256 = str(provider_manifest["manifest_sha256"])
    _validate_rehearsal_evidence(
        rehearsal_evidence,
        closure_sha256=closure_sha256,
        provider_manifest_sha256=provider_manifest_sha256,
    )
    target_dispositions = [
        {
            "target_sha256": target["target_sha256"],
            "target_kind": target["target_kind"],
            "disposition": target["disposition"],
        }
        for target in provider_manifest.get("targets", [])
    ]
    payload: dict[str, Any] = {
        "schema_version": 1,
        "document_ref": TRACKED_CONTEXT_DOCUMENT_REF,
        "frozen_artifact_hash": TRACKED_CONTEXT_FROZEN_ARTIFACT_HASH,
        "generated_at": datetime.now(UTC).isoformat(),
        "deployment_revision": deployment_revision,
        "verified_backup_ref": backup_ref,
        "verified_backup_sha256": _file_sha256(backup_path),
        "verified_backup_format": "postgresql_custom",
        "verified_backup_bytes": backup_path.stat().st_size,
        "matching_application_image": matching_application_image,
        "matching_application_revision": matching_application_revision,
        "matching_restore_evidence_sha256": _file_sha256(
            matching_restore_evidence_path
        ),
        "governance_closure_sha256": closure_sha256,
        "governance_row_count": closure_payload["row_count"],
        "provider_manifest_sha256": provider_manifest_sha256,
        "provider_target_dispositions": target_dispositions,
        "clean_rehearsal_evidence_sha256": rehearsal_evidence["evidence_sha256"],
        "removed_tables": list(RESET_REMOVED_TABLES),
        "preserved_families": list(RESET_PRESERVED_FAMILIES),
        "provider_mutation_authorized": False,
        "production_reset_executed": False,
    }
    payload["reset_manifest_sha256"] = _manifest_sha256(payload)
    return payload


def validate_reset_manifest(payload: Mapping[str, Any]) -> None:
    if payload.get("schema_version") != 1:
        raise RuntimeError("unsupported production reset manifest schema")
    if (
        payload.get("document_ref") != TRACKED_CONTEXT_DOCUMENT_REF
        or payload.get("frozen_artifact_hash")
        != TRACKED_CONTEXT_FROZEN_ARTIFACT_HASH
    ):
        raise RuntimeError("production reset manifest artifact mismatch")
    if payload.get("provider_mutation_authorized") is not False:
        raise RuntimeError("production reset manifest cannot authorize provider mutation")
    if payload.get("production_reset_executed") is not False:
        raise RuntimeError("a prepared reset manifest cannot claim execution")
    if tuple(payload.get("removed_tables", ())) != RESET_REMOVED_TABLES:
        raise RuntimeError("production reset removal boundary is not exact")
    if tuple(payload.get("preserved_families", ())) != RESET_PRESERVED_FAMILIES:
        raise RuntimeError("production reset preservation boundary is not exact")
    if _REVISION.fullmatch(str(payload.get("deployment_revision", ""))) is None:
        raise RuntimeError("production reset deployment revision is invalid")
    if _BACKUP_REF.fullmatch(str(payload.get("verified_backup_ref", ""))) is None:
        raise RuntimeError("production reset backup reference is invalid")
    for field in (
        "frozen_artifact_hash",
        "verified_backup_sha256",
        "matching_restore_evidence_sha256",
        "governance_closure_sha256",
        "provider_manifest_sha256",
        "clean_rehearsal_evidence_sha256",
        "reset_manifest_sha256",
    ):
        if _SHA256.fullmatch(str(payload.get(field, ""))) is None:
            raise RuntimeError(f"production reset manifest has invalid {field}")
    if payload.get("reset_manifest_sha256") != _manifest_sha256(payload):
        raise RuntimeError("production reset manifest hash mismatch")
    dispositions = list(payload.get("provider_target_dispositions", []))
    target_hashes = [item.get("target_sha256") for item in dispositions]
    if len(target_hashes) != len(set(target_hashes)):
        raise RuntimeError("production reset manifest repeats a provider target")


def authorization_binding(payload: Mapping[str, Any]) -> ProductionResetAuthorityBinding:
    validate_reset_manifest(payload)
    return ProductionResetAuthorityBinding(
        document_ref=str(payload["document_ref"]),
        frozen_artifact_hash=str(payload["frozen_artifact_hash"]),
        reset_manifest_sha256=str(payload["reset_manifest_sha256"]),
        verified_backup_ref=str(payload["verified_backup_ref"]),
        verified_backup_sha256=str(payload["verified_backup_sha256"]),
        deployment_revision=str(payload["deployment_revision"]),
    )


def verify_manifest_artifacts(
    payload: Mapping[str, Any],
    *,
    backup_path: Path,
    deployment_revision: str,
) -> None:
    validate_reset_manifest(payload)
    if backup_path.name != payload["verified_backup_ref"]:
        raise RuntimeError("backup artifact name does not match reset manifest")
    if _file_sha256(backup_path) != payload["verified_backup_sha256"]:
        raise RuntimeError("backup artifact hash does not match reset manifest")
    if deployment_revision != payload["deployment_revision"]:
        raise RuntimeError("running deployment revision does not match reset manifest")


def verify_supporting_artifacts(
    manifest: Mapping[str, Any],
    *,
    closure_payload: Mapping[str, Any],
    provider_manifest: Mapping[str, Any],
    rehearsal_evidence: Mapping[str, Any],
) -> None:
    validate_reset_manifest(manifest)
    _validate_closure_payload(closure_payload)
    _validate_provider_manifest(provider_manifest)
    closure_sha256 = str(closure_payload["closure_sha256"])
    provider_manifest_sha256 = str(provider_manifest["manifest_sha256"])
    _validate_rehearsal_evidence(
        rehearsal_evidence,
        closure_sha256=closure_sha256,
        provider_manifest_sha256=provider_manifest_sha256,
    )
    if closure_sha256 != manifest["governance_closure_sha256"]:
        raise RuntimeError("governance closure does not match reset manifest")
    if provider_manifest_sha256 != manifest["provider_manifest_sha256"]:
        raise RuntimeError("provider inventory does not match reset manifest")
    if (
        rehearsal_evidence["evidence_sha256"]
        != manifest["clean_rehearsal_evidence_sha256"]
    ):
        raise RuntimeError("clean rehearsal does not match reset manifest")


def _provider_state_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    accounts = [
        {
            key: value
            for key, value in dict(account).items()
            if key != "clean_account_ref"
        }
        for account in payload.get("accounts", [])
    ]
    return {
        key: value
        for key, value in payload.items()
        if key not in {"generated_at", "manifest_sha256", "accounts"}
    } | {"accounts": accounts}


def verify_live_provider_state(
    *,
    database_url: str,
    sealed_provider_manifest: Mapping[str, Any],
) -> None:
    _validate_provider_manifest(sealed_provider_manifest)
    current = provider_disposition_inventory(database_url=database_url)
    _validate_provider_manifest(current)
    if _sha256(_provider_state_payload(current)) != _sha256(
        _provider_state_payload(sealed_provider_manifest)
    ):
        raise RuntimeError("live provider state changed after reset manifest sealing")


def _json_list(value: object) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [str(item) for item in value]
    return []


def production_reset_authority_evidence(
    *,
    database_url: str,
    manifest: Mapping[str, Any],
    operator_discord_user_id: str,
) -> dict[str, str]:
    binding = authorization_binding(manifest)
    expected_text = production_reset_authorization_text(binding)
    expected_content_hash = hashlib.sha256(expected_text.encode("utf-8")).hexdigest()
    database_target = database_url.replace(
        "postgresql+psycopg://", "postgresql://", 1
    )
    with psycopg.connect(database_target, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        rows = connection.execute(
            """
            SELECT ref_id, actor_ref, basis_refs, payload_json
              FROM decisions
             WHERE decision_kind = 'production_reset_authorization'
               AND document_ref = %s
               AND frozen_artifact_hash = %s
               AND architecture_authority IS FALSE
               AND authorized_scope = 'production_reset_exact_manifest_revision'
             ORDER BY created_at DESC
            """,
            (binding.document_ref, binding.frozen_artifact_hash),
        ).fetchall()
        for row in rows:
            if dict(row["payload_json"]) != {
                "reset_manifest_sha256": binding.reset_manifest_sha256,
                "verified_backup_ref": binding.verified_backup_ref,
                "verified_backup_sha256": binding.verified_backup_sha256,
                "deployment_revision": binding.deployment_revision,
            }:
                continue
            basis_refs = _json_list(row["basis_refs"])
            if len(basis_refs) != 1 or not basis_refs[0].startswith("utt_"):
                continue
            utterance = connection.execute(
                """
                SELECT actor_ref, transport, verbatim_text, content_hash
                  FROM operator_utterances
                 WHERE ref_id = %s
                """,
                (basis_refs[0],),
            ).fetchone()
            if (
                utterance is None
                or utterance["actor_ref"]
                != f"discord_user:{operator_discord_user_id}"
                or utterance["transport"] != "discord"
                or utterance["verbatim_text"] != expected_text
                or utterance["content_hash"] != expected_content_hash
            ):
                continue
            audit = connection.execute(
                """
                SELECT ref_id
                  FROM audit_events
                 WHERE event_type = 'decision.production_reset_authorization_recorded'
                   AND primary_ref = %s
                   AND basis_refs::jsonb = %s::jsonb
                """,
                (row["ref_id"], json.dumps(basis_refs)),
            ).fetchone()
            if audit is not None:
                return {
                    "decision_ref": str(row["ref_id"]),
                    "utterance_ref": basis_refs[0],
                    "audit_ref": str(audit["ref_id"]),
                }
    raise RuntimeError(
        "no exact manifest, backup, and revision-bound production reset Decision exists"
    )


def verify_production_reset_authority(
    *,
    database_url: str,
    manifest: Mapping[str, Any],
    operator_discord_user_id: str,
) -> str:
    return production_reset_authority_evidence(
        database_url=database_url,
        manifest=manifest,
        operator_discord_user_id=operator_discord_user_id,
    )["decision_ref"]


def validate_governance_closure_extension(
    *,
    sealed_closure: Mapping[str, Any],
    final_closure: Mapping[str, Any],
    authority_evidence: Mapping[str, str],
) -> dict[str, Any]:
    _validate_closure_payload(sealed_closure)
    _validate_closure_payload(final_closure)
    sealed_rows = {
        (str(row["table"]), str(row["ref_id"])): row
        for row in sealed_closure.get("rows", [])
    }
    final_rows = {
        (str(row["table"]), str(row["ref_id"])): row
        for row in final_closure.get("rows", [])
    }
    for key, sealed_row in sealed_rows.items():
        final_row = final_rows.get(key)
        if final_row is None or final_row.get("row_sha256") != sealed_row.get(
            "row_sha256"
        ):
            raise RuntimeError(
                f"sealed governance row changed before reset: {key[1]}"
            )
        if final_row.get("restore_hints_sha256") != sealed_row.get(
            "restore_hints_sha256"
        ):
            raise RuntimeError(
                f"sealed governance restore hints changed before reset: {key[1]}"
            )
    required = {
        ("decisions", authority_evidence["decision_ref"]),
        ("operator_utterances", authority_evidence["utterance_ref"]),
        ("audit_events", authority_evidence["audit_ref"]),
    }
    missing = sorted(ref_id for table, ref_id in required if (table, ref_id) not in final_rows)
    if missing:
        raise RuntimeError(
            "final governance closure omits reset authority: " + ", ".join(missing)
        )
    final_seeds = {str(ref) for ref in final_closure.get("seed_refs", [])}
    if authority_evidence["decision_ref"] not in final_seeds:
        raise RuntimeError("reset authorization Decision is not a final closure seed")
    added = sorted(set(final_rows) - set(sealed_rows))
    result = {
        "sealed_closure_sha256": sealed_closure["closure_sha256"],
        "final_closure_sha256": final_closure["closure_sha256"],
        "sealed_rows": len(sealed_rows),
        "final_rows": len(final_rows),
        "added_refs": [ref_id for _table, ref_id in added],
        "authority_decision_ref": authority_evidence["decision_ref"],
    }
    result["extension_sha256"] = _sha256(result)
    return result


def record_production_reset_completion(
    *,
    database_url: str,
    manifest: Mapping[str, Any],
    operator_discord_user_id: str,
    final_closure_sha256: str,
    closure_extension_sha256: str,
) -> str:
    if _SHA256.fullmatch(final_closure_sha256) is None:
        raise ValueError("final_closure_sha256 must be lowercase SHA-256")
    if _SHA256.fullmatch(closure_extension_sha256) is None:
        raise ValueError("closure_extension_sha256 must be lowercase SHA-256")
    authority = production_reset_authority_evidence(
        database_url=database_url,
        manifest=manifest,
        operator_discord_user_id=operator_discord_user_id,
    )
    binding = authorization_binding(manifest)
    data = {
        "document_ref": binding.document_ref,
        "frozen_artifact_hash": binding.frozen_artifact_hash,
        "reset_manifest_sha256": binding.reset_manifest_sha256,
        "verified_backup_ref": binding.verified_backup_ref,
        "verified_backup_sha256": binding.verified_backup_sha256,
        "deployment_revision": binding.deployment_revision,
        "final_closure_sha256": final_closure_sha256,
        "closure_extension_sha256": closure_extension_sha256,
        "provider_mutation_performed": False,
    }
    database_target = database_url.replace(
        "postgresql+psycopg://", "postgresql://", 1
    )
    with psycopg.connect(database_target, row_factory=dict_row) as connection:
        existing = connection.execute(
            """
            SELECT ref_id
              FROM audit_events
             WHERE event_type = 'production_reset.completed'
               AND primary_ref = %s
               AND data ->> 'reset_manifest_sha256' = %s
            """,
            (authority["decision_ref"], binding.reset_manifest_sha256),
        ).fetchone()
        if existing is not None:
            return str(existing["ref_id"])
        audit_ref = new_public_ref("aud")
        connection.execute(
            """
            INSERT INTO audit_events (
                id, ref_id, event_type, entity_type, entity_id,
                actor_type, actor_id, request_id, primary_ref,
                affected_refs, basis_refs, data, created_at
            ) VALUES (
                %s, %s, 'production_reset.completed', 'production_reset', NULL,
                'operator', %s, NULL, %s, %s, %s, %s, %s
            )
            """,
            (
                uuid.uuid4(),
                audit_ref,
                f"discord_user:{operator_discord_user_id}",
                authority["decision_ref"],
                Jsonb([authority["decision_ref"]]),
                Jsonb([authority["utterance_ref"], authority["decision_ref"]]),
                Jsonb(data),
                datetime.now(UTC),
            ),
        )
        return audit_ref


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Docket production reset preparation")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build-manifest")
    build.add_argument("--backup", type=Path, required=True)
    build.add_argument("--backup-ref", required=True)
    build.add_argument("--deployment-revision", required=True)
    build.add_argument("--matching-application-image", required=True)
    build.add_argument("--matching-application-revision", required=True)
    build.add_argument("--matching-restore-evidence", type=Path, required=True)
    build.add_argument("--closure", type=Path, required=True)
    build.add_argument("--provider-manifest", type=Path, required=True)
    build.add_argument("--rehearsal-evidence", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    show = subparsers.add_parser("authorization-text")
    show.add_argument("--manifest", type=Path, required=True)
    verify = subparsers.add_parser("verify-execution")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--backup", type=Path, required=True)
    verify.add_argument("--closure", type=Path, required=True)
    verify.add_argument("--provider-manifest", type=Path, required=True)
    verify.add_argument("--rehearsal-evidence", type=Path, required=True)
    verify.add_argument("--database-url", required=True)
    verify.add_argument("--operator-discord-user-id", required=True)
    verify.add_argument("--deployment-revision", required=True)
    verify.add_argument("--output", type=Path, required=True)
    authority = subparsers.add_parser("verify-authority")
    authority.add_argument("--manifest", type=Path, required=True)
    authority.add_argument("--database-url", required=True)
    authority.add_argument("--operator-discord-user-id", required=True)
    authority.add_argument("--output", type=Path, required=True)
    extension = subparsers.add_parser("verify-closure-extension")
    extension.add_argument("--sealed-closure", type=Path, required=True)
    extension.add_argument("--final-closure", type=Path, required=True)
    extension.add_argument("--authority-evidence", type=Path, required=True)
    extension.add_argument("--output", type=Path, required=True)
    completion = subparsers.add_parser("record-completion")
    completion.add_argument("--manifest", type=Path, required=True)
    completion.add_argument("--closure-extension", type=Path, required=True)
    completion.add_argument("--database-url", required=True)
    completion.add_argument("--operator-discord-user-id", required=True)
    completion.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "build-manifest":
        manifest = build_reset_manifest(
            backup_path=args.backup,
            backup_ref=args.backup_ref,
            deployment_revision=args.deployment_revision,
            matching_application_image=args.matching_application_image,
            matching_application_revision=args.matching_application_revision,
            matching_restore_evidence_path=args.matching_restore_evidence,
            closure_payload=_load_json(args.closure),
            provider_manifest=_load_json(args.provider_manifest),
            rehearsal_evidence=_load_json(args.rehearsal_evidence),
        )
        _write_private_json(args.output, manifest)
        print(
            json.dumps(
                {
                    "reset_manifest_sha256": manifest["reset_manifest_sha256"],
                    "verified_backup_ref": manifest["verified_backup_ref"],
                    "verified_backup_sha256": manifest["verified_backup_sha256"],
                    "deployment_revision": manifest["deployment_revision"],
                },
                sort_keys=True,
            )
        )
    elif args.command == "authorization-text":
        manifest = _load_json(args.manifest)
        text = production_reset_authorization_text(authorization_binding(manifest))
        os.write(1, text.encode("utf-8") + b"\n")
    elif args.command == "verify-execution":
        manifest = _load_json(args.manifest)
        closure = _load_json(args.closure)
        provider_manifest = _load_json(args.provider_manifest)
        rehearsal_evidence = _load_json(args.rehearsal_evidence)
        verify_manifest_artifacts(
            manifest,
            backup_path=args.backup,
            deployment_revision=args.deployment_revision,
        )
        verify_supporting_artifacts(
            manifest,
            closure_payload=closure,
            provider_manifest=provider_manifest,
            rehearsal_evidence=rehearsal_evidence,
        )
        verify_live_provider_state(
            database_url=args.database_url,
            sealed_provider_manifest=provider_manifest,
        )
        evidence = production_reset_authority_evidence(
            database_url=args.database_url,
            manifest=manifest,
            operator_discord_user_id=args.operator_discord_user_id,
        )
        _write_private_json(args.output, evidence)
        print(json.dumps(evidence, sort_keys=True))
    elif args.command == "verify-authority":
        evidence = production_reset_authority_evidence(
            database_url=args.database_url,
            manifest=_load_json(args.manifest),
            operator_discord_user_id=args.operator_discord_user_id,
        )
        _write_private_json(args.output, evidence)
        print(json.dumps(evidence, sort_keys=True))
    elif args.command == "verify-closure-extension":
        verification = validate_governance_closure_extension(
            sealed_closure=_load_json(args.sealed_closure),
            final_closure=_load_json(args.final_closure),
            authority_evidence=_load_json(args.authority_evidence),
        )
        _write_private_json(args.output, verification)
        print(json.dumps(verification, sort_keys=True))
    else:
        extension = _load_json(args.closure_extension)
        audit_ref = record_production_reset_completion(
            database_url=args.database_url,
            manifest=_load_json(args.manifest),
            operator_discord_user_id=args.operator_discord_user_id,
            final_closure_sha256=str(extension["final_closure_sha256"]),
            closure_extension_sha256=str(extension["extension_sha256"]),
        )
        result = {
            "audit_ref": audit_ref,
            "reset_manifest_sha256": _load_json(args.manifest)[
                "reset_manifest_sha256"
            ],
        }
        _write_private_json(args.output, result)
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
