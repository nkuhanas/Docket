"""Effective policy journeys over MCP; work and reported classifications stay unchanged."""

import json
from datetime import UTC, datetime

import pytest
import test_native_plugin
from sqlalchemy import func, select
from test_work_lineage import change
from test_work_reports import work_client

from docket.domain.public_refs import new_public_ref
from docket.models import (
    AuthenticatedRequest,
    CompensationPolicy,
    CompensationPolicyRevision,
    CompensationTerm,
    Engagement,
    Operation,
    ToolInvocation,
    WorkSessionRevision,
)
from docket.schemas.compensation import POLICY_TOOL_PURPOSES
from docket.services.compensation_policies import CompensationPolicyService, instant

linked_plugin = test_native_plugin.linked_plugin


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_effective_pay_terms_and_reporting_rules_preserve_work_and_history(
    session_factory, linked_plugin
):
    async with work_client(linked_plugin) as call:
        created = await change(
            call,
            "docket_create_engagement",
            [],
            "policy-engagement",
            label="Example policy engagement",
            kind="contract",
        )
        with session_factory() as session:
            eng = session.scalar(
                select(Engagement.ref_id).where(
                    Engagement.created_by_changeset_ref == created["changeset_ref"]
                )
            )
        logged = await change(
            call,
            "docket_log_work_session",
            [eng],
            "policy-work",
            description="Example actual work",
            engagement_ref=eng,
            duration_minutes=90,
            work_date="2026-11-04",
            reported_compensation_class="billable",
        )
        original_work = logged["work_records"][0]["ref"]
        preview = await change(
            call,
            "docket_set_pay_terms",
            [eng],
            "first-pay-policy",
            engagement_ref=eng,
            effective_from="2026-10-01",
            timezone="America/Los_Angeles",
            rate="80.123456789",
            currency="USD",
            compensable=True,
            billable=True,
            preview=True,
        )
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(CompensationPolicy)) == 0
        applied = await call("docket_apply_change", {"change_id": preview["change_id"]})
        assert applied.get("disposition") == "committed", applied
        replay = await call("docket_apply_change", {"change_id": preview["change_id"]})
        assert replay.pop("replayed") and replay == applied
        with session_factory() as session:
            first = session.scalar(select(CompensationPolicyRevision))
            first_ref = first.ref_id
            original = CompensationPolicyService(session).terms(first_ref)[0]
            original_term = original.ref_id
            assert original.rate == "80.123456789" and original.end_at is None
        await change(
            call,
            "docket_set_pay_terms",
            [eng],
            "future-rate",
            engagement_ref=eng,
            effective_from="2026-11-15",
            rate="90",
        )
        with session_factory() as session:
            policy = session.scalar(select(CompensationPolicy))
            current = CompensationPolicyService(session)
            before = current.terms(current.revision(policy).ref_id)
            future_ref = before[-1].ref_id
            assert [row.rate for row in before] == ["80.123456789", "90"]
            assert instant(before[0].end_at) == datetime(2026, 11, 15, 8, tzinfo=UTC)
        await change(
            call,
            "docket_set_pay_terms",
            [eng],
            "bounded-rate-correction",
            engagement_ref=eng,
            effective_from="2026-11-10",
            effective_to="2026-11-12",
            rate="85",
        )
        await change(
            call,
            "docket_set_reporting_rules",
            [eng],
            "reporting-rules",
            engagement_ref=eng,
            effective_from="2026-10-01",
            timezone="America/Los_Angeles",
            period_days=14,
            anchor_date="2026-10-05",
            rounding_minutes=15,
            rounding_scope="period",
            hour_limit_week=20,
            overtime_after_hours=40,
            overtime_multiplier="1.5",
            group_with_payer=True,
        )
        await change(
            call,
            "docket_set_reporting_rules",
            [eng],
            "clear-overtime",
            engagement_ref=eng,
            effective_from="2026-11-01",
            overtime_after_hours=None,
        )
        packet = await call("docket_get_work_context", {"refs": [eng]})
        assert len(packet["objects"][0]["state"]["policies"]) == 2
        assert "docket_set_pay_terms" in packet["objects"][0]["actions"]
        skipped = await call(
            "docket_set_pay_terms",
            {
                "context_id": packet["context_id"],
                "operation_id": "invalid-local-policy-date",
                "engagement_ref": eng,
                "effective_from": "2011-12-30",
                "timezone": "Pacific/Apia",
                "rate": 20,
            },
        )
        assert skipped["error"]["code"] == "invalid_compensation_policy"
        assert skipped["error"]["details"]["field"] == "effective_from"
        pay_summary = next(
            p for p in packet["objects"][0]["state"]["policies"] if p["kind"] == "pay"
        )
        policy_context = await call("docket_get_work_context", {"refs": [pay_summary["ref"]]})
        assert policy_context["objects"][0]["state"]["engagement_ref"] == eng
        assert "docket_set_pay_terms" in policy_context["objects"][0]["actions"]
        chunks = []
        offset = 0
        for _ in range(30):
            audit = await call(
                "docket_get_history_entry",
                {
                    "ref": pay_summary["revision_ref"],
                    "view": "audit",
                    "text_offset": offset,
                    "text_limit": 256,
                },
            )
            assert audit.get("ok"), audit
            entry = audit["entry"]
            chunks.append(entry["policy_json_chunk"])
            if entry.get("text_next_offset") is None:
                break
            offset = entry["text_next_offset"]
        else:
            raise AssertionError("Policy history did not finish")
        assert [term["rate"] for term in json.loads("".join(chunks))["terms"]] == [
            "80.123456789",
            "85",
            "80.123456789",
            "90",
        ]
        with session_factory() as session:
            service = CompensationPolicyService(session)
            pay = session.scalar(select(CompensationPolicy).where(CompensationPolicy.kind == "pay"))
            terms = service.terms(service.revision(pay).ref_id)
            assert [t.rate for t in terms] == ["80.123456789", "85", "80.123456789", "90"]
            assert terms[-1].ref_id == future_ref
            assert all(t.currency == "USD" and t.billable for t in terms)
            assert service.terms(first_ref)[0].ref_id == original_term
            assert service.terms(first_ref)[0].end_at is None
            rules = session.scalar(
                select(CompensationPolicy).where(CompensationPolicy.kind == "reporting")
            )
            current = service.terms(service.revision(rules).ref_id)[-1]
            assert current.period_days == 14 and current.anchor_date.isoformat() == "2026-10-05"
            assert current.overtime_after_hours is None and current.overtime_multiplier is None
            work = session.scalar(
                select(WorkSessionRevision).where(WorkSessionRevision.work_ref == original_work)
            )
            assert work.revision == 1 and work.reported_compensation_class == "billable"
            assert work.duration_us == 90 * 60_000_000
            assert session.scalar(select(func.count()).select_from(Operation)) == 0
            roots = set(session.scalars(select(AuthenticatedRequest.ref_id)))
            assert all(
                roots.intersection(r.basis_refs)
                for r in session.scalars(select(CompensationPolicyRevision))
            )
            invocations = [
                dict(row) for row in session.execute(select(ToolInvocation.__table__)).mappings()
            ]
            assert "80.123456789" not in json.dumps(invocations, default=str)
        await change(
            call,
            "docket_set_pay_terms",
            [eng],
            "currency-only-window",
            engagement_ref=eng,
            effective_from="2026-11-01",
            currency="CAD",
        )
        with session_factory() as session:
            service = CompensationPolicyService(session)
            pay = session.scalar(
                select(CompensationPolicy).where(
                    CompensationPolicy.engagement_ref == eng,
                    CompensationPolicy.kind == "pay",
                )
            )
            terms = service.terms(service.revision(pay).ref_id)
            assert [t.rate for t in terms] == [
                "80.123456789",
                "80.123456789",
                "85",
                "80.123456789",
                "90",
            ]
            assert [t.currency for t in terms] == ["USD", "CAD", "CAD", "CAD", "CAD"]
            assert service.terms(first_ref)[0].currency == "USD"
        expires = await change(
            call, "docket_create_engagement", [], "expiry-eng", label="Example finite terms"
        )
        with session_factory() as session:
            finite_eng = session.scalar(
                select(Engagement.ref_id).where(
                    Engagement.created_by_changeset_ref == expires["changeset_ref"]
                )
            )
        await change(
            call,
            "docket_set_pay_terms",
            [finite_eng],
            "finite-rate",
            engagement_ref=finite_eng,
            effective_from="2026-10-01",
            effective_to="2026-11-01",
            rate=20,
            currency="USD",
        )
        await change(
            call,
            "docket_set_pay_terms",
            [finite_eng],
            "patch-across-expiry",
            engagement_ref=finite_eng,
            effective_from="2026-10-15",
            currency="CAD",
        )
        with session_factory() as session:
            service = CompensationPolicyService(session)
            head = session.scalar(
                select(CompensationPolicy).where(CompensationPolicy.engagement_ref == finite_eng)
            )
            terms = service.terms(service.revision(head).ref_id)
            assert [t.rate for t in terms] == ["20", "20", None]
            assert terms[-1].basis == "unknown" and terms[-1].currency == "CAD"


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_pay_policy_exact_preview_rejects_stale_and_keeps_contracts_small(
    session_factory, linked_plugin
):
    _app, settings, _token, _principal = linked_plugin
    from docket.plugin.server import PluginMCP

    tools = {tool.name: tool for tool in await PluginMCP(settings).list_tools()}
    for name in POLICY_TOOL_PURPOSES:
        tool = tools[name]

        def pack(value):
            return len(json.dumps(value, separators=(",", ":")).encode())

        assert pack(tool.input_schema) <= 4096
        assert pack(tool.model_dump(by_alias=True, exclude_none=True)) <= 6144
        assert not {"kind", "term", "policy", "mutation_type", "work"}.intersection(
            tool.input_schema["properties"]
        )
    async with work_client(linked_plugin) as call:
        created = await change(
            call, "docket_create_engagement", [], "stale-policy-eng", label="Example employment"
        )
        with session_factory() as session:
            eng = session.scalar(
                select(Engagement.ref_id).where(
                    Engagement.created_by_changeset_ref == created["changeset_ref"]
                )
            )
        packet = await call("docket_get_work_context", {"refs": [eng]})
        proposals = []
        for rate in (20, 25):
            result = await call(
                "docket_set_pay_terms",
                {
                    "context_id": packet["context_id"],
                    "operation_id": f"same-policy-preview-{rate}",
                    "engagement_ref": eng,
                    "effective_from": "2026-11-01",
                    "rate": rate,
                    "preview": True,
                },
            )
            assert result.get("disposition") == "prepared", result
            proposals.append(result)
        winner = await call("docket_apply_change", {"change_id": proposals[0]["change_id"]})
        assert winner.get("disposition") == "committed", winner
        stale = await call("docket_apply_change", {"change_id": proposals[1]["change_id"]})
        assert stale.get("disposition") != "committed", stale
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(CompensationPolicyRevision)) == 1
            assert session.scalar(select(CompensationTerm.rate)) == "20"


@pytest.mark.asyncio
async def test_policy_commands_do_not_inherit_authority_from_read_scope(
    session_factory, linked_plugin
):
    async with work_client(linked_plugin) as call:
        packet = await call("docket_get_work_context", {})
        rejected = await call(
            "docket_set_pay_terms",
            {
                "context_id": packet["context_id"],
                "operation_id": "unauthorized-rate",
                "engagement_ref": new_public_ref("eng"),
                "effective_from": "2026-11-01",
                "rate": 20,
            },
        )
        assert rejected.get("ok") is False
        assert rejected["error"]["code"] == "agent_authority_denied"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(CompensationPolicy)) == 0
