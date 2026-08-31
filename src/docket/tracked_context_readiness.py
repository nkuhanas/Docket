from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from docket.domain.public_refs import new_public_ref
from docket.models import Base
from docket.specification_artifacts import specification_artifact_manifest

_PUBLIC_REF = re.compile(r"^[a-z][a-z0-9]{1,7}_[0-9A-HJKMNP-TV-Z]{26}$")
_PUBLIC_REF_IN_TEXT = re.compile(r"\b[a-z][a-z0-9]{1,7}_[0-9A-HJKMNP-TV-Z]{26}\b")
_REVERSE_EVIDENCE_TABLES = frozenset(
    {
        "agent_responses",
        "audit_events",
        "deferred_ingress",
        "intent_turns",
        "interpreted_statements",
        "runtime_log_entries",
        "tool_invocations",
    }
)
_AUTHORITY_REF_FIELDS = frozenset(
    {
        "agent_response_ref",
        "basis_refs",
        "decision_refs",
        "gateway_instance_ref",
        "intent_session_ref",
        "primary_ref",
        "prerequisite_decision_refs",
        "related_refs",
        "responds_to_utterance_refs",
        "statement_refs",
        "tool_call_refs",
        "utterance_ref",
        "utterance_refs",
    }
)
_GOVERNANCE_PREFIXES = frozenset(
    {
        "aud",
        "call",
        "chg",
        "cnf",
        "conf",
        "dec",
        "drain",
        "gwy",
        "log",
        "rsp",
        "satt",
        "sattempt",
        "ses",
        "sreq",
        "stm",
        "trace",
        "turn",
        "utt",
    }
)
_CLEAN_TABLES = frozenset(Base.metadata.tables)
_OBSOLETE_TABLES = frozenset(
    {
        "accounts",
        "actions",
        "action_revisions",
        "approvals",
        "calendar_links",
        "calendar_profiles",
        "operation_bundles",
        "operation_items",
        "queue_items",
        "records",
        "record_sources",
        "semantic_prompt_projections",
        "source_items",
        "triage_brief_entries",
    }
)
_ALLOWED_PROVIDER_DISPOSITIONS = frozenset(
    {
        "leave_external_unmanaged",
        "adopt_into_clean_model",
        "delete_by_separately_authorized_provider_operation",
    }
)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _database_name(database_url: str) -> str:
    parsed = urlparse(database_url.replace("postgresql+psycopg://", "postgresql://", 1))
    return parsed.path.removeprefix("/")


def _psycopg_url(database_url: str) -> str:
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def require_rehearsal_database(database_url: str, *, suffix: str) -> None:
    name = _database_name(database_url)
    if not name.endswith(suffix):
        raise ValueError(f"rehearsal database must end with {suffix!r}")


def require_cutover_database(database_url: str) -> None:
    name = _database_name(database_url)
    if re.fullmatch(r"docket_cutover_[A-Za-z0-9_]+", name) is None:
        raise ValueError("cutover database must use the docket_cutover_ namespace")


def _refs(value: object) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, str):
        if _PUBLIC_REF.fullmatch(value):
            refs.add(value)
    elif isinstance(value, Mapping):
        for child in value.values():
            refs.update(_refs(child))
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for child in value:
            refs.update(_refs(child))
    return refs


@dataclass(frozen=True)
class ClosureRow:
    table: str
    ref_id: str
    row: dict[str, Any]
    restore_hints: dict[str, Any] = field(default_factory=dict)

    @property
    def refs(self) -> set[str]:
        return _refs(self.row)

    @property
    def authority_refs(self) -> set[str]:
        refs: set[str] = set()
        for field_name, value in self.row.items():
            candidates = _refs(value)
            if field_name in _AUTHORITY_REF_FIELDS:
                refs.update(candidates)
        return refs

    @property
    def reverse_refs(self) -> set[str]:
        fields_by_table = {
            "agent_responses": {"basis_refs", "responds_to_utterance_refs"},
            "audit_events": {"primary_ref"},
            "deferred_ingress": {"operator_utterance_ref"},
            "intent_turns": {"agent_response_ref", "utterance_ref"},
            "interpreted_statements": {"utterance_ref"},
            "runtime_log_entries": {"related_refs"},
            "tool_invocations": {
                "utterance_refs",
            },
        }
        fields = fields_by_table.get(self.table, set())
        refs = {ref for field in fields for ref in _refs(self.row.get(field))}
        if self.table == "runtime_log_entries":
            return {
                ref for ref in refs if ref.partition("_")[0] in _GOVERNANCE_PREFIXES
            }
        return refs

    def export(self) -> dict[str, Any]:
        exported = {
            "table": self.table,
            "ref_id": self.ref_id,
            "row_sha256": _sha256(self.row),
            "ref_edges": sorted(self.refs),
            "authority_edges": sorted(self.authority_refs),
            "row": self.row,
        }
        if self.restore_hints:
            exported["restore_hints"] = self.restore_hints
            exported["restore_hints_sha256"] = _sha256(self.restore_hints)
        return exported


def compute_governance_closure(
    rows: Iterable[ClosureRow],
    *,
    seed_refs: Iterable[str],
) -> tuple[list[ClosureRow], list[str]]:
    materialized = list(rows)
    by_ref: dict[str, list[ClosureRow]] = {}
    for row in materialized:
        by_ref.setdefault(row.ref_id, []).append(row)

    closure = set(seed_refs)
    changed = True
    while changed:
        changed = False
        for ref_id in tuple(closure):
            for row in by_ref.get(ref_id, ()):  # follow upstream typed references
                before = len(closure)
                closure.update(row.authority_refs)
                changed = changed or len(closure) != before
        for row in materialized:  # include bounded downstream evidence
            if row.table not in _REVERSE_EVIDENCE_TABLES or row.ref_id in closure:
                continue
            if row.reverse_refs & closure:
                closure.add(row.ref_id)
                changed = True

    selected = sorted(
        (row for row in materialized if row.ref_id in closure),
        key=lambda row: (row.table, row.ref_id),
    )
    available = set(by_ref)
    missing = sorted(ref for ref in closure if ref not in available)
    return selected, missing


