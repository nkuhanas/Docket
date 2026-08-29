import uuid
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, select

from docket.config import clear_settings_cache
from docket.domain.public_refs import new_public_ref


@pytest.mark.integration
def test_initial_migration_upgrades_and_downgrades(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "migration.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    monkeypatch.setenv("DOCKET_DATABASE_URL", database_url)
    clear_settings_cache()
    config = Config("alembic.ini")

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    assert {
        "action_revisions",
        "actions",
        "accounts",
        "approvals",
        "calendar_links",
        "calendar_sync_states",
        "calendar_event_cache",
        "calendar_profiles",
        "calendar_reminder_plans",
        "reminder_rules",
        "scheduled_notifications",
        "records",
        "record_sources",
        "command_requests",
        "execution_attempts",
        "operations",
        "operation_items",
        "outbox_events",
        "queue_items",
        "audit_events",
        "backup_runs",
        "connector_checkpoints",
        "discord_daily_threads",
        "discord_projections",
        "queue_item_sources",
        "source_items",
        "operator_utterances",
        "agent_responses",
        "agent_response_projections",
        "interpreted_statements",
        "statement_relations",
        "decisions",
        "deferred_ingress",
        "drain_barriers",
        "execution_leases",
        "gateway_lifetimes",
        "tool_invocations",
        "runtime_log_entries",
        "intent_sessions",
        "intent_turns",
        "semantic_requests",
        "semantic_request_attempts",
        "change_sets",
        "change_set_revisions",
        "semantic_prompt_projections",
        "persisted_semantic_options",
        "conflicts",
        "provenance_sources",
        "person_profiles",
        "organization_profiles",
        "identity_handles",
        "identity_bindings",
        "sender_identity_emails",
        "affiliations",
        "relationships",
        "facts",
        "interactions",
        "interaction_participants",
        "triage_runs",
        "context_packets",
        "attention_cases",
        "attention_case_revisions",
        "case_items",
        "case_sources",
        "triage_brief_entries",
        "daily_brief_case_items",
        "preferences",
        "lane_routing_decisions",
    }.issubset(set(inspect(engine).get_table_names()))
    assert "calendar_schedule_snapshots" not in inspect(engine).get_table_names()
    assert "synced_snapshot" in {
        column["name"] for column in inspect(engine).get_columns("calendar_links")
    }
    assert "lifecycle_version" in {
        column["name"] for column in inspect(engine).get_columns("discord_daily_threads")
    }
    assert "queue_channel_id" in {
        column["name"] for column in inspect(engine).get_columns("reminder_rules")
    }
    assert "destination_channel_id" not in {
        column["name"] for column in inspect(engine).get_columns("reminder_rules")
    }
    assert "source_kind" not in {
        column["name"] for column in inspect(engine).get_columns("reminder_rules")
    }
    assert "daily_thread_id" in {
        column["name"] for column in inspect(engine).get_columns("scheduled_notifications")
    }
    assert "logical_key" in {
        column["name"] for column in inspect(engine).get_columns("calendar_links")
    }
    assert "provider_reminders" in {
        column["name"] for column in inspect(engine).get_columns("calendar_event_cache")
    }
    assert "operation_item_id" in {
        column["name"] for column in inspect(engine).get_columns("execution_attempts")
    }
    for public_table in (
        "accounts",
        "audit_events",
        "entities",
        "canonical_events",
        "daily_briefs",
        "source_items",
        "operations",
        "calendar_lanes",
        "provenance_sources",
        "identity_handles",
        "affiliations",
        "relationships",
        "facts",
        "interactions",
        "triage_runs",
        "context_packets",
        "attention_cases",
        "attention_case_revisions",
        "case_items",
        "triage_brief_entries",
        "preferences",
        "lane_routing_decisions",
        "gateway_lifetimes",
        "drain_barriers",
        "execution_leases",
        "deferred_ingress",
        "semantic_requests",
        "semantic_request_attempts",
        "semantic_prompt_projections",
    ):
        assert "ref_id" in {column["name"] for column in inspect(engine).get_columns(public_table)}
    assert "legacy_ref_id" in {
        column["name"]
        for column in inspect(engine).get_columns("attention_case_revisions")
    }
    assert "resolution_role" in {
        column["name"] for column in inspect(engine).get_columns("case_items")
    }
    assert "result_disposition" in {
        column["name"] for column in inspect(engine).get_columns("tool_invocations")
    }
    assert {
        "utterance_kind",
        "selected_option_id",
        "authority_scope_hash",
        "selected_precondition_hash",
        "discord_interaction_ref",
    }.issubset(
        {
            column["name"]
            for column in inspect(engine).get_columns("operator_utterances")
        }
    )
    assert {"semantic_state", "commit_state", "semantic_request_ref"}.issubset(
        {
            column["name"]
            for column in inspect(engine).get_columns("intent_sessions")
        }
    )
    assert {"transport_state", "domain_state", "gateway_instance_ref"}.issubset(
        {
            column["name"]
            for column in inspect(engine).get_columns("tool_invocations")
        }
    )
    assert "registration_key" in {
        column["name"]
        for column in inspect(engine).get_columns("gateway_lifetimes")
    }
    assert {
        tuple(constraint["column_names"])
        for constraint in inspect(engine).get_unique_constraints("record_sources")
    } == {("record_id", "source_request_key")}
    action_status = next(
        constraint
        for constraint in inspect(engine).get_check_constraints("actions")
        if constraint["name"] == "ck_actions_status"
    )
    assert "partial_failed" in str(action_status["sqltext"])
    projection_constraints = {
        str(constraint["name"]): str(constraint["sqltext"])
        for constraint in inspect(engine).get_check_constraints("discord_projections")
    }
    assert "brief_review" in projection_constraints["ck_discord_projections_view_mode"]
    assert "65535" in projection_constraints["ck_discord_projections_view_page"]
    case_item_constraints = {
        str(constraint["name"]): str(constraint["sqltext"])
        for constraint in inspect(engine).get_check_constraints("case_items")
    }
    assert "not_pursued" in case_item_constraints["ck_case_items_status"]
    assert "legacy_unspecified" in case_item_constraints[
        "ck_case_items_resolution_role"
    ]

    command.downgrade(config, "base")
    assert "records" not in inspect(engine).get_table_names()
    engine.dispose()
    clear_settings_cache()


@pytest.mark.integration
def test_typed_registry_migration_preserves_legacy_entity_provenance(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "typed-registry.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    monkeypatch.setenv("DOCKET_DATABASE_URL", database_url)
    clear_settings_cache()
    config = Config("alembic.ini")
    command.upgrade(config, "0030")
    engine = create_engine(database_url)
    legacy = MetaData()
    entities = Table("entities", legacy, autoload_with=engine)
    entity_id = uuid.uuid4()
    entity_ref = f"ent_{entity_id.hex[:26].upper()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            entities.insert().values(
                id=entity_id.hex,
                ref_id=entity_ref,
                entity_class="person",
                canonical_name="Migration Operator",
                normalized_name="migration operator",
                status="active",
                attributes={"is_operator": True, "preferred_name": "Operator"},
                authority="explicit_user",
                merged_into_id=None,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )

    command.upgrade(config, "head")
    migrated = MetaData()
    migrated_entities = Table("entities", migrated, autoload_with=engine)
    sources = Table("provenance_sources", migrated, autoload_with=engine)
    profiles = Table("person_profiles", migrated, autoload_with=engine)
    with engine.connect() as connection:
        entity = connection.execute(select(migrated_entities)).mappings().one()
        source = connection.execute(select(sources)).mappings().one()
        profile = connection.execute(select(profiles)).mappings().one()
        assert entity["registration_state"] == "registered"
        assert entity["provenance_status"] == "legacy_preledger"
        assert entity["basis_refs"] == [source["ref_id"]]
        assert entity["source_refs"] == [source["ref_id"]]
        assert source["source_kind"] == "legacy_canonical_object"
        assert source["external_ref"] == entity_ref
        assert profile["is_operator"] is True
        assert profile["preferred_name"] == "Operator"

    command.downgrade(config, "0030")
    assert "provenance_sources" not in inspect(engine).get_table_names()
    assert "registration_state" not in {
        column["name"] for column in inspect(engine).get_columns("entities")
    }
    engine.dispose()
    clear_settings_cache()


@pytest.mark.integration
def test_interactive_continuity_migration_preserves_honest_legacy_states(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "interactive-continuity.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    monkeypatch.setenv("DOCKET_DATABASE_URL", database_url)
    clear_settings_cache()
    config = Config("alembic.ini")
    command.upgrade(config, "0041")
    engine = create_engine(database_url)
    metadata = MetaData()
    sessions = Table("intent_sessions", metadata, autoload_with=engine)
    invocations = Table("tool_invocations", metadata, autoload_with=engine)
    now = datetime.now(UTC)
    session_rows: list[dict[str, object]] = []
    for state in ("open", "needs_clarification", "ready", "committed", "cancelled"):
        session_rows.append(
            {
                "id": uuid.uuid4().hex,
                "ref_id": new_public_ref("ses"),
                "conversation_ref": f"discord:test:{state}",
                "source_utterance_ref": new_public_ref("utt"),
                "case_refs": [],
                "case_revision_refs": [],
                "brief_ref": None,
                "trusted_context_refs": [],
                "resolved_intent_json": {},
                "blocking_clarifications": [],
                "state": state,
                "version": 1,
                "committed_changeset_ref": None,
                "created_at": now,
                "updated_at": now,
            }
        )
    with engine.begin() as connection:
        connection.execute(sessions.insert(), session_rows)
        connection.execute(
            invocations.insert(),
            [
                {
                    "id": uuid.uuid4().hex,
                    "ref_id": new_public_ref("call"),
                    "tool_name": "docket_get_record",
                    "tool_contract_version": "test",
                    "tool_contract_hash": "0" * 64,
                    "caller_profile": "interactive",
                    "actor_ref": None,
                    "utterance_refs": [],
                    "intent_session_ref": None,
                    "case_ref": None,
                    "started_at": now,
                    "completed_at": now,
                    "status": "succeeded",
                    "received_argument_hash": "1" * 64,
                    "normalized_argument_hash": "1" * 64,
                    "result_refs": [],
                    "result_disposition": "succeeded",
                    "error_code": None,
                    "mcp_request_id": None,
                    "trace_id": None,
                    "trace_call_id": None,
                    "trace_ordinal": None,
                },
                {
                    "id": uuid.uuid4().hex,
                    "ref_id": new_public_ref("call"),
                    "tool_name": "docket_commit_changeset",
                    "tool_contract_version": "test",
                    "tool_contract_hash": "0" * 64,
                    "caller_profile": "interactive",
                    "actor_ref": None,
                    "utterance_refs": [],
                    "intent_session_ref": None,
                    "case_ref": None,
                    "started_at": now,
                    "completed_at": now,
                    "status": "rejected_validation",
                    "received_argument_hash": "2" * 64,
                    "normalized_argument_hash": "2" * 64,
                    "result_refs": [],
                    "result_disposition": "rejected_validation",
                    "error_code": "invalid_request",
                    "mcp_request_id": None,
                    "trace_id": None,
                    "trace_call_id": None,
                    "trace_ordinal": None,
                },
            ],
        )

    command.upgrade(config, "head")
    migrated = MetaData()
    migrated_sessions = Table("intent_sessions", migrated, autoload_with=engine)
    migrated_invocations = Table("tool_invocations", migrated, autoload_with=engine)
    with engine.connect() as connection:
        states = {
            row.state: (row.semantic_state, row.commit_state)
            for row in connection.execute(
                select(
                    migrated_sessions.c.state,
                    migrated_sessions.c.semantic_state,
                    migrated_sessions.c.commit_state,
                )
            )
        }
        outcomes = {
            row.status: (row.transport_state, row.domain_state)
            for row in connection.execute(
                select(
                    migrated_invocations.c.status,
                    migrated_invocations.c.transport_state,
                    migrated_invocations.c.domain_state,
                )
            )
        }
    assert states["committed"] == ("ready", "committed")
    assert states["needs_clarification"] == (
        "needs_clarification",
        "not_attempted",
    )
    assert states["ready"] == ("ready", "not_attempted")
    assert outcomes["succeeded"] == ("completed", "succeeded")
    assert outcomes["rejected_validation"] == ("completed", "rejected")

    command.downgrade(config, "0041")
    assert "semantic_state" not in {
        column["name"] for column in inspect(engine).get_columns("intent_sessions")
    }
    assert "semantic_requests" not in inspect(engine).get_table_names()
    engine.dispose()
    clear_settings_cache()


@pytest.mark.integration
def test_case_resolution_migration_types_revision_aliases_and_preserves_bindings(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "attention-case-resolution.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    monkeypatch.setenv("DOCKET_DATABASE_URL", database_url)
    clear_settings_cache()
    config = Config("alembic.ini")
    command.upgrade(config, "0039")
    engine = create_engine(database_url)
    metadata = MetaData()
    cases = Table("attention_cases", metadata, autoload_with=engine)
    revisions = Table("attention_case_revisions", metadata, autoload_with=engine)
    case_items = Table("case_items", metadata, autoload_with=engine)
    sessions = Table("intent_sessions", metadata, autoload_with=engine)
    queues = Table("queue_items", metadata, autoload_with=engine)
    case_id = uuid.uuid4().hex
    revision_id = uuid.uuid4().hex
    item_id = uuid.uuid4().hex
    session_id = uuid.uuid4().hex
    queue_id = uuid.uuid4().hex
    case_ref = new_public_ref("case")
    legacy_revision_ref = new_public_ref("case")
    item_ref = new_public_ref("item")
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            cases.insert().values(
                id=case_id,
                ref_id=case_ref,
                situation_key="a" * 64,
                title="Legacy projected case",
                summary="Legacy case summary",
                status="open",
                priority="normal",
                semantic_classes=["action_request"],
                entity_refs=[],
                source_refs=[],
                latest_revision=1,
                queue_item_id=None,
                resolution_decision_ref=None,
                resolved_at=None,
                first_observed_at=now,
                last_observed_at=now,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            revisions.insert().values(
                id=revision_id,
                ref_id=legacy_revision_ref,
                attention_case_id=case_id,
                case_ref=case_ref,
                revision=1,
                title="Legacy projected case",
                summary="Legacy case summary",
                semantic_classes=["action_request"],
                item_refs=[item_ref],
                source_refs=[],
                content_hash="b" * 64,
                created_at=now,
            )
        )
        connection.execute(
            case_items.insert().values(
                id=item_id,
                ref_id=item_ref,
                attention_case_id=case_id,
                item_key="legacy-item",
                item_type="decision_required",
                status="open",
                payload_json={},
                candidate_refs=[],
                basis_refs=[],
                source_refs=[],
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            sessions.insert().values(
                id=session_id,
                ref_id=new_public_ref("ses"),
                conversation_ref="discord:guild:channel",
                source_utterance_ref=new_public_ref("utt"),
                case_refs=[case_ref],
                case_revision_refs=[legacy_revision_ref],
                brief_ref=None,
                trusted_context_refs=[],
                resolved_intent_json={},
                blocking_clarifications=[],
                state="open",
                version=1,
                committed_changeset_ref=None,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            queues.insert().values(
                id=queue_id,
                primary_source_item_id=None,
                deduplication_key="legacy-case-queue",
                material_fingerprint="c" * 64,
                category="attention_case",
                title="Legacy projected case",
                summary="Legacy case summary",
                status="pending",
                priority="normal",
                presentation="action_required",
                attention_case_ref=case_ref,
                attention_case_revision_ref=legacy_revision_ref,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )

    command.upgrade(config, "0040")
    migrated = MetaData()
    migrated_revisions = Table(
        "attention_case_revisions", migrated, autoload_with=engine
    )
    migrated_items = Table("case_items", migrated, autoload_with=engine)
    migrated_sessions = Table("intent_sessions", migrated, autoload_with=engine)
    migrated_queues = Table("queue_items", migrated, autoload_with=engine)
    with engine.connect() as connection:
        revision = connection.execute(select(migrated_revisions)).mappings().one()
        item = connection.execute(select(migrated_items)).mappings().one()
        intent = connection.execute(select(migrated_sessions)).mappings().one()
        queue = connection.execute(select(migrated_queues)).mappings().one()
        assert revision["ref_id"].startswith("caserev_")
        assert revision["legacy_ref_id"] == legacy_revision_ref
        assert revision["content_hash"] == "b" * 64
        assert item["resolution_role"] == "legacy_unspecified"
        assert intent["case_revision_refs"] == [revision["ref_id"]]
        assert queue["attention_case_revision_ref"] == revision["ref_id"]

    command.downgrade(config, "0039")
    restored = MetaData()
    restored_revisions = Table(
        "attention_case_revisions", restored, autoload_with=engine
    )
    restored_items = Table("case_items", restored, autoload_with=engine)
    restored_sessions = Table("intent_sessions", restored, autoload_with=engine)
    with engine.connect() as connection:
        revision = connection.execute(select(restored_revisions)).mappings().one()
        intent = connection.execute(select(restored_sessions)).mappings().one()
        assert revision["ref_id"] == legacy_revision_ref
        assert intent["case_revision_refs"] == [legacy_revision_ref]
    assert "resolution_role" not in {
        column["name"] for column in inspect(engine).get_columns(restored_items.name)
    }
    engine.dispose()
    clear_settings_cache()


@pytest.mark.integration
def test_tool_outcome_migration_separates_legacy_transport_from_domain_state(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "tool-outcome-reconciliation.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    monkeypatch.setenv("DOCKET_DATABASE_URL", database_url)
    clear_settings_cache()
    config = Config("alembic.ini")
    command.upgrade(config, "0040")
    engine = create_engine(database_url)
    metadata = MetaData()
    traces = Table("discord_mcp_traces", metadata, autoload_with=engine)
    now = datetime.now(UTC)
    trace_id = uuid.uuid4().hex
    with engine.begin() as connection:
        connection.execute(
            traces.insert().values(
                id=trace_id,
                guild_id="1",
                source_channel_id="2",
                source_message_id="3",
                actor_id="4",
                status="completed",
                calls=[
                    {
                        "call_id": "legacy-call",
                        "ordinal": 1,
                        "tool_name": "docket_search_records",
                        "state": "succeeded",
                        "elapsed_ms": 12,
                        "disposition": "succeeded",
                        "error_code": None,
                        "argument_preview": '{"fields":["query"]}',
                    }
                ],
                last_ordinal=1,
                version=1,
                started_at=now,
                completed_at=now,
                created_at=now,
                updated_at=now,
            )
        )

    command.upgrade(config, "0041")
    migrated = Table(
        "discord_mcp_traces", MetaData(), autoload_with=engine
    )
    with engine.connect() as connection:
        call = connection.execute(select(migrated.c.calls)).scalar_one()[0]
        assert call["transport_state"] == "completed"
        assert call["domain_state"] == "unknown"
        assert call["tool_call_ref"] is None
        assert "state" not in call

    command.downgrade(config, "0040")
    restored = Table(
        "discord_mcp_traces", MetaData(), autoload_with=engine
    )
    with engine.connect() as connection:
        call = connection.execute(select(restored.c.calls)).scalar_one()[0]
        assert call["state"] == "failed"
        assert "domain_state" not in call
    engine.dispose()
    clear_settings_cache()


@pytest.mark.integration
def test_passive_gmail_notification_migration_removes_only_local_controls(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "passive-gmail-notifications.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    monkeypatch.setenv("DOCKET_DATABASE_URL", database_url)
    clear_settings_cache()
    config = Config("alembic.ini")
    command.upgrade(config, "0015")

    engine = create_engine(database_url)
    metadata = MetaData()
    accounts = Table("accounts", metadata, autoload_with=engine)
    sources = Table("source_items", metadata, autoload_with=engine)
    queue_items = Table("queue_items", metadata, autoload_with=engine)
    actions = Table("actions", metadata, autoload_with=engine)
    now = datetime.now(UTC)
    account_id = uuid.uuid4().hex
    passive_source_id = uuid.uuid4().hex
    approval_source_id = uuid.uuid4().hex
    passive_queue_id = uuid.uuid4().hex
    approval_queue_id = uuid.uuid4().hex
    with engine.begin() as connection:
        connection.execute(
            accounts.insert().values(
                id=account_id,
                provider="google",
                external_account_id="gmail-migration-test",
                display_name="Gmail migration test",
                email_address=None,
                capabilities=["gmail"],
                enabled=True,
                credential_ref=None,
                created_at=now,
                updated_at=now,
            )
        )
        for source_id, object_id, fingerprint, action_types in (
            (passive_source_id, "passive", "1" * 64, []),
            (
                approval_source_id,
                "approval",
                "2" * 64,
                ["gmail_archive_message"],
            ),
        ):
            connection.execute(
                sources.insert().values(
                    id=source_id,
                    account_id=account_id,
                    provider="gmail",
                    external_object_id=object_id,
                    external_parent_id=None,
                    source_version="1",
                    source_fingerprint=fingerprint,
                    received_at=now,
                    minimal_headers={},
                    status="classified",
                    claim_token=None,
                    claimed_by=None,
                    claimed_until=None,
                    classification={
                        "decision": "actionable",
                        "action_types": action_types,
                    },
                    failure_count=0,
                    next_attempt_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
        for queue_id, source_id, suffix, status in (
            (passive_queue_id, passive_source_id, "passive", "pending"),
            (
                approval_queue_id,
                approval_source_id,
                "approval",
                "awaiting_approval",
            ),
        ):
            connection.execute(
                queue_items.insert().values(
                    id=queue_id,
                    primary_source_item_id=source_id,
                    deduplication_key=f"gmail:{suffix}",
                    material_fingerprint=suffix[0] * 64,
                    category="school_notice",
                    title=suffix.title(),
                    summary=f"{suffix.title()} migration fixture.",
                    status=status,
                    priority="normal",
                    received_at=now,
                    snoozed_until=None,
                    snooze_local_date=None,
                    resolved_at=None,
                    resolution_code=None,
                    resolution_note=None,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        connection.execute(
            actions.insert(),
            [
                {
                    "id": uuid.uuid4().hex,
                    "queue_item_id": passive_queue_id,
                    "record_id": None,
                    "action_type": "snooze_queue_item",
                    "status": "available",
                    "current_revision": 1,
                    "display_order": 10,
                    "created_at": now,
                    "updated_at": now,
                },
                {
                    "id": uuid.uuid4().hex,
                    "queue_item_id": approval_queue_id,
                    "record_id": None,
                    "action_type": "gmail_archive_message",
                    "status": "approval_pending",
                    "current_revision": 1,
                    "display_order": 10,
                    "created_at": now,
                    "updated_at": now,
                },
            ],
        )

    command.upgrade(config, "0016")
    migrated = MetaData()
    migrated_queue_items = Table("queue_items", migrated, autoload_with=engine)
    migrated_actions = Table("actions", migrated, autoload_with=engine)
    migrated_outbox = Table("outbox_events", migrated, autoload_with=engine)
    with engine.connect() as connection:
        passive = (
            connection.execute(
                select(migrated_queue_items).where(migrated_queue_items.c.id == passive_queue_id)
            )
            .mappings()
            .one()
        )
        approval = (
            connection.execute(
                select(migrated_queue_items).where(migrated_queue_items.c.id == approval_queue_id)
            )
            .mappings()
            .one()
        )
        assert passive["status"] == "completed"
        assert passive["resolution_code"] == "gmail_notification"
        assert passive["version"] == 2
        assert approval["status"] == "awaiting_approval"
        assert approval["version"] == 1
        assert (
            connection.scalar(
                select(migrated_actions.c.status).where(
                    migrated_actions.c.queue_item_id == passive_queue_id
                )
            )
            == "superseded"
        )
        refresh = connection.execute(select(migrated_outbox)).mappings().one()
        assert str(refresh["aggregate_id"]).replace("-", "") == passive_queue_id
        assert refresh["payload"]["reason"] == "passive_gmail_notification_migrated"

    command.downgrade(config, "0015")
    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(migrated_queue_items.c.status).where(
                    migrated_queue_items.c.id == passive_queue_id
                )
            )
            == "pending"
        )
        assert (
            connection.scalar(
                select(migrated_actions.c.status).where(
                    migrated_actions.c.queue_item_id == passive_queue_id
                )
            )
            == "available"
        )
        assert connection.scalar(select(migrated_outbox.c.id)) is None

    engine.dispose()
    clear_settings_cache()


@pytest.mark.integration
def test_legacy_reminder_cleanup_preserves_canonical_rules(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "legacy-reminders.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    monkeypatch.setenv("DOCKET_DATABASE_URL", database_url)
    clear_settings_cache()
    config = Config("alembic.ini")
    command.upgrade(config, "0012")

    engine = create_engine(database_url)
    metadata = MetaData()
    accounts = Table("accounts", metadata, autoload_with=engine)
    rules = Table("reminder_rules", metadata, autoload_with=engine)
    notifications = Table("scheduled_notifications", metadata, autoload_with=engine)
    now = datetime.now(UTC)
    account_id = uuid.uuid4().hex
    legacy_rule_id = uuid.uuid4().hex
    canonical_rule_id = uuid.uuid4().hex
    with engine.begin() as connection:
        connection.execute(
            accounts.insert().values(
                id=account_id,
                provider="google",
                external_account_id="migration-test",
                display_name="Migration test",
                email_address=None,
                capabilities=["google_calendar"],
                enabled=True,
                credential_ref=None,
                created_at=now,
                updated_at=now,
            )
        )
        for rule_id, source_kind, provider_event_id in (
            (legacy_rule_id, "legacy_explicit", "legacy-event"),
            (canonical_rule_id, "canonical_plan", "canonical-event"),
        ):
            connection.execute(
                rules.insert().values(
                    id=rule_id,
                    account_id=account_id,
                    calendar_id="migration-calendar",
                    scope="event",
                    provider_event_id=provider_event_id,
                    lead_seconds=600,
                    queue_channel_id="123456789012345678",
                    source_kind=source_kind,
                    enabled=source_kind == "canonical_plan",
                    version=1,
                    created_by_actor_id="325761533034496010",
                    created_at=now,
                    updated_at=now,
                )
            )
        connection.execute(
            notifications.insert().values(
                id=uuid.uuid4().hex,
                reminder_rule_id=legacy_rule_id,
                calendar_event_id=None,
                provider_event_id="legacy-event",
                event_start_key="2026-07-26T12:00:00+00:00",
                scheduled_for=now,
                status="cancelled",
                outbox_event_id=None,
                daily_thread_id=None,
                discord_message_id=None,
                attempt_count=0,
                last_error_code=None,
                created_at=now,
                updated_at=now,
            )
        )

    command.upgrade(config, "head")
    migrated = MetaData()
    migrated_rules = Table("reminder_rules", migrated, autoload_with=engine)
    migrated_notifications = Table("scheduled_notifications", migrated, autoload_with=engine)
    assert "source_kind" not in migrated_rules.c
    with engine.connect() as connection:
        assert connection.scalars(select(migrated_rules.c.id)).all() == [canonical_rule_id]
        assert connection.scalars(select(migrated_notifications.c.id)).all() == []

    command.downgrade(config, "0012")
    downgraded = MetaData()
    downgraded_rules = Table("reminder_rules", downgraded, autoload_with=engine)
    with engine.connect() as connection:
        assert (
            connection.execute(select(downgraded_rules.c.source_kind)).scalar_one()
            == "canonical_plan"
        )

    engine.dispose()
    clear_settings_cache()


@pytest.mark.integration
def test_residual_snoozed_housekeeping_card_is_retired(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "residual-housekeeping.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    monkeypatch.setenv("DOCKET_DATABASE_URL", database_url)
    clear_settings_cache()
    config = Config("alembic.ini")
    command.upgrade(config, "0022")

    engine = create_engine(database_url)
    metadata = MetaData()
    queue_items = Table("queue_items", metadata, autoload_with=engine)
    actions = Table("actions", metadata, autoload_with=engine)
    now = datetime.now(UTC)
    queue_id = uuid.uuid4().hex
    with engine.begin() as connection:
        connection.execute(
            queue_items.insert().values(
                id=queue_id,
                primary_source_item_id=None,
                deduplication_key="gmail:residual-housekeeping",
                material_fingerprint="a" * 64,
                category="application_receipt",
                title="Application received",
                summary="An obsolete alpha housekeeping card.",
                status="snoozed",
                priority="normal",
                presentation="proposal",
                received_at=now,
                snoozed_until=now,
                snooze_local_date=now.date(),
                resolved_at=None,
                resolution_code=None,
                resolution_note=None,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            actions.insert().values(
                id=uuid.uuid4().hex,
                queue_item_id=queue_id,
                record_id=None,
                action_type="gmail_archive_message",
                status="approval_pending",
                current_revision=1,
                display_order=10,
                created_at=now,
                updated_at=now,
            )
        )

    command.upgrade(config, "head")
    migrated = MetaData()
    migrated_queue_items = Table("queue_items", migrated, autoload_with=engine)
    migrated_actions = Table("actions", migrated, autoload_with=engine)
    with engine.connect() as connection:
        queue = (
            connection.execute(
                select(migrated_queue_items).where(migrated_queue_items.c.id == queue_id)
            )
            .mappings()
            .one()
        )
        assert queue["status"] == "completed"
        assert queue["presentation"] == "awareness"
        assert queue["resolution_code"] == "alpha_housekeeping_retired"
        assert (
            connection.scalar(
                select(migrated_actions.c.status).where(
                    migrated_actions.c.queue_item_id == queue_id
                )
            )
            == "superseded"
        )

    engine.dispose()
    clear_settings_cache()
