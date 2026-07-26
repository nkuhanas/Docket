import uuid
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, select

from docket.config import clear_settings_cache


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
        "discord_daily_threads",
        "discord_projections",
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

    command.downgrade(config, "base")
    assert "records" not in inspect(engine).get_table_names()
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
    migrated_notifications = Table(
        "scheduled_notifications", migrated, autoload_with=engine
    )
    assert "source_kind" not in migrated_rules.c
    with engine.connect() as connection:
        assert connection.scalars(select(migrated_rules.c.id)).all() == [
            canonical_rule_id
        ]
        assert connection.scalars(select(migrated_notifications.c.id)).all() == []

    command.downgrade(config, "0012")
    downgraded = MetaData()
    downgraded_rules = Table("reminder_rules", downgraded, autoload_with=engine)
    with engine.connect() as connection:
        assert connection.execute(
            select(downgraded_rules.c.source_kind)
        ).scalar_one() == "canonical_plan"

    engine.dispose()
    clear_settings_cache()