def _seed_refs_from_files(paths: Iterable[Path]) -> set[str]:
    allowed_prefixes = {"aud", "call", "dec", "log", "utt"}
    refs: set[str] = set()
    for path in paths:
        refs.update(
            ref
            for ref in _PUBLIC_REF_IN_TEXT.findall(path.read_text(encoding="utf-8"))
            if ref.partition("_")[0] in allowed_prefixes
        )
    return refs


def _ref_tables(connection: psycopg.Connection[dict[str, Any]]) -> list[str]:
    rows = connection.execute(
        """
        SELECT table_name
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND column_name = 'ref_id'
         ORDER BY table_name
        """
    ).fetchall()
    return [str(row["table_name"]) for row in rows]


def _snapshot_rows(
    connection: psycopg.Connection[dict[str, Any]],
) -> list[ClosureRow]:
    rows: list[ClosureRow] = []
    for table in _ref_tables(connection):
        query = sql.SQL(
            "SELECT ref_id, to_jsonb(source_row)::text AS row_json "
            "FROM {} AS source_row WHERE ref_id IS NOT NULL"
        ).format(sql.Identifier(table))
        for result in connection.execute(query).fetchall():
            row_json = json.loads(str(result["row_json"]))
            restore_hints: dict[str, Any] = {}
            if table == "facts":
                subject_ref = row_json.get("subject_ref")
                if subject_ref is None and row_json.get("subject_entity_id") is not None:
                    subject = connection.execute(
                        "SELECT ref_id FROM entities WHERE id = %s",
                        (row_json["subject_entity_id"],),
                    ).fetchone()
                    if subject is None:
                        raise RuntimeError(
                            f"fact {result['ref_id']} has no resolvable subject"
                        )
                    subject_ref = subject["ref_id"]
                if subject_ref is not None:
                    restore_hints["subject_ref"] = str(subject_ref)
            rows.append(
                ClosureRow(
                    table=table,
                    ref_id=str(result["ref_id"]),
                    row=row_json,
                    restore_hints=restore_hints,
                )
            )
    return rows


def _artifact_seed_refs(
    connection: psycopg.Connection[dict[str, Any]],
) -> set[str]:
    seeds: set[str] = set()
    manifest = specification_artifact_manifest()
    for artifact in manifest.artifacts:
        rows = connection.execute(
            """
            SELECT ref_id, basis_refs
              FROM decisions
             WHERE decision_kind = 'specification_signoff'
               AND document_ref = %s
               AND frozen_artifact_hash = %s
               AND architecture_authority IS TRUE
            """,
            (artifact.document_ref, artifact.frozen_artifact_hash),
        ).fetchall()
        for row in rows:
            seeds.add(str(row["ref_id"]))
            seeds.update(_refs(row["basis_refs"]))
        for prerequisite in artifact.prerequisites:
            if prerequisite.decision_ref is not None:
                seeds.add(prerequisite.decision_ref)
        if artifact.bootstrap_authority is not None:
            seeds.add(artifact.bootstrap_authority.utterance_ref)
    bootstrap_rows = connection.execute(
        "SELECT ref_id, basis_refs FROM decisions "
        "WHERE decision_kind = 'provenance_bootstrap_signoff'"
    ).fetchall()
    for row in bootstrap_rows:
        seeds.add(str(row["ref_id"]))
        seeds.update(_refs(row["basis_refs"]))
    return seeds


def export_governance_closure(
    *,
    database_url: str,
    seed_files: Iterable[Path],
    extra_seed_refs: Iterable[str] = (),
) -> dict[str, Any]:
    with psycopg.connect(_psycopg_url(database_url), row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        seeds = _seed_refs_from_files(seed_files)
        seeds.update(_artifact_seed_refs(connection))
        seeds.update(extra_seed_refs)
        rows = _snapshot_rows(connection)
        closure, missing = compute_governance_closure(rows, seed_refs=seeds)
        available_refs = {row.ref_id for row in rows}
    if missing:
        raise RuntimeError(
            "governance closure contains missing typed refs: " + ", ".join(missing)
        )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "document_ref": "ONT-DELTA-2026-08-29-TRACKED-CONTEXT",
        "generated_at": datetime.now(UTC).isoformat(),
        "seed_refs": sorted(seeds),
        "row_count": len(closure),
        "unresolved_non_authority_refs": sorted(
            {
                ref
                for row in closure
                for ref in row.refs - row.authority_refs
                if ref not in available_refs
            }
        ),
        "rows": [row.export() for row in closure],
    }
    payload["closure_sha256"] = _sha256(
        {key: value for key, value in payload.items() if key != "generated_at"}
    )
    return payload


def _target_key(*parts: object) -> str:
    return hashlib.sha256(
        ":".join(str(part) for part in parts).encode("utf-8")
    ).hexdigest()


def _new_clean_rehearsal_ref(prefix: str) -> str:
    _old_prefix, separator, ulid = new_public_ref("src").partition("_")
    if separator != "_" or not ulid:
        raise AssertionError("public reference generator returned an invalid payload")
    return f"{prefix}_{ulid}"


