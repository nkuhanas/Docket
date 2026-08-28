from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DecisionPrerequisite(_ManifestModel):
    decision_kind: str = Field(min_length=1, max_length=128)
    document_ref: str = Field(pattern=r"^ONT-DELTA-[A-Z0-9-]+$")
    frozen_artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class BootstrapAuthority(_ManifestModel):
    utterance_ref: str = Field(pattern=r"^utt_[0-9A-HJKMNP-TV-Z]{26}$")
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SpecificationArtifact(_ManifestModel):
    document_ref: str = Field(pattern=r"^ONT-DELTA-[A-Z0-9-]+$")
    frozen_artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["signed_architecture", "candidate_spec"]
    signoff_text: str = Field(min_length=1, max_length=1000)
    architecture_authority: bool
    implementation_authority: str = Field(min_length=1, max_length=128)
    authorized_scope: str | None = Field(default=None, min_length=1, max_length=128)
    prerequisite: DecisionPrerequisite
    bootstrap_authority: BootstrapAuthority | None = None

    @model_validator(mode="after")
    def signoff_names_exact_artifact(self) -> SpecificationArtifact:
        if (
            self.document_ref not in self.signoff_text
            or self.frozen_artifact_hash not in self.signoff_text
        ):
            raise ValueError("signoff_text must name the exact document ref and hash")
        if self.status == "candidate_spec" and self.bootstrap_authority is None:
            raise ValueError("candidate specifications require bootstrap authority evidence")
        return self


class SpecificationArtifactManifest(_ManifestModel):
    schema_version: Literal[1]
    artifacts: tuple[SpecificationArtifact, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def artifact_bindings_are_unique(self) -> SpecificationArtifactManifest:
        refs = [artifact.document_ref for artifact in self.artifacts]
        bindings = [
            (artifact.document_ref, artifact.frozen_artifact_hash)
            for artifact in self.artifacts
        ]
        if len(refs) != len(set(refs)) or len(bindings) != len(set(bindings)):
            raise ValueError("specification artifact bindings must be unique")
        return self


@lru_cache(maxsize=1)
def specification_artifact_manifest() -> SpecificationArtifactManifest:
    resource = files("docket").joinpath("specification_artifacts.json")
    return SpecificationArtifactManifest.model_validate_json(resource.read_text(encoding="utf-8"))


def specification_artifact(
    document_ref: str,
    frozen_artifact_hash: str,
) -> SpecificationArtifact | None:
    return next(
        (
            artifact
            for artifact in specification_artifact_manifest().artifacts
            if artifact.document_ref == document_ref
            and artifact.frozen_artifact_hash == frozen_artifact_hash
        ),
        None,
    )
