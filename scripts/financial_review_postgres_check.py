"""Exercise competing allocations and report replay on the disposable release database."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from docket.agent_auth import AgentCallContext, agent_call_context
from docket.domain.errors import DocketError
from docket.models import AccountActivity, FinancialReviewFact, FundingIdentity
from docket.plugin.server import PluginMCP
from docket.services.financial_review import FinancialReviewService


async def check_financial_review(settings, factory, principal):
    token = agent_call_context.set(AgentCallContext(principal))
    try:
        server = PluginMCP(settings)

        async def call(name, args):
            result = (await server.call_tool(name, args)).structured_content
            assert result.get("ok") or result.get("disposition") == "committed", result
            return result

        with factory() as db:
            account = db.scalar(
                select(FundingIdentity.ref_id).where(
                    FundingIdentity.operator_ref == principal.operator_ref,
                    FundingIdentity.kind == "funding_account",
                )
            )
        year = await call("docket_get_financial_year", {"tax_year": 2026, "refs": [account]})
        creation = await call(
            "docket_create_account_activity",
            dict(
                context_id=year["context_id"],
                operation_id="review-pg-posting",
                account_ref=account,
                kind="unknown",
                amount="-100",
                currency="USD",
                posted_on="2026-04-15",
                description="Synthetic tax payment",
            ),
        )
        with factory() as db:
            ref = db.scalar(
                select(AccountActivity.ref_id).where(
                    AccountActivity.changeset_ref == creation["changeset_ref"]
                )
            )
        barrier = Barrier(2)

        def allocate(jurisdiction):
            local = agent_call_context.set(AgentCallContext(principal))
            try:
                with factory.begin() as db:
                    barrier.wait(15)
                    svc = FinancialReviewService(db)
                    return svc.apply(
                        db,
                        SimpleNamespace(ref_id=creation["changeset_ref"]),
                        SimpleNamespace(
                            object_type="tax_allocation",
                            create_spec={
                                "activity_ref": ref,
                                "tax_year": 2026,
                                "jurisdiction": jurisdiction,
                                "tax_type": "income",
                                "kind": "estimated",
                                "amount": "80",
                            },
                        ),
                    )[0]
            except DocketError as exc:
                assert "available posted amount" in exc.message
                return None
            finally:
                agent_call_context.reset(local)

        with ThreadPoolExecutor(max_workers=2) as executor:
            winners = list(executor.map(allocate, ("US", "US-CA")))
        assert sum(bool(r) for r in winners) == 1
        with factory() as db:
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(FinancialReviewFact)
                    .where(FinancialReviewFact.changeset_ref == creation["changeset_ref"])
                )
                == 1
            )
        try:
            with factory.begin() as db:
                db.execute(
                    update(FinancialReviewFact)
                    .where(FinancialReviewFact.ref_id.in_([r for r in winners if r]))
                    .values(fields={})
                )
        except DBAPIError:
            pass
        else:
            raise AssertionError("PostgreSQL allowed a financial review history rewrite")
        packet = await call("docket_get_financial_year", {"tax_year": 2026})
        for i in range(2):
            await call(
                "docket_create_financial_year_review",
                dict(
                    context_id=packet["context_id"],
                    operation_id=f"review-pg-replay-{i}",
                    report_ref=packet["report_ref"],
                ),
            )
        return {
            "financial_review_allocation_race": "passed",
            "financial_year_attention_replay": "passed",
        }
    finally:
        agent_call_context.reset(token)