def _export_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def provider_disposition_inventory(*, database_url: str) -> dict[str, Any]:
    with psycopg.connect(_psycopg_url(database_url), row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        accounts = connection.execute(
            """
            SELECT ref_id AS pre_reset_account_ref,
                   provider, external_account_id, display_name, email_address,
                   capabilities, enabled, credential_ref
              FROM accounts
             ORDER BY ref_id
            """
        ).fetchall()
        event_targets = connection.execute(
            """
            WITH targets AS (
                SELECT a.ref_id AS account_ref,
                       cl.calendar_id,
                       cl.external_event_id AS provider_event_id,
                       'calendar_link'::text AS source_family,
                       cl.origin_kind::text AS source_state,
                       ce.ref_id AS event_ref,
                       COALESCE(ce.title, r.title, cache.summary, cl.logical_key) AS label
                  FROM calendar_links cl
                  JOIN accounts a ON a.id = cl.account_id
             LEFT JOIN canonical_events ce ON ce.id = cl.canonical_event_id
             LEFT JOIN records r ON r.id = cl.record_id
             LEFT JOIN calendar_event_cache cache
                    ON cache.account_id = cl.account_id
                   AND cache.calendar_id = cl.calendar_id
                   AND cache.provider_event_id = cl.external_event_id
                UNION ALL
                SELECT a.ref_id, binding.calendar_id, binding.provider_event_id,
                       'provider_event_binding', binding.status, ce.ref_id, ce.title
                  FROM provider_event_bindings binding
                  JOIN accounts a ON a.id = binding.account_id
                  JOIN canonical_events ce ON ce.id = binding.canonical_event_id
            )
            SELECT account_ref, calendar_id, provider_event_id,
                   array_agg(DISTINCT source_family ORDER BY source_family) AS source_families,
                   array_agg(DISTINCT source_state ORDER BY source_state) AS source_states,
                   max(event_ref) AS event_ref, max(label) AS label
              FROM targets
             GROUP BY account_ref, calendar_id, provider_event_id
             ORDER BY account_ref, calendar_id, provider_event_id
            """
        ).fetchall()
        calendar_targets = connection.execute(
            """
            SELECT a.ref_id AS account_ref, lane.calendar_id,
                   array_agg(lane.ref_id ORDER BY lane.ref_id) AS lane_refs,
                   array_agg(DISTINCT lane.status ORDER BY lane.status) AS lane_states,
                   max(lane.display_name) AS label
              FROM calendar_lanes lane
              JOIN accounts a ON a.id = lane.account_id
             WHERE lane.calendar_id IS NOT NULL
             GROUP BY a.ref_id, lane.calendar_id
             ORDER BY a.ref_id, lane.calendar_id
            """
        ).fetchall()
        operations = connection.execute(
            """
            SELECT operation.ref_id, operation.operation_type, operation.status,
                   account.ref_id AS account_ref,
                   operation.originating_changeset_ref, operation.basis_refs,
                   operation.canonical_target_refs, operation.provenance_status,
                   operation.attempt_count, operation.last_error_code
              FROM operations operation
              JOIN accounts account ON account.id = operation.account_id
             ORDER BY operation.ref_id
            """
        ).fetchall()
        operation_items = connection.execute(
            """
            SELECT operation.ref_id AS operation_ref, item.item_key, item.item_type,
                   item.parameters_sha256, item.status, item.attempt_count,
                   item.last_error_code
              FROM operation_items item
              JOIN operations operation ON operation.id = item.operation_id
             ORDER BY operation.ref_id, item.item_key
            """
        ).fetchall()
        execution_attempts = connection.execute(
            """
            SELECT operation.ref_id AS operation_ref,
                   item.item_key AS operation_item_key,
                   attempt.attempt_number, attempt.kind, attempt.status,
                   attempt.provider_request_id, attempt.error_code,
                   attempt.started_at, attempt.completed_at
              FROM execution_attempts attempt
              JOIN operations operation ON operation.id = attempt.operation_id
         LEFT JOIN operation_items item ON item.id = attempt.operation_item_id
             ORDER BY operation.ref_id, item.item_key NULLS FIRST,
                      attempt.attempt_number
            """
        ).fetchall()
        bindings = connection.execute(
            """
            SELECT event.ref_id AS event_ref, account.ref_id AS account_ref,
                   binding.calendar_id, binding.provider_event_id,
                   binding.status, binding.version
              FROM provider_event_bindings binding
              JOIN canonical_events event ON event.id = binding.canonical_event_id
              JOIN accounts account ON account.id = binding.account_id
             ORDER BY account.ref_id, binding.calendar_id, binding.provider_event_id
            """
        ).fetchall()
        links = connection.execute(
            """
            SELECT account.ref_id AS account_ref, link.calendar_id,
                   link.external_event_id AS provider_event_id, link.origin_kind,
                   event.ref_id AS event_ref, link.recurrence_kind,
                   link.last_synced_version
              FROM calendar_links link
              JOIN accounts account ON account.id = link.account_id
         LEFT JOIN canonical_events event ON event.id = link.canonical_event_id
             ORDER BY account.ref_id, link.calendar_id, link.external_event_id
            """
        ).fetchall()
        events = connection.execute(
            """
            SELECT DISTINCT event.ref_id, event.title, event.status, event.lane_ref,
                   event.routing_decision_ref, event.version
              FROM canonical_events event
             WHERE EXISTS (
                       SELECT 1 FROM calendar_links link
                        WHERE link.canonical_event_id = event.id
                   )
                OR EXISTS (
                       SELECT 1 FROM provider_event_bindings binding
                        WHERE binding.canonical_event_id = event.id
                   )
             ORDER BY event.ref_id
            """
        ).fetchall()
        lanes = connection.execute(
            """
            SELECT lane.ref_id, account.ref_id AS account_ref, lane.lane,
                   lane.display_name, lane.calendar_id, lane.status,
                   lane.enabled, lane.version
              FROM calendar_lanes lane
              JOIN accounts account ON account.id = lane.account_id
             ORDER BY lane.ref_id
            """
        ).fetchall()

    targets: list[dict[str, Any]] = []
    for row in calendar_targets:
        targets.append(
            {
                "target_kind": "google_calendar",
                "target_sha256": _target_key(
                    row["account_ref"], "google_calendar", row["calendar_id"]
                ),
                "account_ref": row["account_ref"],
                "calendar_id": row["calendar_id"],
                "lane_refs": row["lane_refs"],
                "source_states": row["lane_states"],
                "label": row["label"],
                "disposition": "leave_external_unmanaged",
            }
        )
    for row in event_targets:
        targets.append(
            {
                "target_kind": "google_calendar_event",
                "target_sha256": _target_key(
                    row["account_ref"],
                    "google_calendar_event",
                    row["calendar_id"],
                    row["provider_event_id"],
                ),
                "account_ref": row["account_ref"],
                "calendar_id": row["calendar_id"],
                "provider_event_id": row["provider_event_id"],
                "source_families": row["source_families"],
                "source_states": row["source_states"],
                "event_ref": row["event_ref"],
                "label": row["label"],
                "disposition": "leave_external_unmanaged",
            }
        )
    if any(
        target["disposition"] not in _ALLOWED_PROVIDER_DISPOSITIONS
        for target in targets
    ):
        raise AssertionError("invalid provider disposition")

    status_blockers = {
        "operations": [
            str(row["ref_id"])
            for row in operations
            if row["status"] in {"pending", "running", "reconciliation_required"}
        ],
        "operation_items": [
            f"{row['operation_ref']}:{row['item_key']}"
            for row in operation_items
            if row["status"] in {"pending", "running", "reconciliation_required"}
        ],
        "execution_attempts": [
            f"{row['operation_ref']}:{row['operation_item_key']}:{row['attempt_number']}"
            for row in execution_attempts
            if row["status"] in {"started", "unknown"}
        ],
    }
    clean_accounts = [
        {
            "clean_account_ref": _new_clean_rehearsal_ref("acct"),
            "pre_reset_account_ref": row["pre_reset_account_ref"],
            "provider": row["provider"],
            "external_account_id": row["external_account_id"],
            "external_identity_sha256": _target_key(
                row["provider"], row["external_account_id"]
            ),
            "display_name": row["display_name"],
            "email_address": row["email_address"],
            "capabilities": row["capabilities"],
            "enabled": row["enabled"],
            "credential_ref": row["credential_ref"],
        }
        for row in accounts
    ]
    payload: dict[str, Any] = {
        "schema_version": 1,
        "document_ref": "ONT-DELTA-2026-08-29-TRACKED-CONTEXT",
        "generated_at": datetime.now(UTC).isoformat(),
        "default_disposition": "leave_external_unmanaged",
        "provider_mutation_authorized": False,
        "accounts": clean_accounts,
        "operations": _export_rows(operations),
        "operation_items": _export_rows(operation_items),
        "execution_attempts": _export_rows(execution_attempts),
        "provider_event_bindings": _export_rows(bindings),
        "calendar_links": _export_rows(links),
        "canonical_events_with_external_effects": _export_rows(events),
        "calendar_lanes": _export_rows(lanes),
        "reset_blockers": status_blockers,
        "targets": targets,
    }
    payload["manifest_sha256"] = _sha256(
        {key: value for key, value in payload.items() if key != "generated_at"}
    )
    return payload


def _json_list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


_GOVERNANCE_RESTORE_ORDER = {
    "operator_utterances": 10,
    "gateway_lifetimes": 20,
    "intent_sessions": 30,
    "interpreted_statements": 40,
    "change_sets": 50,
    "intent_turns": 60,
    "facts": 70,
    "decisions": 80,
    "tool_invocations": 90,
    "runtime_log_entries": 100,
    "agent_responses": 110,
    "audit_events": 120,
}
_TRANSFORMED_RESTORE_FIELDS = {
    "agent_responses": {"projection_ref"},
}


def _table_columns(
    connection: psycopg.Connection[dict[str, Any]],
    table: str,
) -> dict[str, str]:
    rows = connection.execute(
        """
        SELECT column_name, data_type
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = %s
         ORDER BY ordinal_position
        """,
        (table,),
    ).fetchall()
    if not rows:
        raise RuntimeError(f"clean governance target table is missing: {table}")
    return {str(row["column_name"]): str(row["data_type"]) for row in rows}


def _adapt_value(value: object, data_type: str) -> object:
    if data_type in {"json", "jsonb"}:
        return Jsonb(value)
    return value


def _insert_mapping(
    connection: psycopg.Connection[dict[str, Any]],
    table: str,
    values: Mapping[str, Any],
) -> None:
    column_types = _table_columns(connection, table)
    unknown = sorted(set(values) - set(column_types))
    if unknown:
        raise RuntimeError(
            f"clean restore attempted unknown {table} columns: {', '.join(unknown)}"
        )
    columns = list(values)
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(column) for column in columns),
        sql.SQL(", ").join(sql.Placeholder() for _column in columns),
    )
    connection.execute(
        query,
        tuple(_adapt_value(values[column], column_types[column]) for column in columns),
    )


