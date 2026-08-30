import json
import uuid

import pytest
from sqlalchemy import func, select

from docket.config import get_settings
from docket.domain.public_refs import new_public_ref
from docket.internal_api.schemas import OperatorUtteranceCapture
from docket.models import (
    Affiliation,
    ChangeSet,
    Conflict,
    Entity,
    EntityAlias,
    Fact,
    IdentityBinding,
    IdentityHandle,
    Interaction,
    InteractionParticipant,
    OrganizationInstitutionProfile,
    PersonProfile,
    Relationship,
)
from docket.schemas.authority import (
    ChangeSetContent,
    StatementInput,
    StatementRelationInput,
)
from docket.schemas.registry import IdentityResolutionRequest
from docket.services.entity_resolution import DeterministicIdentityResolutionService
from docket.services.history import HistoryService
from docket.services.interactive_authority import InteractiveAuthorityService
from docket.services.network import NetworkQueryService
from docket.services.provenance import ProvenanceService
from docket.services.provenance_refs import ProvenanceRefService
from docket.services.statements import StatementService


def _capture(session, *, message_id: str, text: str) -> tuple[str, str]:
    settings = get_settings()
    request_key = (
        f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
        f"{message_id}:0"
    )
    request = OperatorUtteranceCapture.model_validate(
        {
            "request_id": str(uuid.uuid4()),
            "guild_id": settings.discord_guild_id,
            "channel_id": settings.chat_channel_id,
            "message_id": message_id,
            "actor_id": settings.operator_discord_user_id,
            "verbatim_text": text,
            "request_key": request_key,
        }
    )
    result = ProvenanceService(session).capture_operator_utterance(request)
    return str(result["ref"]), request_key


