from __future__ import annotations

import copy
import hashlib
import uuid
from collections.abc import Iterable
from datetime import UTC
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError, IdempotencyConflict
from docket.internal_api.schemas import SemanticOptionSelection
from docket.models import (
    AuditEvent,
    DeferredIngress,
    DiscordDailyThread,
    IdentityHandle,
    IntentSession,
    OperatorUtterance,
    OutboxEvent,
    PersistedSemanticOption,
    SemanticPromptProjection,
    SemanticRequest,
)
from docket.models.base import utc_now
from docket.schemas.authority import ChangeSetContent, SemanticOptionDraft
from docket.security import decode_semantic_option_token, verify_semantic_option_token
from docket.services.continuity import ContinuityService
from docket.services.gateway_lifetimes import GatewayLifetimeService

CURRENT_SELECTION_UTTERANCE = "$current_selection_utterance"


def _replace_authority_slot(value: Any, authority_ref: str) -> tuple[Any, int]:
    """Replace an existing typed utterance only in provenance-bearing fields."""

    replacements = 0

    def visit(item: Any, path: tuple[str, ...]) -> Any:
        nonlocal replacements
        if isinstance(item, dict):
            return {key: visit(child, (*path, key)) for key, child in item.items()}
        if isinstance(item, list):
            return [visit(child, path) for child in item]
        allowed = bool(path) and (
            path[-1] == "basis_refs" or path[-2:] == ("resolution_basis", "utterance_ref")
        )
        if allowed and item == authority_ref:
            replacements += 1
            return CURRENT_SELECTION_UTTERANCE
        return item

    return visit(copy.deepcopy(value), ()), replacements


def complete_selection_provenance(template: dict[str, Any], utterance_ref: str) -> dict[str, Any]:
    """Substitute the only permitted option-time symbol and validate the result."""

    def visit(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: visit(child) for key, child in item.items()}
        if isinstance(item, list):
            return [visit(child) for child in item]
        if item == CURRENT_SELECTION_UTTERANCE:
            return utterance_ref
        if isinstance(item, str) and item.startswith("$"):
            raise DocketError(
                code="invalid_symbolic_authority",
                message="Persisted option contains an unsupported authority symbol.",
            )
        return item

    completed = visit(copy.deepcopy(template))
    return ChangeSetContent.model_validate(completed).model_dump(mode="json")


def _authority_scope(content: dict[str, Any], exclusions: list[str]) -> dict[str, Any]:
    evidence_keys = {
        "basis_refs",
        "source_refs",
        "expected_versions",
        "idempotency_key",
        "utterance_ref",
        "case_revision_ref",
    }
    execution_keys = {"change_id", "intent_id"}

    def semantic(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: semantic(child)
                for key, child in sorted(value.items())
                if key not in evidence_keys and key not in execution_keys
            }
        if isinstance(value, list):
            return [semantic(child) for child in value]
        return value

    return {
        "effects": semantic(content),
        "explicit_exclusions": sorted(exclusions),
    }