def _projection_for_agent_response(
    connection: psycopg.Connection[dict[str, Any]],
    row: Mapping[str, Any],
) -> str:
    projection_ref = new_public_ref("proj")
    basis_refs = _json_list(row.get("basis_refs"))
    operator_ref = "governance_restore"
    for utterance_ref in basis_refs:
        if not utterance_ref.startswith("utt_"):
            continue
        utterance = connection.execute(
            "SELECT actor_ref FROM operator_utterances WHERE ref_id = %s",
            (utterance_ref,),
        ).fetchone()
        if utterance is not None:
            operator_ref = str(utterance["actor_ref"])
            break
    visible_text = str(row["verbatim_text"])
    semantic_content = {
        "governance_restore": True,
        "pre_reset_projection_ref": row.get("projection_ref"),
    }
    _insert_mapping(
        connection,
        "operator_projections",
        {
            "id": uuid.uuid4(),
            "ref_id": projection_ref,
            "projection_kind": "agent_response",
            "operator_ref": operator_ref,
            "primary_public_ref": row["ref_id"],
            "primary_revision_ref": None,
            "supersedes_projection_ref": None,
            "intent_session_ref": row.get("intent_session_ref"),
            "case_ref": None,
            "case_revision_ref": None,
            "brief_ref": None,
            "semantic_content": semantic_content,
            "visible_text": visible_text,
            "render_schema_version": 1,
            "render_sha256": hashlib.sha256(visible_text.encode()).hexdigest(),
            "component_sha256": hashlib.sha256(b"{}").hexdigest(),
            "basis_refs": basis_refs,
            "created_at": row.get("generated_at") or row.get("submitted_at"),
        },
    )
    return projection_ref


