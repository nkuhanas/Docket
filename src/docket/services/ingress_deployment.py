from __future__ import annotations

import copy
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from docket.domain.canonical import sha256_json
from docket.domain.public_refs import new_public_ref
from docket.models import (
    AttentionCase,
    DeferredIngress,
    IntentSession,
    OperatorProjection,
    OutboxEvent,
    PersistedSemanticOption,
    ProjectionDelivery,
)
from docket.providers.discord import DiscordProjectionAdapter

_QUIESCED = "ingress_deployment_quiesced"
_REGENERATED = "ingress_deployment_regenerated"


def _rebase_case_execution(
    value: Any,
    *,
    case_ref: str | None,
    old_revision_ref: str | None,
    new_revision_ref: str | None,
    case_version: int | None,
) -> Any:
    if isinstance(value, list):
        return [
            _rebase_case_execution(
                item,
                case_ref=case_ref,
                old_revision_ref=old_revision_ref,
                new_revision_ref=new_revision_ref,
                case_version=case_version,
            )
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, nested in value.items():
        if (
            key == "case_revision_ref"
            and old_revision_ref is not None
            and nested == old_revision_ref
            and new_revision_ref is not None
        ):
            result[key] = new_revision_ref
        elif key == "expected_versions" and isinstance(nested, dict):
            expected = copy.deepcopy(nested)
            if case_ref is not None and case_version is not None and case_ref in expected:
                expected[case_ref] = case_version
            result[key] = expected
        else:
            result[key] = _rebase_case_execution(
                nested,
                case_ref=case_ref,
                old_revision_ref=old_revision_ref,
                new_revision_ref=new_revision_ref,
                case_version=case_version,
            )
    return result


def _discord_message_parts(value: str) -> tuple[str, str, str]:
    parts = value.split(":")
    if len(parts) != 4 or parts[0] != "discord_message":
        raise RuntimeError("projection delivery has no exact Discord message binding")
    return parts[1], parts[2], parts[3]


class IngressDeploymentService:
    """Quiesce and regenerate clean semantic options around an ingress rollout."""

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
                    select(OperatorProjection, ProjectionDelivery)
                    .join(
                        ProjectionDelivery,
                        ProjectionDelivery.projection_id == OperatorProjection.id,
                    )
                    .where(
                        OperatorProjection.projection_kind == "clarification",
                        ProjectionDelivery.status == "delivered",
                        ProjectionDelivery.external_message_ref.is_not(None),
                        ProjectionDelivery.last_error_code.is_(None),
                    )
                    .order_by(OperatorProjection.created_at)
                )
            )
        quiesced_refs: list[str] = []
        for projection, delivery in rows:
            assert delivery.external_message_ref is not None
            _guild_id, channel_id, message_id = _discord_message_parts(
                delivery.external_message_ref
            )
            request = {
                "request_id": str(uuid.uuid4()),
                "projection_id": str(projection.id),
                "projection_ref": projection.ref_id,
                "projection_version": projection.render_schema_version,
                "channel_id": channel_id,
                "message_id": message_id,
            }
            acknowledgement = self.adapter.quiesce_semantic_prompt(projection.id, request)
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
                raise RuntimeError("Discord returned an invalid quiescence acknowledgement")
            with self.session_factory.begin() as session:
                stored = session.get(ProjectionDelivery, delivery.id, with_for_update=True)
                if stored is None or stored.status != "delivered":
                    raise RuntimeError("projection delivery changed during ingress quiescence")
                stored.last_error_code = _QUIESCED
            quiesced_refs.append(projection.ref_id)
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
            rows = list(
                session.execute(
                    select(OperatorProjection, ProjectionDelivery)
                    .join(
                        ProjectionDelivery,
                        ProjectionDelivery.projection_id == OperatorProjection.id,
                    )
                    .where(ProjectionDelivery.last_error_code == _QUIESCED)
                    .order_by(OperatorProjection.created_at)
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
                str(binding.get("projection_ref"))
                for binding in pending_bindings
                if binding.get("projection_ref")
            }
            for projection, delivery in rows:
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
                    delivery.last_error_code = _REGENERATED
                    continue
                replacement = self._clone_projection(
                    session,
                    projection=projection,
                    delivery=delivery,
                    intent_session=intent_session,
                )
                delivery.last_error_code = _REGENERATED
                regenerated_refs.append(replacement.ref_id)
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
        projection: OperatorProjection,
        delivery: ProjectionDelivery,
        intent_session: IntentSession,
    ) -> OperatorProjection:
        prior_options = list(
            session.scalars(
                select(PersistedSemanticOption)
                .where(PersistedSemanticOption.projection_id == projection.id)
                .order_by(PersistedSemanticOption.created_at, PersistedSemanticOption.option_id)
            )
        )
        if not 1 <= len(prior_options) <= 4:
            raise RuntimeError("quiesced projection has an invalid option count")
        current_case_ref = (
            intent_session.case_refs[0]
            if len(intent_session.case_refs) == 1
            else projection.case_ref
        )
        current_case_revision_ref = (
            intent_session.case_revision_refs[0]
            if len(intent_session.case_revision_refs) == 1
            else projection.case_revision_ref
        )
        current_case = (
            session.scalar(select(AttentionCase).where(AttentionCase.ref_id == current_case_ref))
            if current_case_ref is not None
            else None
        )
        projection_id = uuid.uuid4()
        projection_ref = new_public_ref("proj")
        option_rows: list[PersistedSemanticOption] = []
        option_render: list[dict[str, str]] = []
        option_components: list[dict[str, str]] = []
        for prior in prior_options:
            preconditions = copy.deepcopy(prior.execution_preconditions_json)
            preconditions.update(
                {
                    "projection_ref": projection_ref,
                    "intent_session_version": intent_session.version,
                    "case_ref": current_case_ref,
                    "case_revision_ref": current_case_revision_ref,
                }
            )
            expected_versions = preconditions.get("expected_versions")
            if (
                isinstance(expected_versions, dict)
                and current_case_ref is not None
                and current_case is not None
                and current_case_ref in expected_versions
            ):
                expected_versions[current_case_ref] = current_case.version
            template = _rebase_case_execution(
                copy.deepcopy(prior.compilation_template_json),
                case_ref=current_case_ref,
                old_revision_ref=prior.case_revision_ref,
                new_revision_ref=current_case_revision_ref,
                case_version=current_case.version if current_case is not None else None,
            )
            option = PersistedSemanticOption(
                id=uuid.uuid4(),
                ref_id=new_public_ref("opt"),
                projection_id=projection_id,
                projection_ref=projection_ref,
                option_id=prior.option_id,
                visible_text=prior.visible_text,
                action_kind=prior.action_kind,
                authority_scope_json=copy.deepcopy(prior.authority_scope_json),
                execution_preconditions_json=preconditions,
                compilation_template_json=template,
                case_ref=current_case_ref,
                case_revision_ref=current_case_revision_ref,
                intent_session_ref=prior.intent_session_ref,
                authority_scope_hash=prior.authority_scope_hash,
                precondition_hash=sha256_json(preconditions),
            )
            option_rows.append(option)
            option_render.append(
                {"option_ref": option.ref_id, "visible_text": option.visible_text}
            )
            option_components.append(
                {
                    "option_ref": option.ref_id,
                    "authority_scope_hash": option.authority_scope_hash,
                    "precondition_hash": option.precondition_hash,
                }
            )
        prior_render = projection.semantic_content.get("render", {})
        question = str(prior_render.get("question", "Choose how Docket should proceed."))
        render = {
            "question": question,
            "options": option_render,
            "intent_session_ref": intent_session.ref_id,
            "case_ref": current_case_ref,
            "case_revision_ref": current_case_revision_ref,
        }
        component_binding = {
            "projection_ref": projection_ref,
            "options": option_components,
        }
        visible_text = question + "\n\n" + "\n".join(
            f"{index}. {option.visible_text}"
            for index, option in enumerate(option_rows, start=1)
        )
        replacement = OperatorProjection(
            id=projection_id,
            ref_id=projection_ref,
            projection_kind="clarification",
            operator_ref=projection.operator_ref,
            primary_public_ref=current_case_ref or intent_session.ref_id,
            primary_revision_ref=current_case_revision_ref,
            supersedes_projection_ref=projection.ref_id,
            intent_session_ref=intent_session.ref_id,
            case_ref=current_case_ref,
            case_revision_ref=current_case_revision_ref,
            semantic_content={"render": render, "component_binding": component_binding},
            visible_text=visible_text,
            render_schema_version=projection.render_schema_version,
            render_sha256=sha256_json(render),
            component_sha256=sha256_json(component_binding),
            basis_refs=list(projection.basis_refs),
        )
        session.add_all([replacement, *option_rows])
        session.flush()
        session.add(
            ProjectionDelivery(
                projection_id=replacement.id,
                projection_ref=replacement.ref_id,
                transport=delivery.transport,
                destination_ref=delivery.destination_ref,
                source_message_ref=delivery.source_message_ref,
                status="pending",
            )
        )
        session.add(
            OutboxEvent(
                event_type="discord.semantic_prompt.requested",
                aggregate_type="operator_projection",
                aggregate_id=replacement.id,
                deduplication_key=f"semantic_prompt:{replacement.ref_id}",
                payload={"projection_ref": replacement.ref_id},
                status="pending",
            )
        )
        return replacement