@pytest.mark.integration
def test_rich_registry_graph_commits_atomically_with_create_references(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        utterance_ref, request_key = _capture(
            session,
            message_id="1542800000000000001",
            text=(
                "Register Isaac, Cal Poly, its engineering college and CS department; "
                "record our drone-work context and today's meeting."
            ),
        )
        isaac_planned_ref = new_public_ref("ent")
        statement_input = StatementInput.model_validate(
            {
                "statement_kind": "registry_command",
                "subject_refs": [isaac_planned_ref],
                "predicate": "register_context_graph",
                "value_json": {
                    "person": "Isaac",
                    "institution": "Cal Poly",
                    "context": "drone work",
                },
                "affected_fields": [
                    "entities",
                    "identity",
                    "affiliation",
                    "relationship",
                    "fact",
                    "interaction",
                ],
                "interpreter_version": "registry-test-v1",
            }
        )
        statement = StatementService(session).derive(
            utterance_ref, [statement_input]
        )[0]
        basis = [statement.ref_id]
        content = ChangeSetContent.model_validate(
            {
                "basis_refs": basis,
                "registry_changes": [
                    {
                        "mutation_type": "entity_create",
                        "change_id": "operator",
                        "action": "create",
                        "object_type": "entity",
                        "create_spec": {
                            "entity_kind": "person",
                            "display_name": "Operator",
                            "preferred_name": "Chace",
                            "is_operator": True,
                        },
                        "affected_fields": ["identity"],
                        "basis_refs": basis,
                    },
                    {
                        "mutation_type": "entity_create",
                        "change_id": "cal-poly",
                        "action": "create",
                        "object_type": "entity",
                        "create_spec": {
                            "entity_kind": "institution",
                            "display_name": "Cal Poly",
                            "organization_type": "university",
                        },
                        "affected_fields": ["identity"],
                        "basis_refs": basis,
                    },
                    {
                        "mutation_type": "entity_create",
                        "change_id": "engineering-college",
                        "action": "create",
                        "object_type": "entity",
                        "create_spec": {
                            "entity_kind": "organization",
                            "display_name": "College of Engineering",
                            "organization_type": "college",
                            "parent_entity_change_id": "cal-poly",
                        },
                        "affected_fields": ["identity", "hierarchy"],
                        "basis_refs": basis,
                    },
                    {
                        "mutation_type": "entity_create",
                        "change_id": "cs-department",
                        "action": "create",
                        "object_type": "entity",
                        "create_spec": {
                            "entity_kind": "organization",
                            "display_name": "Computer Science Department",
                            "organization_type": "department",
                            "parent_entity_change_id": "engineering-college",
                        },
                        "affected_fields": ["identity", "hierarchy"],
                        "basis_refs": basis,
                    },
                    {
                        "mutation_type": "entity_create",
                        "change_id": "isaac",
                        "action": "create",
                        "object_type": "entity",
                        "create_spec": {
                            "ref_id": isaac_planned_ref,
                            "entity_kind": "person",
                            "display_name": "Isaac",
                        },
                        "affected_fields": ["identity"],
                        "basis_refs": basis,
                    },
                    {
                        "mutation_type": "identity_handle_create",
                        "change_id": "isaac-email",
                        "action": "create",
                        "object_type": "identity_handle",
                        "create_spec": {
                            "handle_type": "email",
                            "value": "isaac@example.com",
                        },
                        "affected_fields": ["identity_binding"],
                        "basis_refs": basis,
                    },
                    {
                        "mutation_type": "identity_binding_bind",
                        "change_id": "bind-isaac-email",
                        "action": "bind",
                        "object_type": "identity_binding",
                        "object_change_id": "isaac-email",
                        "payload": {
                            "entity_change_id": "isaac",
                            "resolution_basis": {
                                "kind": "operator_selection",
                                "utterance_ref": utterance_ref,
                            },
                        },
                        "affected_fields": ["identity_binding"],
                        "basis_refs": [utterance_ref],
                    },
                    {
                        "mutation_type": "affiliation_create",
                        "change_id": "isaac-affiliation",
                        "action": "create",
                        "object_type": "affiliation",
                        "create_spec": {
                            "subject_change_id": "isaac",
                            "organization_change_id": "cs-department",
                            "role": "provisional department head",
                            "domain": "CS / drone work",
                        },
                        "affected_fields": ["affiliation"],
                        "basis_refs": basis,
                    },
                    {
                        "mutation_type": "relationship_create",
                        "change_id": "operator-isaac-relationship",
                        "action": "create",
                        "object_type": "relationship",
                        "create_spec": {
                            "subject_change_id": "operator",
                            "object_change_id": "isaac",
                            "relationship_type": "collaborator",
                            "context": "drone work",
                        },
                        "affected_fields": ["relationship"],
                        "basis_refs": basis,
                    },
                    {
                        "mutation_type": "fact_create",
                        "change_id": "isaac-area",
                        "action": "create",
                        "object_type": "fact",
                        "create_spec": {
                            "subject_change_id": "isaac",
                            "predicate": "area_of_work",
                            "value_json": "drone software",
                        },
                        "affected_fields": ["area_of_work"],
                        "basis_refs": basis,
                    },
                    {
                        "mutation_type": "interaction_create",
                        "change_id": "meeting",
                        "action": "create",
                        "object_type": "interaction",
                        "create_spec": {
                            "interaction_type": "met_person",
                            "occurred_at": "2026-08-28T17:00:00Z",
                            "summary": "Met Isaac to discuss drone software.",
                            "participants": [
                                {"entity_change_id": "operator", "role": "operator"},
                                {"entity_change_id": "isaac", "role": "participant"},
                            ],
                            "organization_change_ids": ["cs-department"],
                        },
                        "affected_fields": ["interaction"],
                        "basis_refs": basis,
                    },
                ],
            }
        )
        result = InteractiveAuthorityService(session).process_turn(
            utterance_ref=utterance_ref,
            request_key=request_key,
            actor_id=get_settings().operator_discord_user_id,
            intent_session_ref=None,
            expected_session_version=None,
            statements=[statement_input],
            relations=[],
            resolved_intent_json={"kind": "registry_authoring"},
            blocking_clarifications=[],
            content=content,
            changeset_ref=None,
            expected_changeset_version=None,
        )
        assert result["state"] == "committed"
        assert len(result["affected_refs"]) == 10
        isaac = session.scalar(select(Entity).where(Entity.display_name == "Isaac"))
        cs_department = session.scalar(
            select(Entity).where(Entity.display_name == "Computer Science Department")
        )
        operator = session.scalar(select(PersonProfile).where(PersonProfile.is_operator))
        assert isaac is not None and cs_department is not None and operator is not None
        assert isaac.ref_id == isaac_planned_ref
        graph = NetworkQueryService(session)
        search_result = graph.network_search(
            query="isaac", entity_kinds=["person"], cursor=None, limit=25
        )
        assert search_result["items"][0]["ref"] == isaac.ref_id
        person_context = graph.person_context(isaac.ref_id)
        assert person_context["current_affiliations"][0]["role"] == (
            "provisional department head"
        )
        organization_context = graph.organization_context(cs_department.ref_id)
        assert organization_context["known_people"][0]["ref"] == isaac.ref_id
        people = graph.query_people(
            affiliated_with=cs_department.ref_id,
            shares_course_with_operator=False,
            known_through=None,
            relationship_type=None,
            current_role="department head",
            interaction_recency_days=None,
            fact_constraints={"area_of_work": "drone software"},
            cursor=None,
            limit=25,
        )
        assert people["items"][0]["ref"] == isaac.ref_id
        operator_entity = session.get(Entity, operator.entity_id)
        assert operator_entity is not None
        neighborhood = graph.neighborhood(
            root_ref=operator_entity.ref_id, depth=2, max_nodes=100
        )
        assert isaac.ref_id in {item["ref"] for item in neighborhood["nodes"]}

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Entity)) == 5
        assert session.scalar(select(func.count()).select_from(PersonProfile)) == 2
        assert session.scalar(select(func.count()).select_from(OrganizationInstitutionProfile)) == 3
        assert session.scalar(select(func.count()).select_from(IdentityHandle)) == 1
        assert session.scalar(select(func.count()).select_from(IdentityBinding)) == 1
        assert session.scalar(select(func.count()).select_from(Affiliation)) == 1
        assert session.scalar(select(func.count()).select_from(Relationship)) == 1
        assert session.scalar(select(func.count()).select_from(Fact)) == 1
        assert session.scalar(select(func.count()).select_from(Interaction)) == 1
        assert session.scalar(select(func.count()).select_from(InteractionParticipant)) == 2
        changeset = session.scalar(select(ChangeSet))
        fact = session.scalar(select(Fact))
        assert changeset is not None and fact is not None
        assert fact.created_by_changeset_ref == changeset.ref_id
        assert fact.basis_refs == [statement.ref_id]
        assert ProvenanceRefService(session).authority_utterance_refs(
            fact.basis_refs
        ) == {utterance_ref}
        fact_history = HistoryService(session).get_entry(fact.ref_id, view="audit")
        assert fact_history["entry"]["subject_ref"] in {
            entity.ref_id
            for entity in session.scalars(
                select(Entity).where(Entity.display_name == "Isaac")
            )
        }
        assert fact_history["entry"]["value_json"] == "drone software"
        related_history = HistoryService(session).search(
            related_ref=fact_history["entry"]["subject_ref"], limit=25
        )
        assert {item["type"] for item in related_history["items"]}.issuperset(
            {"fact", "affiliation", "relationship"}
        )
        hierarchy = list(
            session.scalars(
                select(OrganizationInstitutionProfile).order_by(OrganizationInstitutionProfile.created_at)
            )
        )
        assert hierarchy[0].parent_entity_id is None
        assert hierarchy[1].parent_entity_id == hierarchy[0].entity_id
        assert hierarchy[2].parent_entity_id == hierarchy[1].entity_id


