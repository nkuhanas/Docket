import pytest

from docket.domain.enums import IntentAuthority
from docket.domain.errors import DocketError
from docket.models import EntityResolution
from docket.services.entities import EntityService


def test_entity_registry_resolves_aliases_and_preserves_ambiguity(session) -> None:
    service = EntityService(session)
    unresolved = service.resolve(entity_class="organization", mention="Robotics Club")
    assert unresolved.state == "unresolved"

    robotics = service.create(
        entity_class="organization",
        canonical_name="Cal Poly Robotics Club",
        attributes={"context": "club"},
        authority=IntentAuthority.EXPLICIT_USER,
    )
    service.add_alias(
        entity_id=robotics.entity_id,
        alias="Robotics Club",
        authority=IntentAuthority.EXPLICIT_USER,
    )
    resolved = service.resolve(entity_class="organization", mention="robotics   club")
    assert resolved.state == "resolved"
    assert resolved.resolved_entity is not None
    assert resolved.resolved_entity.entity_id == robotics.entity_id

    lab = service.create(
        entity_class="organization",
        canonical_name="Robotics Research Lab",
        attributes={"context": "department"},
        authority=IntentAuthority.EXPLICIT_USER,
    )
    service.add_alias(
        entity_id=lab.entity_id,
        alias="Robotics Club",
        authority=IntentAuthority.INFERRED,
        confidence=0.5,
    )
    ambiguous = service.resolve(entity_class="organization", mention="Robotics Club")
    assert ambiguous.state == "ambiguous"
    assert {candidate.entity_id for candidate in ambiguous.candidates} == {
        robotics.entity_id,
        lab.entity_id,
    }

    corrected = service.rebind_resolution(
        resolution_id=ambiguous.resolution_id,
        entity_id=robotics.entity_id,
        actor_id="000000000000000001",
    )
    assert corrected.state == "resolved"
    assert corrected.resolved_entity is not None
    assert corrected.resolved_entity.entity_id == robotics.entity_id
    future = service.resolve(entity_class="organization", mention="Robotics Club")
    assert future.state == "resolved"
    assert future.resolved_entity is not None
    assert future.resolved_entity.entity_id == robotics.entity_id

    institution = service.create(
        entity_class="institution",
        canonical_name="California Polytechnic State University, San Luis Obispo",
        attributes={},
        authority=IntentAuthority.EXPLICIT_USER,
    )
    service.relate(
        subject_entity_id=lab.entity_id,
        predicate="affiliated_with",
        object_entity_id=institution.entity_id,
        authority=IntentAuthority.EXPLICIT_USER,
    )
    service.merge(
        survivor_id=robotics.entity_id,
        absorbed_id=lab.entity_id,
        authority=IntentAuthority.EXPLICIT_USER,
        actor_id="000000000000000001",
    )
    relationships = service.relationships(robotics.entity_id)
    assert len(relationships) == 1
    assert relationships[0]["predicate"] == "affiliated_with"
    assert relationships[0]["subject"]["entity_id"] == str(robotics.entity_id)
    assert relationships[0]["object"]["entity_id"] == str(institution.entity_id)


def test_inferred_unknown_entity_is_explicitly_provisional(session) -> None:
    result = EntityService(session).resolve(
        entity_class="location",
        mention="Building 192, room 106",
        allow_provisional=True,
    )

    assert result.state == "provisional"
    assert result.resolved_entity is not None
    assert result.resolved_entity.status == "provisional"
    assert result.resolved_entity.authority == "inferred"


def test_explicit_registration_resolves_an_existing_unknown_mention(session) -> None:
    service = EntityService(session)
    unresolved = service.resolve(entity_class="organization", mention="PolyUAS")
    assert unresolved.state == "unresolved"

    entity = service.create(
        entity_class="organization",
        canonical_name="PolyUAS",
        attributes={"context": "club"},
        authority=IntentAuthority.EXPLICIT_USER,
    )

    persisted = session.get(EntityResolution, unresolved.resolution_id)
    assert persisted is not None
    assert persisted.state == "resolved"
    assert persisted.resolved_entity_id == entity.entity_id


