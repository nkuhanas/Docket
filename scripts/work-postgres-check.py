"""Synthetic work-storage migration, immutability and uniqueness evidence."""

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

from alembic import command
from alembic.config import Config
from sqlalchemy import delete, inspect, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

ROOT = Path(__file__).resolve().parents[1]


async def check():
    from docket.agent_auth import AgentCallContext, AgentPrincipal, agent_call_context
    from docket.config import get_settings
    from docket.database import configure_database, get_session_factory
    from docket.models import (
        AuthenticatedRequest,
        Engagement,
        EngagementRevision,
        WorkSegment,
        WorkSession,
        WorkSessionRevision,
    )
    from docket.plugin.server import PluginMCP

    settings = get_settings()
    url = make_url(settings.database_url)
    assert settings.environment.value == "test" and not settings.external_writes_enabled
    assert url.get_backend_name() == "postgresql"
    assert url.database in {"docket_work_test", "docket_work_smoke"}
    assert url.host in {"127.0.0.1", "localhost", "postgres"}
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "head")
    engine = configure_database(settings.database_url)
    factory = get_session_factory()
    tables = (Engagement, EngagementRevision, WorkSession, WorkSessionRevision, WorkSegment)
    for model in tables:
        columns = {
            column["name"]: column for column in inspect(engine).get_columns(model.__tablename__)
        }
        assert set(columns) == set(model.__table__.columns.keys())
        for column in model.__table__.columns:
            assert columns[column.name]["nullable"] == column.nullable
            assert columns[column.name]["type"]._type_affinity == column.type._type_affinity
        with engine.connect() as connection:
            assert not connection.execute(select(model).limit(1)).first()
    command.downgrade(config, "20261104e3f4")
    assert all(model.__tablename__ not in inspect(engine).get_table_names() for model in tables)
    command.upgrade(config, "head")

    principal = AgentPrincipal(
        "synthetic-work-storage",
        settings.canonical_operator_ref,
        "interactive",
        frozenset({"read", "stage", "commit", "resolve_conflict"}),
    )
    context_token = agent_call_context.set(AgentCallContext(principal))
    try:
        server = PluginMCP(settings)
        overview = (await server.call_tool("docket_get_overview", {})).structured_content
        created = (
            await server.call_tool(
                "docket_create_item",
                {
                    "context_id": overview["context_id"],
                    "operation_id": "work-storage-fixture",
                    "title": "Synthetic storage fixture",
                    "kind": "test.work_storage",
                },
            )
        ).structured_content
        assert created.get("disposition") == "committed", created
    finally:
        agent_call_context.reset(context_token)
    changeset_ref = created["changeset_ref"]
    with factory.begin() as session:
        request_ref = session.scalar(
            select(AuthenticatedRequest.ref_id).where(
                AuthenticatedRequest.committed_changeset_ref == changeset_ref,
            )
        )
        assert request_ref
        engagement = Engagement(
            operator_ref=principal.operator_ref, created_by_changeset_ref=changeset_ref
        )
        work = WorkSession(
            operator_ref=principal.operator_ref,
            lifecycle="recorded",
            created_by_changeset_ref=changeset_ref,
        )
        session.add_all([engagement, work])
        session.flush()
        terms = EngagementRevision(
            engagement_ref=engagement.ref_id,
            revision=1,
            label="Synthetic engagement",
            kind="unknown",
            basis_refs=[request_ref],
            created_by_changeset_ref=changeset_ref,
        )
        revision = WorkSessionRevision(
            work_ref=work.ref_id,
            revision=1,
            lifecycle="recorded",
            superseded=False,
            description="Synthetic work",
            time_precision="interval",
            duration_us=9_000_000_000,
            start_at=datetime(2026, 11, 5, 18, tzinfo=UTC),
            end_at=datetime(2026, 11, 5, 20, 30, tzinfo=UTC),
            start_offset_seconds=-28800,
            end_offset_seconds=-28800,
            timezone="America/Los_Angeles",
            work_date=date(2026, 11, 5),
            engagement_ref=engagement.ref_id,
            reported_compensation_class="billable",
            source_method="report",
            basis_refs=[request_ref],
            source_refs=[],
            created_by_changeset_ref=changeset_ref,
        )
        session.add_all([terms, revision])
        session.flush()
        segment = WorkSegment(
            revision_id=revision.id,
            ordinal=0,
            kind="active",
            start_at=revision.start_at,
            end_at=revision.end_at,
            start_offset_seconds=-28800,
            end_offset_seconds=-28800,
        )
        session.add(segment)
        session.flush()
        retained = [
            (EngagementRevision, terms.id),
            (WorkSessionRevision, revision.id),
            (WorkSegment, segment.id),
        ]
        heads = [(Engagement, engagement.id), (WorkSession, work.id)]
    for model, identity in retained:
        # Core SQL intentionally bypasses the ORM guards to prove PostgreSQL protection.
        for statement in (
            update(model).where(model.id == identity).values(id=identity),
            delete(model).where(model.id == identity),
        ):
            try:
                with factory.begin() as session:
                    session.execute(statement)
            except DBAPIError as exc:
                assert "append-only" in str(exc.orig)
            else:
                raise AssertionError("PostgreSQL allowed immutable work history to change")
    for model, identity in heads:
        try:
            with factory.begin() as session:
                session.execute(delete(model).where(model.id == identity))
        except DBAPIError as exc:
            assert "append-only" in str(exc.orig)
        else:
            raise AssertionError("PostgreSQL deleted a retained work head")

    timer_barrier = Barrier(2)

    def start_timer(_index):
        try:
            with factory.begin() as session:
                session.connection()
                timer_barrier.wait(timeout=10)
                timer = WorkSession(
                    operator_ref=principal.operator_ref,
                    lifecycle="running",
                    created_by_changeset_ref=changeset_ref,
                )
                session.add(timer)
                session.flush()
                session.add(
                    WorkSessionRevision(
                        work_ref=timer.ref_id,
                        revision=1,
                        lifecycle="running",
                        superseded=False,
                        description="Synthetic timer",
                        time_precision="timer",
                        duration_us=0,
                        start_at=datetime.now(UTC),
                        start_offset_seconds=0,
                        timezone="America/Los_Angeles",
                        source_method="timer",
                        basis_refs=[request_ref],
                        source_refs=[],
                        created_by_changeset_ref=changeset_ref,
                    )
                )
            return "created"
        except IntegrityError as exc:
            assert "uq_work_primary_timer" in str(exc.orig)
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(start_timer, (1, 2))) == ["conflict", "created"]
    try:
        command.downgrade(config, "20261104e3f4")
    except RuntimeError as exc:
        assert "cannot discard retained" in str(exc)
    else:
        raise AssertionError("Downgrade discarded populated work history")
    engine.dispose()
    print(
        json.dumps(
            {
                "work_schema_roundtrip": "passed",
                "revision_immutability": "passed",
                "single_primary_timer_race": "passed",
                "retained_downgrade": "refused",
            }
        )
    )


if __name__ == "__main__":
    # Change directory before importing Docket so even import-time settings cannot
    # read the workspace .env; only explicit synthetic validation settings apply.
    with TemporaryDirectory(prefix="docket-work-storage-") as directory:
        os.chdir(directory)
        asyncio.run(check())
