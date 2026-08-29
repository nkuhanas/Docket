from __future__ import annotations

import copy
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from docket.domain.canonical import sha256_json
from docket.models import (
    DeferredIngress,
    IntentSession,
    OutboxEvent,
    PersistedSemanticOption,
    SemanticPromptProjection,
)
from docket.providers.discord import DiscordProjectionAdapter

_QUIESCED = "ingress_deployment_quiesced"


class IngressDeploymentService:
    """Quiesce and regenerate semantic controls around an ingress-only rollout."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        adapter: DiscordProjectionAdapter,
    ) -> None:
        self.session_factory = session_factory
        self.adapter = adapter

    def quiesce(self) -> dict[str, Any]:
        with self.session_factory() as session:
            rows = list(
                session.execute(
                    select(
                        SemanticPromptProjection.id,
                        SemanticPromptProjection.ref_id,
                        SemanticPromptProjection.projection_version,
                        SemanticPromptProjection.channel_id,
                        SemanticPromptProjection.message_id,
                    )
                    .where(
                        SemanticPromptProjection.status == "delivered",
                        SemanticPromptProjection.message_id.is_not(None),
                    )
                    .order_by(SemanticPromptProjection.created_at)
                )
            )
        quiesced_refs: list[str] = []
        for projection_id, projection_ref, version, channel_id, message_id in rows:
            request = {
                "request_id": str(uuid.uuid4()),
                "projection_id": str(projection_id),
                "projection_ref": projection_ref,
                "projection_version": version,
                "channel_id": channel_id,
                "message_id": message_id,
            }
            acknowledgement = self.adapter.quiesce_semantic_prompt(projection_id, request)
            if not all(
                acknowledgement.get(field) == request[field]
                for field in (
                    "request_id",
                    "projection_id",
                    "projection_ref",
                    "projection_version",
                    "message_id",
                )
            ) or acknowledgement.get("quiesced") is not True:
                raise RuntimeError(
                    "Discord returned an invalid semantic quiescence acknowledgement"
                )
            with self.session_factory.begin() as session:
                projection = session.get(
                    SemanticPromptProjection,
                    projection_id,
                    with_for_update=True,
                )
                if projection is None:
                    raise RuntimeError("quiesced semantic projection disappeared")
                if projection.status == "delivered":
                    projection.status = "superseded"
                    projection.last_error_code = _QUIESCED
                elif not (
                    projection.status == "superseded"
                    and projection.last_error_code == _QUIESCED
                ):
                    raise RuntimeError("semantic projection changed during ingress quiescence")
            quiesced_refs.append(projection_ref)
        return {
            "ok": True,
            "state": "quiesced",
            "count": len(quiesced_refs),
            "projection_refs": quiesced_refs,
        }

    def regenerate(self) -> dict[str, Any]:
        regenerated_refs: list[str] = []
        skipped_refs: list[str] = []
        with self.session_factory.begin() as session:
            projections = list(
                session.scalars(
                    select(SemanticPromptProjection)
                    .where(
                        SemanticPromptProjection.status == "superseded",
                        SemanticPromptProjection.last_error_code == _QUIESCED,
                    )
                    .order_by(SemanticPromptProjection.created_at)
                    .with_for_update()
                )
            )
            pending_bindings = [
                row.selected_option_binding_json or {}
                for row in session.scalars(
                    select(DeferredIngress).where(
                        DeferredIngress.ingress_kind == "button_selection",
                        DeferredIngress.status.in_(("pending", "claimed")),
                    )
                )
            ]
            selected_projection_refs = {
                str(binding.get("prompt_projection_ref"))
                for binding in pending_bindings
                if binding.get("prompt_projection_ref")
            }
            for projection in projections:
                intent_session = session.scalar(
                    select(IntentSession).where(
                        IntentSession.ref_id == projection.intent_session_ref
                    )
                )
                if (
                    projection.ref_id in selected_projection_refs
                    or intent_session is None
                    or intent_session.semantic_state != "needs_clarification"
                ):
                    skipped_refs.append(projection.ref_id)
                    continue
                regenerated = self._clone_projection(
                    session,
                    projection=projection,
                    intent_session=intent_session,
                )
                projection.last_error_code = "ingress_deployment_regenerated"
                regenerated_refs.append(regenerated.ref_id)
        return {
            "ok": True,
            "state": "regenerated",
            "count": len(regenerated_refs),
            "projection_refs": regenerated_refs,
            "skipped_projection_refs": skipped_refs,
        }

    @staticmethod
    def _clone_projection(
        session: Session,
        *,
        projection: SemanticPromptProjection,
        intent_session: IntentSession,
    ) -> SemanticPromptProjection:
        prior_options = list(
            session.scalars(
                select(PersistedSemanticOption)
                .where(PersistedSemanticOption.prompt_projection_id == projection.id)
                .order_by(PersistedSemanticOption.created_at, PersistedSemanticOption.option_id)
            )
        )
        if not 1 <= len(prior_options) <= 4:
            raise RuntimeError("quiesced projection has an invalid option count")
        current_version = session.scalar(
            select(func.max(SemanticPromptProjection.projection_version)).where(
                SemanticPromptProjection.intent_session_ref == projection.intent_session_ref
            )
        )
        replacement = SemanticPromptProjection(
            intent_session_ref=projection.intent_session_ref,
            projection_version=int(current_version or 0) + 1,
            guild_id=projection.guild_id,
            channel_id=projection.channel_id,
            parent_channel_id=projection.parent_channel_id,
            source_message_id=projection.source_message_id,
            question_text=projection.question_text,
            case_ref=projection.case_ref,
            case_revision_ref=projection.case_revision_ref,
            render_sha256="0" * 64,
            component_sha256="0" * 64,
            status="pending",
        )
        session.add(replacement)
        session.flush()
        component_options: list[dict[str, str]] = []
        for prior in prior_options:
            preconditions = copy.deepcopy(prior.execution_preconditions_json)
            preconditions.update(
                {
                    "prompt_projection_ref": replacement.ref_id,
                    "prompt_projection_version": replacement.projection_version,
                    "intent_session_version": intent_session.version,
                }
            )
            option = PersistedSemanticOption(
                prompt_projection_id=replacement.id,
                prompt_projection_ref=replacement.ref_id,
                prompt_projection_version=replacement.projection_version,
                option_id=prior.option_id,
                visible_text=prior.visible_text,
                action_kind=prior.action_kind,
                authority_scope_json=copy.deepcopy(prior.authority_scope_json),
                execution_preconditions_json=preconditions,
                compilation_template_json=copy.deepcopy(prior.compilation_template_json),
                case_ref=prior.case_ref,
                case_revision_ref=prior.case_revision_ref,
                intent_session_ref=prior.intent_session_ref,
                authority_scope_hash=prior.authority_scope_hash,
                precondition_hash=sha256_json(preconditions),
            )
            session.add(option)
            component_options.append(
                {
                    "option_id": option.option_id,
                    "authority_scope_hash": option.authority_scope_hash,
                    "precondition_hash": option.precondition_hash,
                }
            )
        render = {
            "question": replacement.question_text,
            "options": [option.visible_text for option in prior_options],
            "intent_session_ref": replacement.intent_session_ref,
            "case_ref": replacement.case_ref,
            "case_revision_ref": replacement.case_revision_ref,
        }
        replacement.render_sha256 = sha256_json(render)
        replacement.component_sha256 = sha256_json(
            {
                "projection_ref": replacement.ref_id,
                "projection_version": replacement.projection_version,
                "options": component_options,
            }
        )
        session.add(
            OutboxEvent(
                event_type="discord.semantic_prompt.requested",
                aggregate_type="semantic_prompt_projection",
                aggregate_id=replacement.id,
                deduplication_key=(
                    f"semantic_prompt:{replacement.ref_id}:"
                    f"v{replacement.projection_version}"
                ),
                payload={
                    "projection_ref": replacement.ref_id,
                    "projection_version": replacement.projection_version,
                    "reason": "ingress_deployment_regeneration",
                },
                status="pending",
            )
        )
        return replacement