def test_entity_profiles_are_patchable_and_searchable_through_relationships(session) -> None:
    service = EntityService(session)
    operator = service.create(
        entity_class="person",
        canonical_name="Nico Kuhanas",
        attributes={
            "preferred_name": "Nico",
            "email_addresses": ["nico@example.test"],
            "notes": "Prefers concise messages",
            "is_operator": True,
        },
        authority=IntentAuthority.EXPLICIT_USER,
    )
    advisor = service.create(
        entity_class="person",
        canonical_name="Dr. Ada Advisor",
        attributes={"job_title": "Faculty Advisor", "department": "Computer Science"},
        authority=IntentAuthority.EXPLICIT_USER,
    )
    service.add_alias(
        entity_id=advisor.entity_id,
        alias="Ada",
        authority=IntentAuthority.EXPLICIT_USER,
    )
    relation_id = service.relate(
        subject_entity_id=advisor.entity_id,
        predicate="advises",
        object_entity_id=operator.entity_id,
        attributes={"role": "Academic advisor", "primary": True},
        authority=IntentAuthority.EXPLICIT_USER,
    )

    operators = service.search(entity_class="person", is_operator=True)
    assert [result.entity.entity_id for result in operators] == [operator.entity_id]
    advisors = service.search(
        entity_class="person",
        predicate="advises",
        related_entity_id=operator.entity_id,
        direction="subject",
    )
    assert [result.entity.entity_id for result in advisors] == [advisor.entity_id]
    assert advisors[0].aliases[0].alias == "Ada"
    assert advisors[0].relationships[0].relation_id == relation_id
    assert advisors[0].relationships[0].attributes.role == "Academic advisor"

    updated = service.update(
        entity_id=operator.entity_id,
        expected_version=operator.version,
        canonical_name=None,
        attribute_updates={"preferred_contact_method": "email"},
        remove_attribute_keys=["notes"],
        authority=IntentAuthority.EXPLICIT_USER,
    )
    assert updated.attributes.email_addresses == ["nico@example.test"]
    assert updated.attributes.preferred_contact_method == "email"
    assert updated.attributes.notes is None


def test_entity_relationship_corrections_are_explicit_and_reversible(session) -> None:
    service = EntityService(session)
    person = service.create(
        entity_class="person",
        canonical_name="Pat Person",
        attributes={},
        authority=IntentAuthority.EXPLICIT_USER,
    )
    organization = service.create(
        entity_class="organization",
        canonical_name="Example Organization",
        attributes={},
        authority=IntentAuthority.EXPLICIT_USER,
    )
    relation_id = service.relate(
        subject_entity_id=person.entity_id,
        predicate="member_of",
        object_entity_id=organization.entity_id,
        attributes={"role": "Member"},
        authority=IntentAuthority.EXPLICIT_USER,
    )
    with pytest.raises(DocketError) as conflict:
        service.relate(
            subject_entity_id=person.entity_id,
            predicate="member_of",
            object_entity_id=organization.entity_id,
            attributes={"role": "President"},
            authority=IntentAuthority.EXPLICIT_USER,
        )
    assert conflict.value.code == "entity_relation_exists"

    relation = service.update_relation(
        relation_id=relation_id,
        expected_version=1,
        attributes={"role": "President", "start_date": "2026-08-01"},
        authority=IntentAuthority.EXPLICIT_USER,
    )
    assert relation["attributes"]["role"] == "President"
    assert relation["version"] == 2
    retracted = service.retract_relation(
        relation_id=relation_id,
        expected_version=2,
        reason="The term ended.",
    )
    assert retracted == {
        "relation_id": str(relation_id),
        "status": "retracted",
        "version": 3,
    }
    assert service.search(
        predicate="member_of",
        related_entity_id=organization.entity_id,
        direction="subject",
    ) == []


def test_only_one_person_can_be_the_operator(session) -> None:
    service = EntityService(session)
    service.create(
        entity_class="person",
        canonical_name="First Operator",
        attributes={"is_operator": True},
        authority=IntentAuthority.EXPLICIT_USER,
    )

    with pytest.raises(DocketError) as duplicate:
        service.create(
            entity_class="person",
            canonical_name="Second Operator",
            attributes={"is_operator": True},
            authority=IntentAuthority.EXPLICIT_USER,
        )
    assert duplicate.value.code == "operator_entity_exists"

    with pytest.raises(DocketError) as wrong_class:
        service.create(
            entity_class="organization",
            canonical_name="Operator Organization",
            attributes={"is_operator": True},
            authority=IntentAuthority.EXPLICIT_USER,
        )
    assert wrong_class.value.code == "invalid_operator_entity"


def test_existing_entity_metadata_is_not_silently_overwritten_by_create(session) -> None:
    service = EntityService(session)
    existing = service.create(
        entity_class="person",
        canonical_name="Existing Person",
        attributes={"job_title": "Engineer"},
        authority=IntentAuthority.EXPLICIT_USER,
    )

    with pytest.raises(DocketError) as conflict:
        service.create(
            entity_class="person",
            canonical_name="Existing Person",
            attributes={"job_title": "Manager"},
            authority=IntentAuthority.EXPLICIT_USER,
        )
    assert conflict.value.code == "entity_exists"
    assert service.snapshot(existing.entity_id).entity.attributes.job_title == "Engineer"
