from pathlib import Path

import pytest

from docket.domain.public_refs import new_public_ref
from docket.tracked_context_readiness import (
    _ALLOWED_PROVIDER_DISPOSITIONS,
    _CLEAN_TABLES,
    _OBSOLETE_TABLES,
    ClosureRow,
    _new_clean_rehearsal_ref,
    _seed_refs_from_files,
    _sha256,
    _synthetic_payload,
    _validate_closure_payload,
    _validate_provider_manifest,
    compute_governance_closure,
    require_cutover_database,
    require_rehearsal_database,
)


def test_governance_closure_follows_basis_and_bounded_reverse_evidence() -> None:
    utterance_ref = new_public_ref("utt")
    decision_ref = new_public_ref("dec")
    audit_ref = new_public_ref("aud")
    response_ref = new_public_ref("rsp")
    unrelated_ref = new_public_ref("evt")
    rows = [
        ClosureRow(
            "operator_utterances",
            utterance_ref,
            {"ref_id": utterance_ref, "verbatim_text": "signed"},
        ),
        ClosureRow(
            "decisions",
            decision_ref,
            {"ref_id": decision_ref, "basis_refs": [utterance_ref]},
        ),
        ClosureRow(
            "audit_events",
            audit_ref,
            {"ref_id": audit_ref, "primary_ref": decision_ref},
        ),
        ClosureRow(
            "agent_responses",
            response_ref,
            {"ref_id": response_ref, "basis_refs": [utterance_ref]},
        ),
        ClosureRow(
            "canonical_events",
            unrelated_ref,
            {"ref_id": unrelated_ref, "basis_refs": [utterance_ref]},
        ),
    ]

    closure, missing = compute_governance_closure(rows, seed_refs=[decision_ref])

    assert missing == []
    assert {row.ref_id for row in closure} == {
        utterance_ref,
        decision_ref,
        audit_ref,
        response_ref,
    }


def test_governance_closure_reports_missing_typed_edges() -> None:
    decision_ref = new_public_ref("dec")
    missing_utterance_ref = new_public_ref("utt")
    rows = [
        ClosureRow(
            "decisions",
            decision_ref,
            {"ref_id": decision_ref, "basis_refs": [missing_utterance_ref]},
        )
    ]

    _closure, missing = compute_governance_closure(rows, seed_refs=[decision_ref])

    assert missing == [missing_utterance_ref]


def test_governance_closure_does_not_expand_shared_runtime_or_domain_results() -> None:
    utterance_ref = new_public_ref("utt")
    unrelated_utterance_ref = new_public_ref("utt")
    decision_ref = new_public_ref("dec")
    response_ref = new_public_ref("rsp")
    gateway_ref = new_public_ref("gwy")
    call_ref = new_public_ref("call")
    unrelated_call_ref = new_public_ref("call")
    semantic_request_ref = new_public_ref("sreq")
    change_set_ref = new_public_ref("chg")
    case_revision_ref = new_public_ref("caserev")
    rows = [
        ClosureRow(
            "operator_utterances",
            utterance_ref,
            {"ref_id": utterance_ref, "verbatim_text": "signed"},
        ),
        ClosureRow(
            "operator_utterances",
            unrelated_utterance_ref,
            {"ref_id": unrelated_utterance_ref, "verbatim_text": "domain command"},
        ),
        ClosureRow(
            "decisions",
            decision_ref,
            {"ref_id": decision_ref, "basis_refs": [utterance_ref]},
        ),
        ClosureRow(
            "agent_responses",
            response_ref,
            {
                "ref_id": response_ref,
                "basis_refs": [utterance_ref],
                "gateway_instance_ref": gateway_ref,
            },
        ),
        ClosureRow(
            "gateway_lifetimes",
            gateway_ref,
            {"ref_id": gateway_ref},
        ),
        ClosureRow(
            "tool_invocations",
            call_ref,
            {
                "ref_id": call_ref,
                "utterance_refs": [utterance_ref],
                "gateway_instance_ref": gateway_ref,
                "semantic_request_ref": semantic_request_ref,
                "result_refs": [change_set_ref],
            },
        ),
        ClosureRow(
            "tool_invocations",
            unrelated_call_ref,
            {
                "ref_id": unrelated_call_ref,
                "utterance_refs": [unrelated_utterance_ref],
                "gateway_instance_ref": gateway_ref,
            },
        ),
        ClosureRow(
            "semantic_requests",
            semantic_request_ref,
            {"ref_id": semantic_request_ref, "basis_refs": [utterance_ref]},
        ),
        ClosureRow(
            "change_sets",
            change_set_ref,
            {"ref_id": change_set_ref, "basis_refs": [case_revision_ref]},
        ),
        ClosureRow(
            "attention_case_revisions",
            case_revision_ref,
            {"ref_id": case_revision_ref},
        ),
    ]

    closure, missing = compute_governance_closure(rows, seed_refs=[decision_ref])

    assert missing == []
    assert {row.ref_id for row in closure} == {
        utterance_ref,
        decision_ref,
        response_ref,
        gateway_ref,
        call_ref,
    }