@pytest.mark.integration
def test_unknown_identity_is_not_a_person_and_similarity_is_advisory(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        utterance_ref, _request_key = _capture(
            session,
            message_id="1542800000000000002",
            text="I know Isaac Newton.",
        )
        known = Entity(
            entity_kind="person",
            display_name="Isaac Newton",
            normalized_name="isaac newton",
            canonical_status="active",
            attributes_json={},
            basis_refs=[utterance_ref],
            decision_refs=[],
            source_refs=[],
            created_by_changeset_ref=new_public_ref("chg"),
        )
        session.add(known)
        session.flush()
        resolver = DeterministicIdentityResolutionService(session)
        handle = resolver.observe_unbound_handle(
            handle_type="email",
            value="isaac@example.com",
            source_refs=[utterance_ref],
        )
        assert handle.status == "unbound"
        assert handle.entity_id is None
        unresolved = resolver.resolve(
            IdentityResolutionRequest(
                mention="Isaak Newton",
                entity_kind="person",
                basis_refs=[utterance_ref],
            )
        )
        assert unresolved["state"] == "unresolved"
        assert unresolved["entity_ref"] is None
        assert unresolved["suggestions"] == [
            {"ref": known.ref_id, "display_name": "Isaac Newton"}
        ]
        other = Entity(
            entity_kind="person",
            display_name="Isaac Chen",
            normalized_name="isaac chen",
            canonical_status="active",
            attributes_json={},
            basis_refs=[utterance_ref],
            decision_refs=[],
            source_refs=[],
            created_by_changeset_ref=new_public_ref("chg"),
        )
        session.add(other)
        session.flush()
        session.add_all(
            [
                EntityAlias(
                    entity_id=known.id,
                    alias="Isaac",
                    normalized_alias="isaac",
                    authority="operator_utterance",
                ),
                EntityAlias(
                    entity_id=other.id,
                    alias="Isaac",
                    normalized_alias="isaac",
                    authority="operator_utterance",
                ),
            ]
        )
        session.flush()
        ambiguous = resolver.resolve(
            IdentityResolutionRequest(mention="Isaac", basis_refs=[utterance_ref])
        )
        assert ambiguous["state"] == "ambiguous"
        assert {item["ref"] for item in ambiguous["candidates"]} == {
            known.ref_id,
            other.ref_id,
        }
        handle.entity_id = known.id
        handle.binding_rule = "operator_selection"
        handle.binding_basis_refs = [utterance_ref]
        handle.status = "bound"
        handle.version += 1
        resolved = resolver.resolve(
            IdentityResolutionRequest(
                handle_type="email",
                handle_value="ISAAC@example.com",
                basis_refs=[utterance_ref],
            )
        )
        assert resolved["state"] == "resolved"
        assert resolved["entity_ref"] == known.ref_id
        assert resolved["resolution_rule"] == "exact_identity_handle"

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Entity)) == 2
        assert session.scalar(select(func.count()).select_from(IdentityHandle)) == 1


