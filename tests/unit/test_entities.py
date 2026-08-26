from docket.domain.enums import IntentAuthority
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
