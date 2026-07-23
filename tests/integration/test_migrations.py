import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

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
        "calendar_schedule_snapshots",
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
        "discord_daily_threads",
        "discord_projections",
    }.issubset(set(inspect(engine).get_table_names()))
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
