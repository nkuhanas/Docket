"""Synthetic work migration, canonical commands, immutability and race evidence."""

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
        packet = (await server.call_tool("docket_get_work_context", {})).structured_content
        logged_arguments = {
            "context_id": packet["context_id"],
            "operation_id": "canonical-work-fixture",
            "description": "Synthetic performed work",
            "duration_minutes": 150,
            "work_date": "2026-11-05",
            "reported_compensation_class": "billable",
            "attribution_label": "Synthetic unconfigured arrangement",
        }
        logged = (
            await server.call_tool("docket_log_work_session", logged_arguments)
        ).structured_content
        assert logged.get("disposition") == "committed", logged
        replay = (
            await server.call_tool("docket_log_work_session", logged_arguments)
        ).structured_content
        assert replay["replayed"] and replay["changeset_ref"] == logged["changeset_ref"]
        with factory() as session:
            canonical_ref = session.scalar(select(WorkSession.ref_id))
        packet = (
            await server.call_tool(
                "docket_get_work_context",
                {
                    "refs": [canonical_ref],
                },
            )
        ).structured_content
        assert packet["objects"][0]["state"]["duration_us"] == 9_000_000_000
        previews = []
        for index in (1, 2):
            proposal = (
                await server.call_tool(
                    "docket_update_work_session",
                    {
                        "context_id": packet["context_id"],
                        "operation_id": f"racing-work-{index}",
                        "work_ref": canonical_ref,
                        "outcome": f"Synthetic correction {index}",
                        "preview": True,
                    },
                )
            ).structured_content
            assert proposal.get("change_id"), proposal
            previews.append(proposal["change_id"])
    finally:
        agent_call_context.reset(context_token)

    correction_barrier = Barrier(2)

    def apply_correction(change_id):
        async def invoke():
            token = agent_call_context.set(AgentCallContext(principal))
            try:
                endpoint = PluginMCP(settings)
                correction_barrier.wait(timeout=10)
                return (
                    await endpoint.call_tool(
                        "docket_apply_change",
                        {
                            "change_id": change_id,
                        },
                    )
                ).structured_content
            finally:
                agent_call_context.reset(token)

        return asyncio.run(invoke())

    with ThreadPoolExecutor(max_workers=2) as pool:
        corrections = list(pool.map(apply_correction, previews))
    assert sum(result.get("disposition") == "committed" for result in corrections) == 1, corrections
    assert sum(result.get("ok") is False for result in corrections) == 1, corrections
    rejected = next(result for result in corrections if result.get("ok") is False)
    assert rejected["error"]["code"] in {
        "work_version_conflict", "changeset_validation_failed",
    }, rejected
    with factory() as session:
        head = session.scalar(select(WorkSession).where(WorkSession.ref_id == canonical_ref))
        assert head.version == 2
        revisions = list(
            session.scalars(
                select(WorkSessionRevision)
                .where(
                    WorkSessionRevision.work_ref == canonical_ref,
                )
                .order_by(WorkSessionRevision.revision)
            )
        )
        assert len(revisions) == 2
        assert revisions[0].outcome is None and revisions[1].outcome
        assert all(row.duration_us == 9_000_000_000 for row in revisions)
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
                "canonical_work_retry": "passed",
                "canonical_correction_race": "passed",
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