def test_readiness_file_seeds_only_frozen_governance_categories(tmp_path: Path) -> None:
    utterance_ref = new_public_ref("utt")
    audit_ref = new_public_ref("aud")
    event_ref = new_public_ref("evt")
    status = tmp_path / "readiness.yaml"
    status.write_text(
        f"utterance: {utterance_ref}\naudit: {audit_ref}\nevent: {event_ref}\n",
        encoding="utf-8",
    )

    assert _seed_refs_from_files([status]) == {utterance_ref, audit_ref}


def test_synthetic_closure_is_hash_bound_and_covers_every_artifact() -> None:
    payload = _synthetic_payload()

    assert payload["row_count"] == len(payload["rows"])
    assert len(payload["seed_refs"]) == 4
    assert payload["closure_sha256"] == _sha256(
        {
            key: value
            for key, value in payload.items()
            if key not in {"generated_at", "closure_sha256"}
        }
    )
    for row in payload["rows"]:
        assert row["row_sha256"] == _sha256(row["row"])
    _validate_closure_payload(payload)


def test_provider_manifest_requires_safe_complete_reset_state() -> None:
    payload = {
        "schema_version": 1,
        "provider_mutation_authorized": False,
        "reset_blockers": {
            "operations": [],
            "operation_items": [],
            "execution_attempts": [],
        },
        "targets": [
            {
                "target_sha256": "a" * 64,
                "disposition": "leave_external_unmanaged",
            }
        ],
    }
    payload["manifest_sha256"] = _sha256(payload)

    _validate_provider_manifest(payload)

    payload["reset_blockers"]["operations"].append("op_blocked")
    payload["manifest_sha256"] = _sha256(
        {key: value for key, value in payload.items() if key != "manifest_sha256"}
    )
    with pytest.raises(RuntimeError, match="not reset-ready"):
        _validate_provider_manifest(payload)


def test_rehearsal_database_guard_rejects_non_rehearsal_targets() -> None:
    require_rehearsal_database(
        "postgresql+psycopg://docket:test@postgres/docket_readiness_rehearsal",
        suffix="_rehearsal",
    )
    with pytest.raises(ValueError, match="must end"):
        require_rehearsal_database(
            "postgresql+psycopg://docket:test@postgres/docket",
            suffix="_rehearsal",
        )


def test_cutover_database_guard_requires_disposable_namespace() -> None:
    require_cutover_database(
        "postgresql+psycopg://docket:test@postgres/docket_cutover_20260830"
    )
    require_cutover_database(
        "postgresql+psycopg://docket:test@postgres/"
        "docket_cutover_beba77ce5c7e_20260831T005136Z"
    )
    with pytest.raises(ValueError, match="docket_cutover_"):
        require_cutover_database(
            "postgresql+psycopg://docket:test@postgres/docket"
        )


def test_clean_rehearsal_refs_do_not_activate_live_prefixes() -> None:
    assert _new_clean_rehearsal_ref("acct").startswith("acct_")


def test_clean_rehearsal_uses_actual_clean_model_metadata() -> None:
    for table in _OBSOLETE_TABLES:
        assert table not in _CLEAN_TABLES
    assert "items" in _CLEAN_TABLES
    assert "temporal_bindings" in _CLEAN_TABLES
    assert "tasks" in _CLEAN_TABLES
    assert "attachment_evidence_metadata" in _CLEAN_TABLES
    assert "encrypted_attachment_blobs" in _CLEAN_TABLES


def test_provider_inventory_has_one_safe_nonmutating_default() -> None:
    assert {
        "leave_external_unmanaged",
        "adopt_into_clean_model",
        "delete_by_separately_authorized_provider_operation",
    } == _ALLOWED_PROVIDER_DISPOSITIONS


def test_readiness_evidence_paths_remain_outside_source_tree() -> None:
    for value in (
        "backups/tracked-context-governance-closure.json",
        "backups/tracked-context-provider-dispositions.json",
        "backups/tracked-context-rehearsal-evidence.json",
    ):
        assert Path(value).parts[0] == "backups"
