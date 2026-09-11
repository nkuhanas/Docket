from docket.domain.public_refs import PUBLIC_REF_PREFIXES
from docket.specification_artifacts import (
    specification_artifact,
    specification_artifact_manifest,
)


def test_specification_artifact_manifest_is_unique_and_packaged() -> None:
    manifest = specification_artifact_manifest()

    assert manifest.schema_version == 2
    assert [item.document_ref for item in manifest.artifacts] == [
        "ONT-DELTA-2026-08-27",
        "ONT-DELTA-2026-08-28-CASE-RESOLUTION",
        "ONT-DELTA-2026-08-28-INTERACTIVE-CONTINUITY",
        "ONT-DELTA-2026-08-29-TRACKED-CONTEXT",
        "ONT-DELTA-2026-09-06-INCREMENTAL-CHANGESET-ASSEMBLY",
        "ONT-DELTA-2026-09-11-INTERACTION-CORRECTION",
    ]
    assert all(item.document_ref in item.signoff_text for item in manifest.artifacts)
    assert all(item.frozen_artifact_hash in item.signoff_text for item in manifest.artifacts)
    assert all(item.production_reset_authority is False for item in manifest.artifacts)


def test_case_resolution_candidate_retains_bootstrap_evidence() -> None:
    document_ref = "ONT-DELTA-2026-08-28-CASE-RESOLUTION"
    frozen_hash = "058788ec6728565b51bbce3e80d51146c52fec0c0364f7599e3877f97d964a05"

    artifact = specification_artifact(document_ref, frozen_hash)

    assert artifact is not None
    assert artifact.status == "candidate_spec"
    assert artifact.implementation_authority == "amendment_scope"
    assert artifact.bootstrap_authority is not None
    assert artifact.bootstrap_authority.utterance_ref == "utt_01M157G81T7FV6A4V8RQD54Z6G"
    assert specification_artifact(document_ref, "0" * 64) is None
    assert specification_artifact("ONT-DELTA-UNKNOWN", frozen_hash) is None


def test_later_amendment_uses_existing_ledger_signoff_as_its_prerequisite() -> None:
    document_ref = "ONT-DELTA-2026-08-28-INTERACTIVE-CONTINUITY"
    frozen_hash = "972784149dd2a219d027684a76f04fac37d8147e9656a3ff06326d883fd06579"

    artifact = specification_artifact(document_ref, frozen_hash)

    assert artifact is not None
    assert artifact.status == "candidate_spec"
    assert artifact.implementation_authority == "amendment_scope"
    assert artifact.authorized_scope == (
        "interactive_authority_continuity_and_deployment_drain_amendment"
    )
    assert artifact.prerequisites[0].decision_kind == "specification_signoff"
    assert artifact.prerequisites[0].document_ref == "ONT-DELTA-2026-08-27"
    assert artifact.bootstrap_authority is None


def test_tracked_context_candidate_requires_exact_specification_dag() -> None:
    document_ref = "ONT-DELTA-2026-08-29-TRACKED-CONTEXT"
    frozen_hash = "830c33c9d78485a6a6a8f872b6dfad996869f8a7eaea9a5f7d39d52e9357cf48"

    artifact = specification_artifact(document_ref, frozen_hash)

    assert artifact is not None
    assert artifact.status == "frozen_candidate"
    assert artifact.authorized_scope == (
        "namespace_cleanup_tracked_context_temporal_task_attachment_import_"
        "attention_admission_and_reset_support"
    )
    assert artifact.production_reset_authority is False
    assert [item.decision_ref for item in artifact.prerequisites] == [
        "dec_01M13MANM19BX22EW8QC8AH9DT",
        "dec_01M1587SE1JX3BVQ1QZBQKX6T7",
        "dec_01M15EHKNXVKRBM7MZ3FN39X3E",
    ]
    assert all(
        item.decision_kind == "specification_signoff" for item in artifact.prerequisites
    )
    assert all(item.architecture_authority is True for item in artifact.prerequisites)


def test_incremental_changeset_candidate_requires_exact_specification_dag() -> None:
    document_ref = "ONT-DELTA-2026-09-06-INCREMENTAL-CHANGESET-ASSEMBLY"
    frozen_hash = "0557d095d8c4d166f4f3f8a47d58247842649bbfbfc4f696223e065754d858d7"

    artifact = specification_artifact(document_ref, frozen_hash)

    assert artifact is not None
    assert artifact.status == "frozen_candidate"
    assert artifact.implementation_authority == "amendment_scope"
    assert artifact.authorized_scope == (
        "incremental_changeset_assembly_concurrency_idempotency_and_compilation"
    )
    assert artifact.production_reset_authority is False
    assert artifact.bootstrap_authority is None
    assert [item.decision_ref for item in artifact.prerequisites] == [
        "dec_01M13MANM19BX22EW8QC8AH9DT",
        "dec_01M1587SE1JX3BVQ1QZBQKX6T7",
        "dec_01M15EHKNXVKRBM7MZ3FN39X3E",
        "dec_01M18DYEYJVVJ7TW5VQQBCA6NC",
    ]
    assert [item.document_ref for item in artifact.prerequisites] == [
        "ONT-DELTA-2026-08-27",
        "ONT-DELTA-2026-08-28-CASE-RESOLUTION",
        "ONT-DELTA-2026-08-28-INTERACTIVE-CONTINUITY",
        "ONT-DELTA-2026-08-29-TRACKED-CONTEXT",
    ]
    assert all(
        item.decision_kind == "specification_signoff" for item in artifact.prerequisites
    )
    assert all(item.architecture_authority is True for item in artifact.prerequisites)


def test_interaction_correction_binds_five_prerequisites_without_reset_authority() -> None:
    artifact = specification_artifact(
        "ONT-DELTA-2026-09-11-INTERACTION-CORRECTION",
        "39ca596f2ff00e70cad28fe6e3750f284efde53d4db47d8490531d61ae9fd23a",
    )
    assert artifact is not None
    assert artifact.status == "frozen_candidate"
    assert artifact.implementation_authority == "amendment_scope"
    assert artifact.authorized_scope == (
        "scoped_interaction_occurrence_safety_staging_repair_"
        "instruction_control_and_delivery_observability"
    )
    assert artifact.bootstrap_authority is None
    assert artifact.production_reset_authority is False
    assert [item.decision_ref for item in artifact.prerequisites] == [
        "dec_01M13MANM19BX22EW8QC8AH9DT",
        "dec_01M1587SE1JX3BVQ1QZBQKX6T7",
        "dec_01M15EHKNXVKRBM7MZ3FN39X3E",
        "dec_01M18DYEYJVVJ7TW5VQQBCA6NC",
        "dec_01M1W7648P7YJZ22GRD114WSBV",
    ]
    for prerequisite in artifact.prerequisites:
        prior = specification_artifact(
            prerequisite.document_ref, prerequisite.frozen_artifact_hash
        )
        assert prior is not None
        assert prerequisite.decision_kind == "specification_signoff"
        assert prerequisite.architecture_authority is True


def test_signed_implementation_activates_only_the_clean_namespace() -> None:
    assert {
        "acct",
        "bentry",
        "citem",
        "conf",
        "item",
        "opt",
        "rem",
        "sattempt",
        "task",
        "time",
        "tproj",
        "trace",
    } <= PUBLIC_REF_PREFIXES
    assert PUBLIC_REF_PREFIXES.isdisjoint({"cnf", "dproj", "itm", "lease", "prompt", "satt"})