@pytest.mark.integration
def test_fact_supersession_preserves_historical_assertion(session_factory) -> None:
    with session_factory.begin() as session:
        first_utterance_ref, first_request_key = _capture(
            session,
            message_id="1542800000000000003",
            text="Register Professor Lupo; his office hours are Monday at 3.",
        )
        prior_statement_input = StatementInput.model_validate(
            {
                "statement_kind": "fact_assertion",
                "subject_refs": [first_utterance_ref],
                "predicate": "office_hours",
                "value_json": "Monday 3 PM",
                "affected_fields": ["office_hours"],
                "interpreter_version": "registry-test-v1",
            }
        )
        prior_statement = StatementService(session).derive(
            first_utterance_ref, [prior_statement_input]
        )[0]
        first_basis = [prior_statement.ref_id]
        first_content = ChangeSetContent.model_validate(
            {
                "basis_refs": first_basis,
                "registry_changes": [
                    {
                        "mutation_type": "entity_create",
                        "change_id": "professor-lupo",
                        "action": "create",
                        "object_type": "entity",
                        "create_spec": {
                            "entity_kind": "person",
                            "display_name": "Professor Lupo",
                        },
                        "affected_fields": ["identity"],
                        "basis_refs": first_basis,
                    },
                    {
                        "mutation_type": "fact_create",
                        "change_id": "monday-hours",
                        "action": "create",
                        "object_type": "fact",
                        "create_spec": {
                            "subject_change_id": "professor-lupo",
                            "predicate": "office_hours",
                            "value_json": "Monday 3 PM",
                        },
                        "affected_fields": ["office_hours"],
                        "basis_refs": first_basis,
                    },
                ],
            }
        )
        first = InteractiveAuthorityService(session).process_turn(
            utterance_ref=first_utterance_ref,
            request_key=first_request_key,
            actor_id=get_settings().operator_discord_user_id,
            intent_session_ref=None,
            expected_session_version=None,
            statements=[prior_statement_input],
            relations=[],
            resolved_intent_json={"kind": "registry_authoring"},
            blocking_clarifications=[],
            content=first_content,
            changeset_ref=None,
            expected_changeset_version=None,
        )
        assert first["state"] == "committed"
        person = session.scalar(select(Entity).where(Entity.display_name == "Professor Lupo"))
        prior = session.scalar(select(Fact))
        assert person is not None and prior is not None

        correction_ref, correction_key = _capture(
            session,
            message_id="1542800000000000004",
            text="His office hours moved to Tuesdays at 3.",
        )
        correction_input = StatementInput.model_validate(
            {
                "statement_kind": "fact_assertion",
                "subject_refs": [person.ref_id],
                "predicate": "office_hours",
                "value_json": "Tuesday 3 PM",
                "affected_fields": ["office_hours"],
                "effective_from": "2026-08-28",
                "interpreter_version": "registry-test-v1",
            }
        )
        correction = StatementService(session).derive(
            correction_ref, [correction_input]
        )[0]
        relation = StatementRelationInput(
            source_statement_ref=correction.ref_id,
            target_statement_ref=prior_statement.ref_id,
            relation_kind="supersedes",
        )
        correction_basis = [correction.ref_id]
        correction_content = ChangeSetContent.model_validate(
            {
                "basis_refs": correction_basis,
                "expected_versions": {prior.ref_id: prior.version},
                "registry_changes": [
                    {
                        "mutation_type": "fact_supersede",
                        "change_id": "replace-office-hours",
                        "action": "supersede",
                        "object_type": "fact",
                        "object_ref": prior.ref_id,
                        "affected_fields": ["office_hours"],
                        "basis_refs": correction_basis,
                        "payload": {
                            "replacement": {
                                "subject_ref": person.ref_id,
                                "predicate": "office_hours",
                                "value_json": "Tuesday 3 PM",
                                "valid_from": "2026-08-28",
                            }
                        },
                    }
                ],
            }
        )
        second = InteractiveAuthorityService(session).process_turn(
            utterance_ref=correction_ref,
            request_key=correction_key,
            actor_id=get_settings().operator_discord_user_id,
            intent_session_ref=None,
            expected_session_version=None,
            statements=[correction_input],
            relations=[relation],
            resolved_intent_json={"kind": "explicit_correction"},
            blocking_clarifications=[],
            content=correction_content,
            changeset_ref=None,
            expected_changeset_version=None,
        )
        assert second["state"] == "committed"

    with session_factory() as session:
        facts = list(session.scalars(select(Fact).order_by(Fact.created_at)))
        assert [fact.status for fact in facts] == ["historical", "active"]
        assert [fact.value_json for fact in facts] == ["Monday 3 PM", "Tuesday 3 PM"]


