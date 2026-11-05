"""Boot the actual application without any Hermes/Discord service or credentials."""

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
import httpx2
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select

from docket.agent_auth import TRIAGE_WORK_TOOLS, AgentCallContext, agent_call_context
from docket.config import get_settings
from docket.database import get_session_factory, session_scope
from docket.domain.enums import Environment
from docket.domain.errors import DocketError
from docket.models import (
    AuthenticatedRequest,
    CalendarLane,
    CanonicalEvent,
    Operation,
    PreparedConsumption,
    ProviderAccount,
    Task,
    ToolInvocation,
)
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.schemas.native_commands import CreateTask
from docket.services.oauth import OAuthPrincipalService
from docket.services.operations import OperationRunner
from docket.services.routine_commands import RoutineCommandService
from docket.tool_contracts import contract_tool_names

settings = get_settings()
assert settings.environment is Environment.TEST
assert settings.plugin_enabled and not settings.discord_adapter_enabled
assert not settings.interactive_agent_enabled
assert not settings.external_writes_enabled and not settings.calendar_reads_enabled
assert not settings.gmail_ingestion_enabled

from docket.ingress_credentials import main as provision_ingress  # noqa: E402
from docket.main import app, plugin_app, worker  # noqa: E402


async def check() -> None:
    provision_ingress()  # Native startup does not open an ingress password file.
    assert worker.discord_projection_runner is None
    assert worker.deferred_ingress_runner is None
    assert worker.gateway_lifetime_reconciler is None
    assert worker.notification_dispatcher is not None
    assert worker.triage_inbox_service is not None
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app)) as http,
    ):
        metadata = await http.get(
            "http://docket:8000/plugin/.well-known/oauth-protected-resource",
        )
        assert metadata.status_code == 200
        assert metadata.json()["resource"] == "https://docket.you"
        assert (await http.post("http://docket:8000/plugin/mcp/", json={})).status_code == 401
        for path in (
            "/internal/v1/operator-utterances",
            "/internal/v1/agent/requests",
            "/internal/v1/provenance/history",
        ):
            response = await http.post(
                "http://docket:8000" + path,
                json={},
                headers={"Authorization": "Bearer invalid-retired-credential"},
            )
            assert response.status_code == 404

        assert (await http.get("http://docket:8000/plugin/triage-mcp/")).status_code == 404
        tools = await plugin_app.state.mcp.list_tools()
        assert {tool.name for tool in tools} == contract_tool_names("plugin")
        assert all(
            tool.annotations.read_only_hint
            is (tool.name not in TRIAGE_WORK_TOOLS | plugin_app.state.mcp.authority_tools)
            for tool in tools
        )
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        jwk.update(kid="native-smoke-key", alg="RS256", use="sig")
        verifier = plugin_app.user_middleware[0].kwargs["verifier"]
        verifier.client.close()
        verifier.client = httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"keys": [jwk]}),
            )
        )
        with session_scope() as session:
            principal_ref = (
                OAuthPrincipalService(session, settings)
                .approve(
                    "native-smoke-owner",
                    write=True,
                    events=True,
                    triage=True,
                )
                .ref_id
            )
        now = int(time.time())
        credential = jwt.encode(
            {
                "iss": settings.plugin_oauth_issuer,
                "aud": settings.plugin_public_origin,
                "sub": "native-smoke-owner",
                "azp": settings.plugin_oauth_client_ids[0],
                "iat": now,
                "exp": now + 3600,
                "scope": " ".join(
                    (
                        settings.plugin_read_scope,
                        settings.plugin_write_scope,
                        settings.plugin_triage_scope,
                        settings.plugin_events_scope,
                    )
                ),
            },
            key,
            algorithm="RS256",
            headers={"kid": "native-smoke-key"},
        )
        http.headers["Authorization"] = "Bearer " + credential
        with session_scope() as session:
            account = ProviderAccount(
                provider="google",
                external_account_id="native-smoke",
                capabilities=["google_calendar"],
            )
            session.add(account)
            session.flush()
            session.add(
                CalendarLane(
                    account_id=account.id,
                    lane="meetings",
                    display_name="Synthetic Meetings",
                    color_hex="#123456",
                    calendar_id="synthetic-meetings",
                    status="active",
                    created_by_changeset_ref="synthetic-fixture",
                )
            )

        async with Client(
            streamable_http_client(
                "http://docket:8000/plugin/mcp/",
                http_client=http,
            ),
            mode="legacy",
        ) as client:
            assert {t.name for t in (await client.list_tools()).tools} == (
                contract_tool_names("plugin")
            )
            packet = (await client.call_tool("docket_get_calendar_context", {})).structured_content
            assert packet and packet["calendars"]
            arguments = {
                "context_id": packet["context_id"],
                "operation_id": "native-smoke-event",
                "title": "Synthetic smoke meeting",
                "start": "2026-11-13T13:00:00-08:00",
                "end": "2026-11-13T14:00:00-08:00",
                "timezone": "America/Los_Angeles",
                "calendar_ref": packet["calendars"][0]["calendar_ref"],
                "location": "https://example.test/meeting",
            }
            committed = (
                await client.call_tool(
                    "docket_create_event",
                    arguments,
                )
            ).structured_content
            assert committed and committed["disposition"] == "committed", committed
            retry = (await client.call_tool("docket_create_event", arguments)).structured_content
            assert retry and retry.pop("replayed") is True
            assert retry == committed
            factory = get_session_factory()
            provider = FakeCalendarProvider()
            runner = OperationRunner(factory, provider, consistency_window_seconds=0)
            provider.next_create_outcome = "unknown_after_write"
            assert runner.run_due_once() and not runner.run_due_once()
            assert len(provider.events) == 1 and runner.reconcile_once()
            with session_scope() as session:
                event = session.scalar(
                    select(CanonicalEvent).where(CanonicalEvent.title == "Synthetic smoke meeting")
                )
                assert event is not None
                event_ref = event.ref_id
                root = session.scalar(
                    select(AuthenticatedRequest).where(
                        AuthenticatedRequest.ref_id.in_(event.basis_refs)
                    )
                )
                assert root and root.state == "committed" and root.source_utterance_ref is None

            async def call(name, args):
                result = (await client.call_tool(name, args)).structured_content
                assert result is not None
                return result

            observed = await call("docket_get_context", {"refs": [event_ref]})
            edited = await call(
                "docket_update_event",
                {
                    "context_id": observed["context_id"],
                    "operation_id": "smoke-edit",
                    "event_ref": event_ref,
                    "title": "Updated smoke meeting",
                    "location": None,
                },
            )
            assert edited.get("disposition") == "committed", edited
            assert runner.run_due_once()
            stale = await call(
                "docket_cancel_event",
                {
                    "context_id": observed["context_id"],
                    "operation_id": "smoke-stale",
                    "event_ref": event_ref,
                },
            )
            assert stale.get("ok") is False
            observed = await call("docket_get_context", {"refs": [event_ref]})
            cancelled = await call(
                "docket_cancel_event",
                {
                    "context_id": observed["context_id"],
                    "operation_id": "smoke-cancel",
                    "event_ref": event_ref,
                },
            )
            assert cancelled.get("disposition") == "committed", cancelled
            assert runner.run_due_once() and not provider.events
            question_args = {
                "operation_id": "smoke-question",
                "question": "Which synthetic choice?",
                "choices": [{"option_id": "one", "label": "One", "interpretation": {"choice": 1}}],
            }
            question = await call("docket_request_clarification", question_args)
            assert question.get("disposition") == "clarification_required", question
            question_retry = await call("docket_request_clarification", question_args)
            assert question_retry.get("replayed") is True
            capture = await call(
                "docket_record_context",
                {
                    "operation_id": "smoke-capture",
                    "record_kind": "source_context",
                    "text": "Synthetic reported context",
                },
            )
            assert capture.get("capture_method") == "agent_reported", capture
            overview = await call("docket_get_overview", {})
            pending = await call(
                "docket_create_task",
                {
                    "context_id": overview["context_id"],
                    "operation_id": "smoke-preview",
                    "title": "Original preview",
                    "preview": True,
                },
            )
            read = await call("docket_read_change", {"change_id": pending["change_id"]})
            with session_scope() as session:
                root = session.scalar(
                    select(AuthenticatedRequest).where(
                        AuthenticatedRequest.ref_id == read["request_ref"]
                    )
                )
                assert root is not None
                root_ref, root_key = root.ref_id, root.request_key
            principal = verifier.authenticate(credential, factory)
            barrier = Barrier(2)

            def correct(index):
                context_token = agent_call_context.set(
                    AgentCallContext(
                        principal, root_key, "native-exact-proposal", "correction-" + str(index)
                    )
                )
                try:
                    barrier.wait(timeout=10)
                    with session_scope() as session:
                        return RoutineCommandService(session, settings).task(
                            CreateTask(
                                context_id=overview["context_id"],
                                operation_id=f"smoke-fix-{index}",
                                title=f"Corrected preview {index}",
                                preview=True,
                                replaces_change_id=pending["change_id"],
                            ),
                            root_ref,
                            root_key,
                        )
                except DocketError as exc:
                    return exc.as_dict()
                finally:
                    agent_call_context.reset(context_token)

            def race():
                with ThreadPoolExecutor(max_workers=2) as pool:
                    return list(pool.map(correct, (1, 2)))

            corrections = await asyncio.to_thread(race)
            ready = [value for value in corrections if value.get("disposition") == "prepared"]
            assert len(ready) == 1, corrections
            assert [value["error"]["code"] for value in corrections if "error" in value] == [
                "prepared_change_stale"
            ]
            rejected = await call("docket_apply_change", {"change_id": pending["change_id"]})
            assert rejected["error"]["code"] == "prepared_change_stale", rejected
            applied = await call("docket_apply_change", {"change_id": ready[0]["change_id"]})
            assert applied.get("disposition") == "committed", applied
            with session_scope() as session:
                task_ref = session.scalar(select(Task.ref_id))
            task_context = await call("docket_get_context", {"refs": [task_ref]})
            complete = await call(
                "docket_complete_task",
                {
                    "context_id": task_context["context_id"],
                    "operation_id": "smoke-complete",
                    "task_ref": task_ref,
                },
            )
            assert complete.get("disposition") == "committed", complete
            members = []
            for index in range(2):
                proposal = await call(
                    "docket_create_task",
                    {
                        "context_id": overview["context_id"],
                        "operation_id": f"smoke-batch-{index}",
                        "title": f"Batch member {index}",
                        "preview": True,
                    },
                )
                assert proposal.get("disposition") == "prepared", proposal
                members.append(proposal["change_id"])
            batch = await call(
                "docket_prepare_batch",
                {
                    "operation_id": "smoke-batch",
                    "change_ids": members,
                },
            )
            assert batch.get("disposition") == "prepared", batch
            applied = await call("docket_apply_change", {"change_id": batch["change_id"]})
            assert applied.get("disposition") == "committed", applied
            for member in members:
                retry = await call("docket_apply_change", {"change_id": member})
                assert retry.get("replayed") is True
                assert retry["changeset_ref"] == applied["changeset_ref"]
        with session_scope() as session:
            assert len(list(session.scalars(select(Task)))) == 3
            assert len(list(session.scalars(select(PreparedConsumption)))) == 2
            assert len(list(session.scalars(select(Operation)))) == 3
            calls = list(
                session.scalars(
                    select(ToolInvocation).where(
                        ToolInvocation.actor_ref == principal_ref,
                    )
                )
            )
            assert all(row.actor_ref == principal_ref and not row.utterance_refs for row in calls)
            OAuthPrincipalService(session, settings).revoke(principal_ref)
        assert (await http.post("http://docket:8000/plugin/mcp/", json={})).status_code == 401
        await asyncio.sleep(0)
        assert worker.is_healthy()
    assert worker._task is None and worker._notification_task is None
    print(
        json.dumps(
            {
                "native_only_startup": "passed",
                "tools": len(tools),
                "inference_provider": "caller_owned",
                "delegated_write": "passed",
                "exact_preview_race": "passed",
                "atomic_batch": "passed",
                "provider_unknown_recovery": "passed",
            }
        )
    )


asyncio.run(check())