def _clean_governance_values(
    connection: psycopg.Connection[dict[str, Any]],
    exported: Mapping[str, Any],
) -> dict[str, Any]:
    table = str(exported["table"])
    row = dict(exported["row"])
    target_columns = _table_columns(connection, table)
    values = {key: value for key, value in row.items() if key in target_columns}
    now = datetime.now(UTC)
    if "id" in target_columns:
        values.setdefault("id", uuid.uuid4())
    if table == "operator_utterances":
        values.setdefault("actor_ref", "readiness:synthetic-operator")
        values.setdefault("transport", "discord")
        values.setdefault("conversation_ref", "readiness:synthetic-conversation")
        values.setdefault("reply_to_source_ref", None)
        values.setdefault("said_at", now)
        values.setdefault("recorded_at", now)
        values.setdefault("request_key", f"readiness:{row['ref_id']}")
        values.setdefault("attachment_source_refs", [])
        values.setdefault("utterance_kind", "typed_message")
        values.setdefault("selected_option_ref", None)
        values.setdefault("projection_ref", None)
    elif table == "agent_responses":
        values["projection_ref"] = _projection_for_agent_response(connection, row)
    elif table == "decisions":
        values.setdefault("actor_ref", "readiness:synthetic-operator")
        values.setdefault("authorized_scope", "architecture_and_implementation")
        values.setdefault("implementation_authority", "authorized")
        values.setdefault("payload_json", {})
        values.setdefault("created_at", now)
    elif table == "audit_events":
        values.setdefault("entity_type", "decision")
        values.setdefault("entity_id", None)
        values.setdefault("actor_type", "operator")
        values.setdefault("actor_id", "readiness:synthetic-operator")
        values.setdefault("request_id", None)
        values.setdefault("affected_refs", [])
        values.setdefault("data", {})
        values.setdefault("created_at", now)
    elif table == "change_sets":
        values.setdefault("import_scope_json", None)
        values.setdefault("tracked_context_changes", [])
    elif table == "facts":
        hints = dict(exported.get("restore_hints", {}))
        if exported.get("restore_hints_sha256") != _sha256(hints):
            raise RuntimeError(f"fact restore hints changed for {row['ref_id']}")
        subject_ref = hints.get("subject_ref") or row.get("subject_ref")
        if not isinstance(subject_ref, str):
            raise RuntimeError(f"fact restore subject is missing for {row['ref_id']}")
        values["subject_ref"] = subject_ref
    elif table == "tool_invocations":
        values.setdefault("transport_state", "completed")
        values.setdefault("domain_state", "unknown")
        values.setdefault("trace_ref", None)
    return values


def _materialize_governance_row(
    connection: psycopg.Connection[dict[str, Any]],
    exported: Mapping[str, Any],
) -> None:
    table = str(exported["table"])
    ref_id = str(exported["ref_id"])
    row = dict(exported["row"])
    row_hash = str(exported["row_sha256"])
    if _sha256(row) != row_hash:
        raise RuntimeError(f"governance row hash mismatch for {ref_id}")
    if table not in _GOVERNANCE_RESTORE_ORDER:
        raise RuntimeError(f"no clean governance restore mapping exists for {table}")
    _insert_mapping(connection, table, _clean_governance_values(connection, exported))


def _verify_materialized_row(
    connection: psycopg.Connection[dict[str, Any]],
    exported: Mapping[str, Any],
) -> None:
    table = str(exported["table"])
    ref_id = str(exported["ref_id"])
    target_columns = _table_columns(connection, table)
    query = sql.SQL(
        "SELECT to_jsonb(restored)::text AS row_json FROM {} AS restored "
        "WHERE ref_id = %s"
    ).format(sql.Identifier(table))
    restored = connection.execute(query, (ref_id,)).fetchone()
    if restored is None:
        raise RuntimeError(f"restored governance row is missing for {ref_id}")
    actual = json.loads(str(restored["row_json"]))
    original = dict(exported["row"])
    compared_fields = (
        set(original)
        & set(target_columns)
        - _TRANSFORMED_RESTORE_FIELDS.get(table, set())
    )
    for field_name in compared_fields:
        if _sha256(actual[field_name]) != _sha256(original[field_name]):
            raise RuntimeError(
                f"restored governance field changed for {ref_id}.{field_name}"
            )


def _verify_governance(
    connection: psycopg.Connection[dict[str, Any]],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    expected_rows = list(payload["rows"])
    for exported in expected_rows:
        _verify_materialized_row(connection, exported)
    restored = len(expected_rows)

    artifacts = specification_artifact_manifest().artifacts
    signoff_count = 0
    for artifact in artifacts:
        row = connection.execute(
            """
            SELECT ref_id, basis_refs
              FROM decisions
             WHERE decision_kind = 'specification_signoff'
               AND document_ref = %s
               AND frozen_artifact_hash = %s
               AND architecture_authority IS TRUE
            """,
            (artifact.document_ref, artifact.frozen_artifact_hash),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"missing restored sign-off for {artifact.document_ref}")
        signoff_count += 1
        for utterance_ref in _json_list(row["basis_refs"]):
            if utterance_ref.startswith("utt_"):
                exists = connection.execute(
                    "SELECT 1 FROM operator_utterances WHERE ref_id = %s", (utterance_ref,)
                ).fetchone()
                if exists is None:
                    raise RuntimeError(f"missing sign-off utterance {utterance_ref}")
        audit = connection.execute(
            "SELECT 1 FROM audit_events WHERE primary_ref = %s", (row["ref_id"],)
        ).fetchone()
        if audit is None:
            raise RuntimeError(f"missing sign-off audit for {artifact.document_ref}")

    tables = {
        str(row["table_name"])
        for row in connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
        ).fetchall()
    }
    obsolete = sorted(tables & _OBSOLETE_TABLES)
    if obsolete:
        raise RuntimeError("obsolete tables exist in clean rehearsal: " + ", ".join(obsolete))
    missing_clean = sorted(_CLEAN_TABLES - tables)
    if missing_clean:
        raise RuntimeError("clean rehearsal tables are missing: " + ", ".join(missing_clean))
    revision_row = connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone()
    if revision_row is None:
        raise RuntimeError("clean rehearsal has no Alembic revision")
    return {
        "restored_rows": restored,
        "verified_signoffs": signoff_count,
        "obsolete_tables": [],
        "clean_table_count": len(_CLEAN_TABLES),
        "schema_revision": revision_row["version_num"],
    }