@pytest.mark.integration
def test_ambiguous_fact_contradiction_opens_conflict_and_preserves_canonical_value(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        registration_ref, _registration_key = _capture(
            session,
            message_id="1542800000000000005",
            text="Register Chris.",
        )
        person = Entity(
            entity_kind="person",
            display_name="Chris",
            normalized_name="chris",
            canonical_status="active",
            attributes_json={},
            basis_refs=[registration_ref],
            decision_refs=[],
            source_refs=[],
            created_by_changeset_ref=new_public_ref("chg"),
        )
        session.add(person)
        session.flush()

        prior_ref, prior_key = _capture(
            session,
            message_id="1542800000000000006",
            text="Chris office hours are Monday at 3.",
        )
        prior_input = StatementInput.model_validate(
            {
                "statement_kind": "fact_assertion",
                "subject_refs": [person.ref_id],
                "predicate": "office_hours",
                "value_json": "Monday 3 PM",
                "affected_fields": ["office_hours"],
                "interpreter_version": "registry-test-v1",
            }
        )
        prior_statement = StatementService(session).derive(prior_ref, [prior_input])[0]
        prior_basis = [prior_statement.ref_id]
        prior_content = ChangeSetContent.model_validate(
            {
                "basis_refs": prior_basis,
                "registry_changes": [
                    {
                        "mutation_type": "fact_create",
                        "change_id": "monday-hours",
                        "action": "create",
                        "object_type": "fact",
                        "create_spec": {
                            "subject_ref": person.ref_id,
                            "predicate": "office_hours",
                            "value_json": "Monday 3 PM",
                        },
                        "affected_fields": ["office_hours"],
                        "basis_refs": prior_basis,
                    }
                ],
            }
        )
        prior_result = InteractiveAuthorityService(session).process_turn(
            utterance_ref=prior_ref,
            request_key=prior_key,
            actor_id=get_settings().operator_discord_user_id,
            intent_session_ref=None,
            expected_session_version=None,
            statements=[prior_input],
            relations=[],
            resolved_intent_json={"kind": "fact_assertion"},
            blocking_clarifications=[],
            content=prior_content,
            changeset_ref=None,
            expected_changeset_version=None,
        )
        assert prior_result["state"] == "committed"

        incoming_ref, incoming_key = _capture(
            session,
            message_id="1542800000000000007",
            text="Chris office hours are Wednesday at 2.",
        )
        incoming_input = prior_input.model_copy(
            update={"value_json": "Wednesday 2 PM"}
        )
        incoming_statement = StatementService(session).derive(
            incoming_ref, [incoming_input]
        )[0]
        incoming_basis = [incoming_statement.ref_id]
        incoming_content = ChangeSetContent.model_validate(
            {
                "basis_refs": incoming_basis,
                "registry_changes": [
                    {
                        "mutation_type": "fact_create",
                        "change_id": "wednesday-hours",
                        "action": "create",
                        "object_type": "fact",
                        "create_spec": {
                            "subject_ref": person.ref_id,
                            "predicate": "office_hours",
                            "value_json": "Wednesday 2 PM",
                        },
                        "affected_fields": ["office_hours"],
                        "basis_refs": incoming_basis,
                    }
                ],
            }
        )
        result = InteractiveAuthorityService(session).process_turn(
            utterance_ref=incoming_ref,
            request_key=incoming_key,
            actor_id=get_settings().operator_discord_user_id,
            intent_session_ref=None,
            expected_session_version=None,
            statements=[incoming_input],
            relations=[],
            resolved_intent_json={"kind": "fact_assertion"},
            blocking_clarifications=[],
            content=incoming_content,
            changeset_ref=None,
            expected_changeset_version=None,
        )
        assert result["state"] == "needs_clarification"
        clarification = result["next"]["clarifications"][0]
        assert clarification["code"] == "ambiguous_contradiction"

    with session_factory() as session:
        facts = list(session.scalars(select(Fact)))
        conflict = session.scalar(select(Conflict))
        assert len(facts) == 1
        assert facts[0].value_json == "Monday 3 PM"
        assert conflict is not None and conflict.status == "open"
        assert conflict.prior_statement_refs == [prior_statement.ref_id]
        assert conflict.incoming_statement_refs == [incoming_statement.ref_id]


def test_network_outputs_compact_by_serialized_utf8_bytes() -> None:
    items = [
        {"ref": f"ent_{index:026d}", "display_name": "é" * 1200}
        for index in range(300)
    ]
    page = NetworkQueryService._page(items, limit=25, cursor=None)
    assert len(json.dumps(page, separators=(",", ":")).encode("utf-8")) <= 16384
    assert page["count"] < 25
    assert page["truncated"] is True
    assert page["cursor"] == str(page["count"])

    context = NetworkQueryService._bounded_context(
        {
            "ok": True,
            "ref": "ent_01M13RZZZZZZZZZZZZZZZZZZZZ",
            "relationships": [
                {"ref": f"rel_{index:026d}", "context": "☕" * 2000}
                for index in range(100)
            ],
        }
    )
    assert len(json.dumps(context, separators=(",", ":")).encode("utf-8")) <= 16384
    assert context["warnings"] == ["output_truncated:relationships"]
