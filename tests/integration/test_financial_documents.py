"""Formal timesheets retain exact work without duplicating claim quantities."""

import csv
import io
import json
from datetime import date

import pytest
import test_native_plugin
from sqlalchemy import func, select
from test_work_lineage import change
from test_work_reports import work_client

from docket.domain.public_refs import new_public_ref
from docket.models import FinancialDocument, FinancialDocumentRevision, WorkClaim, WorkClaimEvent
from docket.models.base import utc_now
from docket.plugin.server import PluginMCP
from docket.schemas.financial_documents import FINANCIAL_TOOL_PURPOSES

linked_plugin = test_native_plugin.linked_plugin
MINUTE = 60_000_000
DESCRIPTION = 'Example reported work — GPU 🧪 | "qualification"'


async def export_document(call, ref, format):
    parts = []
    arguments = {"ref": ref, "format": format, "max_bytes": 256}
    for _ in range(200):
        page = await call("docket_read_document", arguments)
        assert page.get("ok"), page
        parts.append(page["content"])
        if page["complete"]:
            return "".join(parts)
        arguments = {"cursor": page["next_cursor"], "max_bytes": 256}
    raise AssertionError("Document export did not terminate")


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_timesheet_preview_reservations_corrections_submission_and_amendment(
    session_factory, linked_plugin
):
    async with work_client(linked_plugin) as call:
        created = await change(
            call,
            "docket_create_engagement",
            [],
            "timesheet-engagement",
            label="Example reported employment",
            kind="employment",
        )
        eng = next(ref for ref in created["affected_refs"] if ref.startswith("eng_"))
        logged = await change(
            call,
            "docket_log_work_session",
            [eng],
            "timesheet-work",
            description=DESCRIPTION,
            duration_minutes=90,
            work_date="2026-11-03",
            timezone="UTC",
            engagement_ref=eng,
        )
        work = logged["work_records"][0]["ref"]
        preview = await change(
            call,
            "docket_prepare_timesheet",
            [eng],
            "timesheet-prepare",
            engagement_ref=eng,
            date_from="2026-11-02",
            date_to="2026-11-09",
            timezone="UTC",
            preview=True,
        )
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(FinancialDocument)) == 0
            assert session.scalar(select(func.count()).select_from(WorkClaim)) == 0
        applied = await call("docket_apply_change", {"change_id": preview["change_id"]})
        assert applied.get("disposition") == "committed", applied
        assert applied["documents"][0]["worked_us"] == 90 * MINUTE
        assert applied["documents"][0]["status"] == "draft"
        doc = next(ref for ref in applied["affected_refs"] if ref.startswith("fdoc_"))
        packet = await call("docket_get_work_context", {"refs": [doc]})
        assert packet.get("ok"), packet
        assert packet["objects"][0]["state"]["status"] == "draft"
        draft_json = json.loads(await export_document(call, doc, "json"))
        assert draft_json["lines"][0]["description"] == DESCRIPTION
        assert len(draft_json["claim_refs"]) == 1
        draft_csv = list(csv.DictReader(io.StringIO(await export_document(call, doc, "csv"))))
        assert draft_csv[0]["record_type"] == "summary"
        assert draft_csv[1]["description"] == DESCRIPTION
        assert "Example reported employment" in await export_document(call, doc, "markdown")
        with session_factory() as session:
            claim = session.scalar(select(WorkClaim))
            assert claim.end_us - claim.start_us == 90 * MINUTE and claim.status == "reserved"
            assert claim.gross_numerator is None
        await change(
            call,
            "docket_update_work_session",
            [work],
            "timesheet-correct-work",
            work_ref=work,
            duration_minutes=120,
        )
        await change(
            call, "docket_refresh_timesheet", [doc], "timesheet-refresh", timesheet_ref=doc
        )
        with session_factory() as session:
            active = session.scalars(select(WorkClaim).where(WorkClaim.status != "released")).all()
            assert sum(claim.end_us - claim.start_us for claim in active) == 120 * MINUTE
            assert session.scalar(select(func.count()).select_from(WorkClaimEvent)) == 3
            released_ref = session.scalar(
                select(WorkClaim.ref_id).where(WorkClaim.status == "released")
            )
        audit = await call("docket_get_history_entry", {"ref": released_ref, "view": "audit"})
        assert [
            row["status"] for row in json.loads(audit["entry"]["claim_json_chunk"])["history"]
        ] == [
            "reserved",
            "released",
        ]
        await change(
            call,
            "docket_update_timesheet",
            [doc],
            "timesheet-submit",
            timesheet_ref=doc,
            status="submitted",
            note="Reported manual submission",
        )
        with session_factory() as session:
            revision = session.scalar(
                select(FinancialDocumentRevision).where(
                    FinancialDocumentRevision.document_ref == doc,
                    FinancialDocumentRevision.status == "submitted",
                )
            )
            submitted_ref, original_snapshot = revision.ref_id, revision.snapshot_json
            assert all(
                c.status in {"reported", "released"} for c in session.scalars(select(WorkClaim))
            )
        submitted_body = await export_document(call, submitted_ref, "json")
        await change(
            call,
            "docket_update_work_session",
            [work],
            "timesheet-later-work",
            work_ref=work,
            duration_minutes=150,
        )
        packet = await call("docket_get_work_context", {"refs": [doc]})
        assert "work_revision_changed" in packet["objects"][0]["state"]["reconciliation_flags"]
        assert await export_document(call, submitted_ref, "json") == submitted_body
        amended = await change(
            call,
            "docket_prepare_timesheet",
            [eng, doc],
            "timesheet-amend",
            engagement_ref=eng,
            date_from="2026-11-02",
            date_to="2026-11-09",
            timezone="UTC",
            amends_ref=doc,
        )
        amendment = next(ref for ref in amended["affected_refs"] if ref.startswith("fdoc_"))
        with session_factory() as session:
            new_claims = session.scalars(
                select(WorkClaim).where(WorkClaim.document_ref == amendment)
            ).all()
            assert sum(c.end_us - c.start_us for c in new_claims) == 30 * MINUTE
            assert (
                session.scalar(
                    select(FinancialDocumentRevision.snapshot_json).where(
                        FinancialDocumentRevision.ref_id == submitted_ref
                    )
                )
                == original_snapshot
            )
        await change(
            call,
            "docket_update_timesheet",
            [amendment],
            "timesheet-cancel-amendment",
            timesheet_ref=amendment,
            status="cancelled",
        )
        with session_factory() as session:
            assert all(
                c.status == "released"
                for c in session.scalars(
                    select(WorkClaim).where(WorkClaim.document_ref == amendment)
                )
            )
        split = await change(
            call,
            "docket_split_work_session",
            [work],
            "timesheet-split-claimed-work",
            work_ref=work,
            parts=[{"duration_minutes": 90}, {"duration_minutes": 60}],
        )
        children = [row["ref"] for row in split["work_records"] if not row["superseded"]]
        packet = await call("docket_get_work_context", {"refs": children})
        quantities = [
            obj["state"]["claim_quantities"]["components"][0]
            for obj in packet["objects"]
            if obj["ref"] in children
        ]
        assert sum(row["reported_us"] for row in quantities) == 120 * MINUTE
        assert sum(row["available_us"] for row in quantities) == 30 * MINUTE


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_stale_preparation_rejects_and_reported_approval_preserves_submitted_basis(
    session_factory, linked_plugin
):
    async with work_client(linked_plugin) as call:
        created = await change(
            call, "docket_create_engagement", [], "stale-sheet-eng", label="Example stale report"
        )
        eng = next(ref for ref in created["affected_refs"] if ref.startswith("eng_"))
        logged = await change(
            call,
            "docket_log_work_session",
            [eng],
            "stale-sheet-work",
            engagement_ref=eng,
            description="Example reported work",
            duration_minutes=60,
            work_date="2026-11-03",
            timezone="UTC",
        )
        work = logged["work_records"][0]["ref"]
        preview = await change(
            call,
            "docket_prepare_timesheet",
            [eng],
            "stale-sheet-preview",
            engagement_ref=eng,
            date_from="2026-11-02",
            date_to="2026-11-09",
            timezone="UTC",
            preview=True,
        )
        await change(
            call,
            "docket_update_work_session",
            [work],
            "stale-sheet-correct",
            work_ref=work,
            duration_minutes=90,
        )
        stale = await call("docket_apply_change", {"change_id": preview["change_id"]})
        assert stale.get("disposition") != "committed", stale
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(FinancialDocument)) == 0
            assert session.scalar(select(func.count()).select_from(WorkClaim)) == 0
        prepared = await change(
            call,
            "docket_prepare_timesheet",
            [eng],
            "current-sheet-prepare",
            engagement_ref=eng,
            date_from="2026-11-02",
            date_to="2026-11-09",
            timezone="UTC",
        )
        doc = next(ref for ref in prepared["affected_refs"] if ref.startswith("fdoc_"))
        await change(
            call,
            "docket_update_timesheet",
            [doc],
            "current-sheet-verify",
            timesheet_ref=doc,
            status="verified",
        )
        await change(
            call,
            "docket_update_timesheet",
            [doc],
            "current-sheet-submit",
            timesheet_ref=doc,
            status="submitted",
        )
        before = await call("docket_read_document", {"ref": doc})
        submitted = before["document"]["revision_ref"]
        await change(
            call,
            "docket_update_work_session",
            [work],
            "approved-sheet-correct-work",
            work_ref=work,
            duration_minutes=120,
        )
        await change(
            call,
            "docket_update_timesheet",
            [doc],
            "current-sheet-approve",
            timesheet_ref=doc,
            status="approved",
            note="Reported employer approval",
        )
        after = await call("docket_read_document", {"ref": doc})
        assert after["document"]["status"] == "approved"
        assert after["document"]["summary"] == before["document"]["summary"]
        original = await call("docket_read_document", {"ref": submitted})
        assert original["document"]["status"] == "submitted"
        today = utc_now().date()
        starts = (today.weekday() + 1) % 7
        await change(
            call,
            "docket_set_reporting_rules",
            [eng],
            "sheet-configured-week",
            engagement_ref=eng,
            effective_from="2020-01-01",
            timezone="UTC",
            week_starts_on=starts,
        )
        current = await change(
            call,
            "docket_prepare_timesheet",
            [eng],
            "sheet-configured-current-period",
            engagement_ref=eng,
            timezone="UTC",
        )
        current_doc = current["documents"][0]
        first = date.fromisoformat(current_doc["date_from"])
        assert first.weekday() == starts and first <= today < date.fromisoformat(
            current_doc["date_to"]
        )
        observed = await call("docket_read_document", {"ref": doc})
        assert "policy_coverage_changed" in observed["document"]["reconciliation_flags"]
        await change(
            call,
            "docket_update_timesheet",
            [current_doc["ref"]],
            "cancel-current-sheet",
            timesheet_ref=current_doc["ref"],
            status="cancelled",
        )
        await change(
            call,
            "docket_set_pay_terms",
            [eng],
            "sheet-current-rate",
            engagement_ref=eng,
            effective_from="2020-01-01",
            timezone="UTC",
            rate="80",
            currency="USD",
            billable=True,
        )
        priced = await change(
            call,
            "docket_prepare_timesheet",
            [eng],
            "sheet-priced",
            engagement_ref=eng,
            date_from="2026-11-02",
            date_to="2026-11-09",
            timezone="UTC",
        )
        priced_ref = priced["documents"][0]["ref"]
        await change(
            call,
            "docket_set_pay_terms",
            [eng],
            "sheet-later-rate",
            engagement_ref=eng,
            effective_from="2026-12-01",
            rate="90",
        )
        unaffected = await call("docket_read_document", {"ref": priced_ref})
        assert "policy_basis_changed" not in unaffected["document"]["reconciliation_flags"]


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": False}], indirect=True)
async def test_formal_contracts_fit_and_read_only_connection_cannot_prepare_claims(linked_plugin):
    _app, settings, _token, _principal = linked_plugin
    tools = {tool.name: tool for tool in await PluginMCP(settings).list_tools()}
    for name in FINANCIAL_TOOL_PURPOSES:
        definition = tools[name].model_dump(mode="json", by_alias=True, exclude_none=True)
        assert len(json.dumps(definition["inputSchema"], separators=(",", ":")).encode()) <= 4096
        assert len(json.dumps(definition, separators=(",", ":")).encode()) <= 6144
        assert not definition["inputSchema"].get("oneOf")
    async with work_client(linked_plugin) as call:
        context = await call("docket_get_work_context", {})
        refused = await call(
            "docket_prepare_timesheet",
            {
                "context_id": context["context_id"],
                "operation_id": "read-only-formal-report",
                "engagement_ref": new_public_ref("eng"),
            },
        )
        assert refused["error"]["code"] == "agent_authority_denied"