def _insert_rehearsal_attachment(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    payload: Mapping[str, Any],
    plaintext: bytes,
    key: bytes,
    now: datetime,
) -> str:
    attachment_ref = new_public_ref("src")
    utterance_ref = next(
        (
            str(row["ref_id"])
            for row in payload["rows"]
            if row["table"] == "operator_utterances"
        ),
        "utt_00000000000000000000000000",
    )
    utterance_row = next(
        (
            dict(row["row"])
            for row in payload["rows"]
            if row["table"] == "operator_utterances"
            and row["ref_id"] == utterance_ref
        ),
        {},
    )
    source_message_ref = str(
        utterance_row.get("source_message_ref", "readiness:synthetic")
    )
    content_hash = hashlib.sha256(plaintext).hexdigest()
    _insert_mapping(
        connection,
        "sources",
        {
            "id": uuid.uuid4(),
            "ref_id": attachment_ref,
            "source_kind": "attachment",
            "external_ref": "readiness-fixture",
            "observed_at": now,
            "content_hash": content_hash,
            "metadata_json": {"purpose": "clean_attachment_restore_rehearsal"},
            "created_at": now,
        },
    )
    _insert_mapping(
        connection,
        "attachment_evidence_metadata",
        {
            "id": uuid.uuid4(),
            "ref_id": attachment_ref,
            "transport": "discord",
            "transport_attachment_ref": "readiness-fixture",
            "source_message_ref": source_message_ref,
            "operator_utterance_ref": utterance_ref,
            "filename": "readiness-fixture.bin",
            "media_type": "application/octet-stream",
            "byte_size": len(plaintext),
            "content_hash": content_hash,
            "received_at": now,
            "recorded_at": now,
            "ingest_state": "available",
            "retention_disposition": "retained_encrypted",
            "derived_content_refs": [],
        },
    )
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, attachment_ref.encode())
    _insert_mapping(
        connection,
        "encrypted_attachment_blobs",
        {
            "id": uuid.uuid4(),
            "attachment_source_ref": attachment_ref,
            "encryption_key_ref": "readiness-fixture",
            "nonce": nonce,
            "ciphertext": ciphertext,
            "ciphertext_hash": hashlib.sha256(ciphertext).hexdigest(),
            "created_at": now,
        },
    )
    return attachment_ref


def _create_clean_database(
    *,
    database_url: str,
    payload: Mapping[str, Any],
    provider_manifest: Mapping[str, Any] | None,
    attachment_plaintext: bytes | None,
    attachment_key: bytes | None,
    destination_kind: str = "rehearsal",
) -> dict[str, Any]:
    if destination_kind == "rehearsal":
        require_rehearsal_database(database_url, suffix="_rehearsal")
    elif destination_kind == "cutover":
        require_cutover_database(database_url)
    else:
        raise ValueError(f"unsupported clean destination kind: {destination_kind}")
    with psycopg.connect(_psycopg_url(database_url), row_factory=dict_row) as connection:
        existing = connection.execute(
            "SELECT count(*) AS total FROM information_schema.tables "
            "WHERE table_schema='public'"
        ).fetchone()
        if existing is None or int(existing["total"]) != 0:
            raise RuntimeError("clean rehearsal database must start empty")
    repository_root = Path(__file__).resolve().parents[2]
    migration_environment = os.environ.copy()
    migration_environment["DOCKET_DATABASE_URL"] = database_url
    subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(repository_root / "alembic.ini"),
            "upgrade",
            "head",
        ],
        check=True,
        cwd=repository_root,
        env=migration_environment,
    )
    with psycopg.connect(_psycopg_url(database_url), row_factory=dict_row) as connection:
        ordered_rows = sorted(
            payload["rows"],
            key=lambda row: (
                _GOVERNANCE_RESTORE_ORDER.get(str(row["table"]), 10_000),
                str(row["ref_id"]),
            ),
        )
        for row in ordered_rows:
            _materialize_governance_row(connection, row)
        provider_accounts = (
            list(provider_manifest.get("accounts", []))
            if provider_manifest is not None
            else []
        )
        now = datetime.now(UTC)
        for account in provider_accounts:
            _insert_mapping(
                connection,
                "provider_accounts",
                {
                    "id": uuid.uuid4(),
                    "ref_id": account["clean_account_ref"],
                    "provider": account["provider"],
                    "external_account_id": account["external_account_id"],
                    "display_name": account.get("display_name"),
                    "email_address": account.get("email_address"),
                    "capabilities": account.get("capabilities", []),
                    "enabled": account.get("enabled", True),
                    "credential_ref": account.get("credential_ref"),
                    "created_at": now,
                    "updated_at": now,
                },
            )

        attachment_ref: str | None = None
        if attachment_plaintext is not None or attachment_key is not None:
            if attachment_plaintext is None or attachment_key is None:
                raise ValueError("attachment rehearsal requires plaintext and key together")
            attachment_ref = _insert_rehearsal_attachment(
                connection,
                payload=payload,
                plaintext=attachment_plaintext,
                key=attachment_key,
                now=now,
            )
        verification = _verify_governance(connection, payload)
        restored_account_row = connection.execute(
            "SELECT count(*) FROM provider_accounts"
        ).fetchone()
        restored_accounts = (
            int(restored_account_row["count"]) if restored_account_row is not None else -1
        )
        if restored_accounts != len(provider_accounts):
            raise RuntimeError("clean ProviderAccount count changed during restore")
    return {
        **verification,
        "attachment_ref": attachment_ref,
        "restored_provider_accounts": restored_accounts,
    }