class SemanticOptionService:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _source_parts(utterance: OperatorUtterance) -> tuple[str, str, str]:
        parts = utterance.source_message_ref.split(":")
        if len(parts) != 4 or parts[0] != "discord_message":
            raise DocketError(
                code="invalid_option_projection_source",
                message="Semantic options require an exact Discord source message.",
            )
        return parts[1], parts[2], parts[3]

    def _parent_channel(self, guild_id: str, channel_id: str) -> str | None:
        return self.session.scalar(
            select(DiscordDailyThread.channel_id).where(
                DiscordDailyThread.guild_id == guild_id,
                DiscordDailyThread.thread_id == channel_id,
            )
        )

    def _identity_label(self, ref_id: str | None) -> str:
        if ref_id is None:
            return "the new identity"
        identity = self.session.scalar(
            select(IdentityHandle).where(IdentityHandle.ref_id == ref_id)
        )
        if identity is None:
            return ref_id
        return f"{identity.handle_type} `{identity.value}`"

    def _effect_label(self, change: dict[str, Any], creates: dict[str, dict[str, Any]]) -> str:
        mutation_type = str(change["mutation_type"])
        create = change.get("create_spec") or {}
        payload = change.get("payload") or {}
        if mutation_type == "entity_create":
            return f"create {create['entity_kind']} “{create['display_name']}”"
        if mutation_type == "identity_handle_create":
            return f"create {create['handle_type']} identity `{create['value']}`"
        if mutation_type == "identity_binding_bind":
            identity = self._identity_label(change.get("object_ref"))
            target_change = creates.get(str(payload.get("entity_change_id")))
            target = (
                f"the new {target_change['create_spec']['entity_kind']} "
                f"“{target_change['create_spec']['display_name']}”"
                if target_change is not None
                else str(payload.get("entity_ref") or payload.get("entity_change_id"))
            )
            return f"bind {identity} to {target}"
        if mutation_type == "attention_case_resolution":
            dispositions = ", ".join(
                f"{item['item_ref']} {item['disposition']}"
                for item in change.get("item_dispositions", [])
            )
            outcome = str(change.get("case_outcome", "keep_open")).replace("_", " ")
            detail = f" ({dispositions})" if dispositions else ""
            return f"set {change['object_ref']} to {outcome}{detail}"
        action = str(change.get("action", "apply")).replace("_", " ")
        object_type = str(change.get("object_type", mutation_type)).replace("_", " ")
        target = change.get("object_ref") or change.get("object_change_id") or "new object"
        return f"{action} {object_type} {target}"

    def render_visible_text(self, content: dict[str, Any], exclusions: Iterable[str]) -> str:
        changes = [
            change
            for key in (
                "registry_changes",
                "preference_changes",
                "lane_changes",
                "event_changes",
                "resolution_changes",
            )
            for change in content.get(key, [])
        ]
        creates = {str(change.get("change_id")): change for change in changes}
        labels = [self._effect_label(change, creates) for change in changes]
        labels.extend(
            f"queue provider operation {intent['operation_type']}"
            for intent in content.get("provider_intents", [])
        )
        labels.extend(f"exclude {value}" for value in exclusions)
        if not labels:
            raise DocketError(
                code="semantic_option_empty",
                message="A semantic option must visibly describe at least one effect.",
            )
        rendered = "; then ".join(labels)
        return rendered[0].upper() + rendered[1:] + "."

    def persist_prompt(
        self,
        *,
        utterance: OperatorUtterance,
        intent_session: IntentSession,
        question: str,
        drafts: list[SemanticOptionDraft],
    ) -> SemanticPromptProjection:
        if not 1 <= len(drafts) <= 4:
            raise DocketError(
                code="semantic_option_count_invalid",
                message="A clarification projection requires one through four options.",
            )
        if any(draft.selection_authority_ref != utterance.ref_id for draft in drafts):
            raise DocketError(
                code="semantic_option_authority_mismatch",
                message="Option templates must use the current authenticated utterance slot.",
            )
        guild_id, channel_id, source_message_id = self._source_parts(utterance)
        current_version = self.session.scalar(
            select(func.max(SemanticPromptProjection.projection_version)).where(
                SemanticPromptProjection.intent_session_ref == intent_session.ref_id
            )
        )
        projection_version = int(current_version or 0) + 1
        case_ref = intent_session.case_refs[0] if len(intent_session.case_refs) == 1 else None
        case_revision_ref = (
            intent_session.case_revision_refs[0]
            if len(intent_session.case_revision_refs) == 1
            else None
        )
        projection = SemanticPromptProjection(
            intent_session_ref=intent_session.ref_id,
            projection_version=projection_version,
            guild_id=guild_id,
            channel_id=channel_id,
            parent_channel_id=self._parent_channel(guild_id, channel_id),
            source_message_id=source_message_id,
            question_text=question.strip(),
            case_ref=case_ref,
            case_revision_ref=case_revision_ref,
            render_sha256="0" * 64,
            component_sha256="0" * 64,
            status="pending",
        )
        self.session.add(projection)
        self.session.flush()

        rendered_options: list[dict[str, str]] = []
        for draft in drafts:
            content = draft.content.model_dump(mode="json")
            template, replacements = _replace_authority_slot(
                content,
                draft.selection_authority_ref,
            )
            if replacements < 1:
                raise DocketError(
                    code="selection_authority_slot_missing",
                    message="Option did not contain a replaceable selection authority slot.",
                )
            scope = _authority_scope(template, draft.explicit_exclusions)
            preconditions = {
                "expected_versions": content.get("expected_versions", {}),
                "case_ref": case_ref,
                "case_revision_ref": case_revision_ref,
                "prompt_projection_ref": projection.ref_id,
                "prompt_projection_version": projection.projection_version,
                "intent_session_version": intent_session.version,
            }
            visible_text = self.render_visible_text(content, draft.explicit_exclusions)
            option = PersistedSemanticOption(
                prompt_projection_id=projection.id,
                prompt_projection_ref=projection.ref_id,
                prompt_projection_version=projection.projection_version,
                option_id=draft.option_id,
                visible_text=visible_text,
                action_kind=draft.action_kind,
                authority_scope_json=scope,
                execution_preconditions_json=preconditions,
                compilation_template_json=template,
                case_ref=case_ref,
                case_revision_ref=case_revision_ref,
                intent_session_ref=intent_session.ref_id,
                authority_scope_hash=sha256_json(scope),
                precondition_hash=sha256_json(preconditions),
            )
            self.session.add(option)
            rendered_options.append(
                {
                    "option_id": option.option_id,
                    "visible_text": visible_text,
                    "authority_scope_hash": option.authority_scope_hash,
                    "precondition_hash": option.precondition_hash,
                }
            )
        render = {
            "question": projection.question_text,
            "options": [item["visible_text"] for item in rendered_options],
            "intent_session_ref": projection.intent_session_ref,
            "case_ref": projection.case_ref,
            "case_revision_ref": projection.case_revision_ref,
        }
        projection.render_sha256 = sha256_json(render)
        projection.component_sha256 = sha256_json(
            {
                "projection_ref": projection.ref_id,
                "projection_version": projection.projection_version,
                "options": [
                    {
                        "option_id": item["option_id"],
                        "authority_scope_hash": item["authority_scope_hash"],
                        "precondition_hash": item["precondition_hash"],
                    }
                    for item in rendered_options
                ],
            }
        )
        self.session.add(
            OutboxEvent(
                event_type="discord.semantic_prompt.requested",
                aggregate_type="semantic_prompt_projection",
                aggregate_id=projection.id,
                deduplication_key=f"semantic_prompt:{projection.ref_id}:v{projection.projection_version}",
                payload={
                    "projection_ref": projection.ref_id,
                    "projection_version": projection.projection_version,
                },
                status="pending",
            )
        )
        return projection

    def capture_selection(self, request: SemanticOptionSelection) -> dict[str, Any]:
        settings = get_settings()
        if request.gateway_instance_ref is not None:
            GatewayLifetimeService(self.session).require_live(request.gateway_instance_ref)
        decoded = decode_semantic_option_token(request.option_token)
        if decoded is None:
            raise DocketError(
                code="invalid_semantic_option_token",
                message="Semantic option binding is malformed.",
            )
        if decoded.actor_id != str(int(request.discord_user_id)):
            raise DocketError(
                code="unauthorized_interaction",
                message="Semantic option is not bound to this Operator.",
            )
        if request.responded_at.astimezone(UTC) > decoded.expires_at:
            raise DocketError(
                code="semantic_option_expired",
                message="This semantic option has expired and must be regenerated.",
            )
        option = self.session.get(PersistedSemanticOption, decoded.option_row_id)
        if option is None:
            raise DocketError(
                code="semantic_option_not_found",
                message="Persisted semantic option was not found.",
            )
        projection = self.session.scalar(
            select(SemanticPromptProjection)
            .where(SemanticPromptProjection.id == option.prompt_projection_id)
            .with_for_update()
        )
        if (
            projection is None
            or option.prompt_projection_ref != projection.ref_id
            or option.prompt_projection_version != decoded.projection_version
            or projection.projection_version != decoded.projection_version
            or projection.guild_id != request.guild_id
            or projection.channel_id != request.channel_id
            or projection.parent_channel_id != request.parent_channel_id
            or projection.message_id != request.message_id
        ):
            raise DocketError(
                code="semantic_option_binding_mismatch",
                message="Discord interaction does not match its persisted option projection.",
            )
        reference = decoded
        signing_key = settings.read_secret(settings.interaction_signing_key_file).encode()
        if not verify_semantic_option_token(
            request.option_token,
            reference=reference,
            signing_key=signing_key,
        ):
            raise DocketError(
                code="invalid_semantic_option_token",
                message="Semantic option signature is invalid.",
            )
        if (
            sha256_json(option.authority_scope_json) != option.authority_scope_hash
            or sha256_json(option.execution_preconditions_json) != option.precondition_hash
        ):
            raise DocketError(
                code="semantic_option_hash_mismatch",
                message="Persisted semantic option no longer matches its signed scope.",
            )
        execution_option = self._latest_compatible_option(option)

        source_key = (
            f"discord:{request.guild_id}:{request.channel_id}:{request.discord_interaction_id}:0"
        )
        existing = self.session.scalar(
            select(OperatorUtterance).where(
                OperatorUtterance.discord_interaction_ref == request.discord_interaction_id
            )
        )
        if existing is not None:
            if (
                existing.selected_option_id != option.option_id
                or existing.authority_scope_hash != option.authority_scope_hash
                or existing.selected_precondition_hash != option.precondition_hash
            ):
                raise IdempotencyConflict(source_key)
            ingress = self.session.scalar(
                select(DeferredIngress).where(DeferredIngress.utterance_ref == existing.ref_id)
            )
            semantic_request = self.session.scalar(
                select(SemanticRequest).where(
                    SemanticRequest.intent_session_ref == option.intent_session_ref,
                    SemanticRequest.authority_scope_hash == option.authority_scope_hash,
                )
            )
            if semantic_request is None:
                semantic_request = self._create_semantic_request(
                    option=option,
                    utterance=existing,
                )
                self._apply_safe_rebase(
                    semantic_request=semantic_request,
                    selected_option=option,
                    execution_option=execution_option,
                    request=request,
                )
                self._append_selection_audit(
                    request=request,
                    option=option,
                    utterance=existing,
                    semantic_request=semantic_request,
                )
            execution_option = self._execution_option(
                semantic_request,
                fallback=execution_option,
            )
            compiled_content = complete_selection_provenance(
                execution_option.compilation_template_json,
                existing.ref_id,
            )
            if ingress is not None:
                ingress.selected_option_binding_json = {
                    **(ingress.selected_option_binding_json or {}),
                    "semantic_request_ref": semantic_request.ref_id,
                    "compiled_content": compiled_content,
                    "execution_prompt_projection_ref": (
                        execution_option.prompt_projection_ref
                    ),
                    "execution_precondition_hash": execution_option.precondition_hash,
                }
            lease_ref, execution_ready = self._claim_selection_ingress(
                ingress,
                utterance=existing,
                semantic_request=semantic_request,
                allow_retry=request.resume_authorized_execution,
                retry_request_id=request.request_id,
                gateway_instance_ref=request.gateway_instance_ref,
            )
            return self._selection_result(
                existing,
                execution_option,
                semantic_request,
                ingress,
                replay=True,
                execution_lease_ref=lease_ref,
                execution_ready=execution_ready,
            )

        prior_prompt_selection = self.session.scalar(
            select(OperatorUtterance).where(
                OperatorUtterance.prompt_projection_ref == option.prompt_projection_ref,
                OperatorUtterance.selected_option_id.is_not(None),
            )
        )
        if prior_prompt_selection is not None:
            raise DocketError(
                code="semantic_prompt_already_selected",
                message="This semantic prompt was already resolved by another selection.",
                details={"utterance_ref": prior_prompt_selection.ref_id},
            )
        if projection.status == "selected":
            raise DocketError(
                code="semantic_prompt_already_selected",
                message="This semantic prompt was already resolved by another selection.",
            )
        utterance = OperatorUtterance(
            actor_ref=f"discord_user:{request.discord_user_id}",
            transport="discord",
            source_message_ref=(
                f"discord_interaction:{request.guild_id}:{request.channel_id}:"
                f"{request.discord_interaction_id}"
            ),
            conversation_ref=f"discord_conversation:{request.guild_id}:{request.channel_id}",
            reply_to_source_ref=(
                f"discord_message:{request.guild_id}:{request.channel_id}:{request.message_id}"
            ),
            said_at=request.responded_at,
            verbatim_text=option.visible_text,
            content_hash=hashlib.sha256(option.visible_text.encode("utf-8")).hexdigest(),
            request_key=source_key,
            utterance_kind="button_selection",
            selected_option_id=option.option_id,
            visible_choice_text=option.visible_text,
            authority_scope_hash=option.authority_scope_hash,
            selected_precondition_hash=option.precondition_hash,
            prompt_projection_ref=option.prompt_projection_ref,
            prompt_projection_version=option.prompt_projection_version,
            case_ref=option.case_ref,
            case_revision_ref=option.case_revision_ref,
            intent_session_ref=option.intent_session_ref,
            discord_interaction_ref=request.discord_interaction_id,
        )
        self.session.add(utterance)
        self.session.flush()
        compiled_content = complete_selection_provenance(
            execution_option.compilation_template_json,
            utterance.ref_id,
        )
        semantic_request = self.session.scalar(
            select(SemanticRequest).where(
                SemanticRequest.intent_session_ref == option.intent_session_ref,
                SemanticRequest.authority_scope_hash == option.authority_scope_hash,
            )
        )
        if semantic_request is None:
            semantic_request = self._create_semantic_request(
                option=option,
                utterance=utterance,
            )
            self._apply_safe_rebase(
                semantic_request=semantic_request,
                selected_option=option,
                execution_option=execution_option,
                request=request,
            )

        ingress = DeferredIngress(
            source_key=source_key,
            ingress_kind="button_selection",
            utterance_ref=utterance.ref_id,
            selected_option_binding_json={
                "prompt_projection_ref": option.prompt_projection_ref,
                "prompt_projection_version": option.prompt_projection_version,
                "option_id": option.option_id,
                "authority_scope_hash": option.authority_scope_hash,
                "precondition_hash": option.precondition_hash,
                "semantic_request_ref": semantic_request.ref_id,
                "compiled_content": compiled_content,
                "execution_prompt_projection_ref": (
                    execution_option.prompt_projection_ref
                ),
                "execution_precondition_hash": execution_option.precondition_hash,
            },
            status="pending",
        )
        self.session.add(ingress)
        self.session.flush()
        lease_ref, execution_ready = self._claim_selection_ingress(
            ingress,
            utterance=utterance,
            semantic_request=semantic_request,
            allow_retry=request.resume_authorized_execution,
            retry_request_id=request.request_id,
            gateway_instance_ref=request.gateway_instance_ref,
        )
        execution_projection = self.session.get(
            SemanticPromptProjection,
            execution_option.prompt_projection_id,
        )
        if execution_projection is not None:
            execution_projection.status = "selected"
        self._append_selection_audit(
            request=request,
            option=option,
            utterance=utterance,
            semantic_request=semantic_request,
        )
        self.session.flush()
        return self._selection_result(
            utterance,
            execution_option,
            semantic_request,
            ingress,
            replay=False,
            execution_lease_ref=lease_ref,
            execution_ready=execution_ready,
        )

    def _latest_compatible_option(
        self,
        selected: PersistedSemanticOption,
    ) -> PersistedSemanticOption:
        candidate = self.session.scalar(
            select(PersistedSemanticOption)
            .join(
                SemanticPromptProjection,
                SemanticPromptProjection.id
                == PersistedSemanticOption.prompt_projection_id,
            )
            .where(
                PersistedSemanticOption.intent_session_ref
                == selected.intent_session_ref,
                PersistedSemanticOption.option_id == selected.option_id,
                PersistedSemanticOption.authority_scope_hash
                == selected.authority_scope_hash,
                SemanticPromptProjection.status.in_(("pending", "delivered")),
            )
            .order_by(PersistedSemanticOption.prompt_projection_version.desc())
        )
        if candidate is None or candidate.prompt_projection_version <= (
            selected.prompt_projection_version
        ):
            return selected
        if (
            candidate.authority_scope_json != selected.authority_scope_json
            or candidate.visible_text != selected.visible_text
        ):
            return selected
        return candidate

    def _execution_option(
        self,
        semantic_request: SemanticRequest | None,
        *,
        fallback: PersistedSemanticOption,
    ) -> PersistedSemanticOption:
        if semantic_request is None:
            return fallback
        binding = semantic_request.selected_option_binding or {}
        projection_ref = binding.get("execution_prompt_projection_ref")
        projection_version = binding.get("execution_prompt_projection_version")
        precondition_hash = binding.get("execution_precondition_hash")
        if not all((projection_ref, projection_version, precondition_hash)):
            return fallback
        return self.session.scalar(
            select(PersistedSemanticOption).where(
                PersistedSemanticOption.prompt_projection_ref == projection_ref,
                PersistedSemanticOption.prompt_projection_version == projection_version,
                PersistedSemanticOption.option_id == fallback.option_id,
                PersistedSemanticOption.authority_scope_hash
                == fallback.authority_scope_hash,
                PersistedSemanticOption.precondition_hash == precondition_hash,
            )
        ) or fallback

    def _apply_safe_rebase(
        self,
        *,
        semantic_request: SemanticRequest,
        selected_option: PersistedSemanticOption,
        execution_option: PersistedSemanticOption,
        request: SemanticOptionSelection,
    ) -> None:
        if execution_option.id == selected_option.id:
            return
        semantic_request.current_precondition_hash = execution_option.precondition_hash
        semantic_request.current_case_revision_ref = execution_option.case_revision_ref
        semantic_request.selected_option_binding = {
            **(semantic_request.selected_option_binding or {}),
            "execution_prompt_projection_ref": execution_option.prompt_projection_ref,
            "execution_prompt_projection_version": (
                execution_option.prompt_projection_version
            ),
            "execution_precondition_hash": execution_option.precondition_hash,
            "execution_case_revision_ref": execution_option.case_revision_ref,
        }
        self.session.add(
            AuditEvent(
                event_type="semantic_option.safely_rebased",
                entity_type="semantic_request",
                entity_id=semantic_request.id,
                actor_type="docket_compiler",
                actor_id=None,
                request_id=request.request_id,
                primary_ref=semantic_request.ref_id,
                affected_refs=[semantic_request.ref_id],
                basis_refs=list(semantic_request.origin_utterance_refs),
                data={
                    "authority_scope_hash": selected_option.authority_scope_hash,
                    "original_precondition_hash": selected_option.precondition_hash,
                    "rebased_precondition_hash": execution_option.precondition_hash,
                    "original_revision": selected_option.case_revision_ref,
                    "rebased_revision": execution_option.case_revision_ref,
                    "semantic_scope_changed": False,
                },
            )
        )

    def _create_semantic_request(
        self,
        *,
        option: PersistedSemanticOption,
        utterance: OperatorUtterance,
    ) -> SemanticRequest:
        intent_session = self.session.scalar(
            select(IntentSession).where(IntentSession.ref_id == option.intent_session_ref)
        )
        if intent_session is None:
            raise DocketError(
                code="intent_session_not_found",
                message="Semantic option's IntentSession no longer exists.",
            )
        semantic_request = SemanticRequest(
            intent_session_id=intent_session.id,
            intent_session_ref=intent_session.ref_id,
            authority_scope_hash=option.authority_scope_hash,
            current_precondition_hash=option.precondition_hash,
            origin_utterance_refs=[utterance.ref_id],
            selected_option_binding={
                "prompt_projection_ref": option.prompt_projection_ref,
                "prompt_projection_version": option.prompt_projection_version,
                "option_id": option.option_id,
                "authority_scope_hash": option.authority_scope_hash,
                "selected_precondition_hash": option.precondition_hash,
            },
            authority_availability="available",
            commit_state="not_attempted",
            current_case_revision_ref=option.case_revision_ref,
            symbolic_substitutions_json={CURRENT_SELECTION_UTTERANCE: utterance.ref_id},
        )
        self.session.add(semantic_request)
        self.session.flush()
        intent_session.semantic_state = "ready"
        intent_session.commit_state = "not_attempted"
        intent_session.semantic_request_ref = semantic_request.ref_id
        return semantic_request

    def _append_selection_audit(
        self,
        *,
        request: SemanticOptionSelection,
        option: PersistedSemanticOption,
        utterance: OperatorUtterance,
        semantic_request: SemanticRequest,
    ) -> None:
        self.session.add(
            AuditEvent(
                event_type="semantic_option.selected",
                entity_type="semantic_request",
                entity_id=semantic_request.id,
                actor_type="operator",
                actor_id=request.discord_user_id,
                request_id=request.request_id,
                primary_ref=semantic_request.ref_id,
                affected_refs=[utterance.ref_id, semantic_request.ref_id],
                basis_refs=[utterance.ref_id],
                data={
                    "prompt_projection_ref": option.prompt_projection_ref,
                    "prompt_projection_version": option.prompt_projection_version,
                    "option_id": option.option_id,
                    "authority_scope_hash": option.authority_scope_hash,
                    "precondition_hash": option.precondition_hash,
                },
            )
        )

    def _claim_selection_ingress(
        self,
        ingress: DeferredIngress | None,
        *,
        utterance: OperatorUtterance,
        semantic_request: SemanticRequest,
        allow_retry: bool,
        retry_request_id: uuid.UUID,
        gateway_instance_ref: str | None,
    ) -> tuple[str | None, bool]:
        if ingress is None or ingress.status in {"completed", "claimed"}:
            return None, False
        if ingress.status == "rejected" and not allow_retry:
            return None, False
        if semantic_request.commit_state != "not_attempted" and not (
            allow_retry
            and semantic_request.authority_availability == "available"
            and semantic_request.commit_state
            in {
                "blocked_validation",
                "blocked_conflict",
                "blocked_version",
                "failed",
                "unknown",
            }
        ):
            return None, False
        if allow_retry:
            binding = ingress.selected_option_binding_json or {}
            if binding.get("last_retry_request_id") == str(retry_request_id):
                return None, False
            ingress.selected_option_binding_json = {
                **binding,
                "last_retry_request_id": str(retry_request_id),
            }
            ingress.status = "pending"
            ingress.last_error_code = None
        claim_token = uuid.uuid4()
        try:
            lease = ContinuityService(self.session).acquire_execution_lease(
                lease_key=f"interactive:{utterance.ref_id}:{claim_token}",
                lease_kind="interactive_turn",
                subject_ref=utterance.ref_id,
                gateway_instance_ref=gateway_instance_ref,
            )
        except DocketError as exc:
            if exc.code != "deployment_drain_active":
                raise
            ingress.status = "pending"
            ingress.drain_ref = str((exc.details or {}).get("drain_ref") or "") or None
            ingress.claimed_by_gateway_ref = None
            ingress.claim_token = None
            ingress.claimed_at = None
            return None, False
        ingress.status = "claimed"
        ingress.drain_ref = None
        ingress.claimed_by_gateway_ref = gateway_instance_ref
        ingress.claim_token = claim_token
        ingress.claimed_at = utc_now()
        return lease.ref_id, True

    @staticmethod
    def _selection_result(
        utterance: OperatorUtterance,
        option: PersistedSemanticOption,
        semantic_request: SemanticRequest | None,
        ingress: DeferredIngress | None,
        *,
        replay: bool,
        execution_lease_ref: str | None,
        execution_ready: bool,
    ) -> dict[str, Any]:
        if semantic_request is None or ingress is None:
            raise DocketError(
                code="selection_continuity_missing",
                message="Selection evidence exists without its durable execution lineage.",
            )
        binding = ingress.selected_option_binding_json or {}
        return {
            "ok": True,
            "ref": utterance.ref_id,
            "state": "accepted",
            "summary": "Decision recorded; authorized execution is queued.",
            "affected_refs": [utterance.ref_id, semantic_request.ref_id, ingress.ref_id],
            "basis_refs": [utterance.ref_id],
            "next": {
                "action": "resume_semantic_request",
                "semantic_request_ref": semantic_request.ref_id,
            },
            "warnings": [],
            "disposition": "replayed_request" if replay else "stored",
            "utterance_ref": utterance.ref_id,
            "request_key": utterance.request_key,
            "intent_session_ref": option.intent_session_ref,
            "case_ref": option.case_ref,
            "case_revision_ref": option.case_revision_ref,
            "semantic_request_ref": semantic_request.ref_id,
            "authority_availability": semantic_request.authority_availability,
            "commit_state": semantic_request.commit_state,
            "committed_changeset_ref": semantic_request.committed_changeset_ref,
            "authority_scope_hash": option.authority_scope_hash,
            "precondition_hash": option.precondition_hash,
            "visible_choice_text": option.visible_text,
            "compiled_content": binding.get("compiled_content"),
            "deferred_ingress_ref": ingress.ref_id,
            "execution_lease_ref": execution_lease_ref,
            "execution_ready": execution_ready,
        }
