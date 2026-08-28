from docket.specification_artifacts import (
    specification_artifact,
    specification_artifact_manifest,
)


def test_specification_artifact_manifest_is_unique_and_packaged() -> None:
    manifest = specification_artifact_manifest()

    assert manifest.schema_version == 1
    assert [item.document_ref for item in manifest.artifacts] == [
        "ONT-DELTA-2026-08-27",
        "ONT-DELTA-2026-08-28-CASE-RESOLUTION",
    ]
    assert all(item.document_ref in item.signoff_text for item in manifest.artifacts)
    assert all(item.frozen_artifact_hash in item.signoff_text for item in manifest.artifacts)


def test_candidate_artifact_requires_exact_binding_and_bootstrap_evidence() -> None:
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