def materialize_clean_cutover_database(
    *,
    database_url: str,
    payload: Mapping[str, Any],
    provider_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    _validate_closure_payload(payload)
    _validate_provider_manifest(provider_manifest)
    result = _create_clean_database(
        database_url=database_url,
        payload=payload,
        provider_manifest=provider_manifest,
        attachment_plaintext=None,
        attachment_key=None,
        destination_kind="cutover",
    )
    if result.get("attachment_ref") is not None:
        raise AssertionError("production clean materialization created rehearsal evidence")
    return {key: value for key, value in result.items() if key != "attachment_ref"}


def _verify_attachment_restore(
    *,
    database_url: str,
    attachment_ref: str,
    attachment_key: bytes,
    expected_plaintext_sha256: str,
) -> dict[str, Any]:
    require_rehearsal_database(database_url, suffix="_restore")
    with psycopg.connect(_psycopg_url(database_url), row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT metadata.content_hash, metadata.byte_size,
                   blob.nonce, blob.ciphertext, blob.ciphertext_hash
              FROM attachment_evidence_metadata metadata
              JOIN encrypted_attachment_blobs blob
                ON blob.attachment_source_ref = metadata.ref_id
             WHERE metadata.ref_id = %s
            """,
            (attachment_ref,),
        ).fetchone()
        if row is None:
            raise RuntimeError("attachment fixture was not restored")
        ciphertext = bytes(row["ciphertext"])
        if hashlib.sha256(ciphertext).hexdigest() != row["ciphertext_hash"]:
            raise RuntimeError("restored attachment ciphertext hash mismatch")
        plaintext = AESGCM(attachment_key).decrypt(
            bytes(row["nonce"]), ciphertext, attachment_ref.encode()
        )
        if hashlib.sha256(plaintext).hexdigest() != expected_plaintext_sha256:
            raise RuntimeError("restored attachment plaintext hash mismatch")
        return {
            "attachment_ref": attachment_ref,
            "byte_size": row["byte_size"],
            "plaintext_sha256": expected_plaintext_sha256,
            "ciphertext_sha256": row["ciphertext_hash"],
        }


def rehearse_backup_restore(
    *,
    payload: Mapping[str, Any],
    provider_manifest: Mapping[str, Any] | None,
    rehearsal_url: str,
    restore_url: str,
    backup_path: Path,
) -> dict[str, Any]:
    _validate_closure_payload(payload)
    if provider_manifest is not None:
        _validate_provider_manifest(provider_manifest)
    if _psycopg_url(rehearsal_url) == _psycopg_url(restore_url):
        raise ValueError("rehearsal and restore databases must be distinct")
    require_rehearsal_database(rehearsal_url, suffix="_rehearsal")
    require_rehearsal_database(restore_url, suffix="_restore")
    plaintext = b"Docket tracked-context encrypted attachment restore fixture\n"
    plaintext_sha = hashlib.sha256(plaintext).hexdigest()
    key = AESGCM.generate_key(bit_length=256)
    clean = _create_clean_database(
        database_url=rehearsal_url,
        payload=payload,
        provider_manifest=provider_manifest,
        attachment_plaintext=plaintext,
        attachment_key=key,
    )
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--file",
            str(backup_path),
            "--dbname",
            _psycopg_url(rehearsal_url),
        ],
        check=True,
    )
    os.chmod(backup_path, 0o600)
    subprocess.run(
        [
            "pg_restore",
            "--no-owner",
            "--exit-on-error",
            "--dbname",
            _psycopg_url(restore_url),
            str(backup_path),
        ],
        check=True,
    )
    with psycopg.connect(_psycopg_url(restore_url), row_factory=dict_row) as connection:
        restored_governance = _verify_governance(connection, payload)
        restored_account_row = connection.execute(
            "SELECT count(*) FROM provider_accounts"
        ).fetchone()
        restored_accounts = (
            int(restored_account_row["count"]) if restored_account_row is not None else -1
        )
    expected_accounts = (
        len(provider_manifest.get("accounts", []))
        if provider_manifest is not None
        else 0
    )
    if restored_accounts != expected_accounts:
        raise RuntimeError("ProviderAccount count changed during backup restore")
    restored_governance["restored_provider_accounts"] = restored_accounts
    attachment = _verify_attachment_restore(
        database_url=restore_url,
        attachment_ref=str(clean["attachment_ref"]),
        attachment_key=key,
        expected_plaintext_sha256=plaintext_sha,
    )
    evidence = {
        "schema_version": 1,
        "document_ref": "ONT-DELTA-2026-08-29-TRACKED-CONTEXT",
        "rehearsed_at": datetime.now(UTC).isoformat(),
        "closure_sha256": payload["closure_sha256"],
        "provider_manifest_sha256": (
            provider_manifest.get("manifest_sha256")
            if provider_manifest is not None
            else None
        ),
        "backup_sha256": hashlib.sha256(backup_path.read_bytes()).hexdigest(),
        "governance": restored_governance,
        "attachment": attachment,
        "production_state_changed": False,
        "provider_mutation_performed": False,
    }
    evidence["evidence_sha256"] = _sha256(
        {key: value for key, value in evidence.items() if key != "rehearsed_at"}
    )
    return evidence


def _write_private_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        json.dumps(
            payload,
            default=_json_default,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    os.chmod(path, 0o600)


def _validate_closure_payload(payload: Mapping[str, Any]) -> None:
    expected_hash = _sha256(
        {
            key: value
            for key, value in payload.items()
            if key not in {"generated_at", "closure_sha256"}
        }
    )
    if payload.get("closure_sha256") != expected_hash:
        raise RuntimeError("governance closure manifest hash mismatch")
    rows = list(payload.get("rows", []))
    if payload.get("row_count") != len(rows):
        raise RuntimeError("governance closure row count does not match manifest")
    identities: set[tuple[str, str]] = set()
    for exported in rows:
        identity = (str(exported.get("table")), str(exported.get("ref_id")))
        if identity in identities:
            raise RuntimeError(f"governance closure repeats row {identity[1]}")
        identities.add(identity)
        if exported.get("row_sha256") != _sha256(exported.get("row")):
            raise RuntimeError(
                f"governance closure row hash mismatch for {identity[1]}"
            )
        if "restore_hints" in exported and exported.get(
            "restore_hints_sha256"
        ) != _sha256(exported["restore_hints"]):
            raise RuntimeError(
                f"governance closure restore hint hash mismatch for {identity[1]}"
            )


def _validate_provider_manifest(payload: Mapping[str, Any]) -> None:
    expected_hash = _sha256(
        {
            key: value
            for key, value in payload.items()
            if key not in {"generated_at", "manifest_sha256"}
        }
    )
    if payload.get("manifest_sha256") != expected_hash:
        raise RuntimeError("provider disposition manifest hash mismatch")
    if payload.get("provider_mutation_authorized") is not False:
        raise RuntimeError("readiness manifest must not authorize provider mutation")
    targets = list(payload.get("targets", []))
    target_hashes = [target.get("target_sha256") for target in targets]
    if len(set(target_hashes)) != len(target_hashes):
        raise RuntimeError("provider disposition manifest contains duplicate targets")
    if any(
        target.get("disposition") not in _ALLOWED_PROVIDER_DISPOSITIONS
        for target in targets
    ):
        raise RuntimeError("provider disposition manifest has an invalid disposition")
    blockers = payload.get("reset_blockers")
    if not isinstance(blockers, Mapping) or any(blockers.values()):
        raise RuntimeError("provider execution state is not reset-ready")


def _synthetic_payload() -> dict[str, Any]:
    rows: list[ClosureRow] = []
    seed_refs: list[str] = []
    for artifact in specification_artifact_manifest().artifacts:
        utterance_ref = new_public_ref("utt")
        decision_ref = new_public_ref("dec")
        audit_ref = new_public_ref("aud")
        seed_refs.append(decision_ref)
        rows.extend(
            (
                ClosureRow(
                    "operator_utterances",
                    utterance_ref,
                    {
                        "ref_id": utterance_ref,
                        "content_hash": hashlib.sha256(
                            artifact.signoff_text.encode()
                        ).hexdigest(),
                        "source_message_ref": f"synthetic:{artifact.document_ref}",
                        "verbatim_text": artifact.signoff_text,
                    },
                ),
                ClosureRow(
                    "decisions",
                    decision_ref,
                    {
                        "ref_id": decision_ref,
                        "decision_kind": "specification_signoff",
                        "document_ref": artifact.document_ref,
                        "frozen_artifact_hash": artifact.frozen_artifact_hash,
                        "architecture_authority": True,
                        "basis_refs": [utterance_ref],
                    },
                ),
                ClosureRow(
                    "audit_events",
                    audit_ref,
                    {
                        "ref_id": audit_ref,
                        "event_type": "decision.specification_signoff_recorded",
                        "primary_ref": decision_ref,
                        "basis_refs": [utterance_ref],
                    },
                ),
            )
        )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "document_ref": "ONT-DELTA-2026-08-29-TRACKED-CONTEXT",
        "generated_at": datetime.now(UTC).isoformat(),
        "seed_refs": seed_refs,
        "row_count": len(rows),
        "rows": [row.export() for row in rows],
    }
    payload["closure_sha256"] = _sha256(
        {key: value for key, value in payload.items() if key != "generated_at"}
    )
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tracked-context offline readiness tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export-governance")
    export_parser.add_argument("--database-url", required=True)
    export_parser.add_argument("--seed-file", action="append", type=Path, default=[])
    export_parser.add_argument("--seed-ref", action="append", default=[])
    export_parser.add_argument("--output", type=Path, required=True)

    provider_parser = subparsers.add_parser("provider-inventory")
    provider_parser.add_argument("--database-url", required=True)
    provider_parser.add_argument("--output", type=Path, required=True)

    rehearse_parser = subparsers.add_parser("rehearse")
    rehearse_parser.add_argument("--closure", type=Path, required=True)
    rehearse_parser.add_argument("--provider-manifest", type=Path, required=True)
    rehearse_parser.add_argument("--rehearsal-url", required=True)
    rehearse_parser.add_argument("--restore-url", required=True)
    rehearse_parser.add_argument("--backup", type=Path, required=True)
    rehearse_parser.add_argument("--output", type=Path, required=True)

    synthetic_parser = subparsers.add_parser("synthetic-rehearsal")
    synthetic_parser.add_argument("--rehearsal-url", required=True)
    synthetic_parser.add_argument("--restore-url", required=True)
    synthetic_parser.add_argument("--backup", type=Path, required=True)
    synthetic_parser.add_argument("--output", type=Path, required=True)

    materialize_parser = subparsers.add_parser("materialize-clean")
    materialize_parser.add_argument("--closure", type=Path, required=True)
    materialize_parser.add_argument("--provider-manifest", type=Path, required=True)
    materialize_parser.add_argument("--database-url", required=True)
    materialize_parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "export-governance":
        payload = export_governance_closure(
            database_url=args.database_url,
            seed_files=args.seed_file,
            extra_seed_refs=args.seed_ref,
        )
        _write_private_json(args.output, payload)
        print(
            json.dumps(
                {
                    "closure_sha256": payload["closure_sha256"],
                    "rows": payload["row_count"],
                }
            )
        )
    elif args.command == "provider-inventory":
        payload = provider_disposition_inventory(database_url=args.database_url)
        _write_private_json(args.output, payload)
        print(
            json.dumps(
                {
                    "manifest_sha256": payload["manifest_sha256"],
                    "targets": len(payload["targets"]),
                }
            )
        )
    elif args.command in {"rehearse", "synthetic-rehearsal"}:
        payload = (
            json.loads(args.closure.read_text(encoding="utf-8"))
            if args.command == "rehearse"
            else _synthetic_payload()
        )
        provider_manifest = (
            json.loads(args.provider_manifest.read_text(encoding="utf-8"))
            if args.command == "rehearse"
            else None
        )
        evidence = rehearse_backup_restore(
            payload=payload,
            provider_manifest=provider_manifest,
            rehearsal_url=args.rehearsal_url,
            restore_url=args.restore_url,
            backup_path=args.backup,
        )
        _write_private_json(args.output, evidence)
        print(
            json.dumps(
                {
                    "evidence_sha256": evidence["evidence_sha256"],
                    "governance": evidence["governance"],
                }
            )
        )
    elif args.command == "materialize-clean":
        verification = materialize_clean_cutover_database(
            database_url=args.database_url,
            payload=json.loads(args.closure.read_text(encoding="utf-8")),
            provider_manifest=json.loads(
                args.provider_manifest.read_text(encoding="utf-8")
            ),
        )
        _write_private_json(args.output, verification)
        print(json.dumps(verification, sort_keys=True))


if __name__ == "__main__":
    main()
