"""Flat financial intent compiled through the existing canonical command engine."""

from typing import Any

from pydantic import BaseModel

from docket.domain.errors import DocketError
from docket.models import FinancialEntry, FinancialTransaction, NativeContext, Payment
from docket.schemas.authority import FinancialPostingCreate, mutation_input_json
from docket.schemas.settlement import (
    AdjustFinancialClaim,
    ApplyPayment,
    RecordPayment,
    ReverseFinancialRecord,
    SettlementSpec,
)
from docket.services.business_commands import context_targets, mutation_plan
from docket.services.financial_state import FinancialState
from docket.services.native_commands import NativeCommandService
from docket.services.settlement import SettlementService

SETTLEMENT_COMMAND_MODELS: dict[str, type[BaseModel]] = {
    "docket_record_payment": RecordPayment,
    "docket_apply_payment": ApplyPayment,
    "docket_adjust_financial_claim": AdjustFinancialClaim,
    "docket_reverse_financial_record": ReverseFinancialRecord,
}


class SettlementCommandService(NativeCommandService):
    def perform(
        self, command: str, arguments: dict[str, Any], request_ref: str, request_key: str
    ) -> dict[str, Any]:
        public = SETTLEMENT_COMMAND_MODELS[command].model_validate(arguments)
        values = public.model_dump(mode="json", exclude_unset=True)
        service = SettlementService(self.session)

        def build(context: NativeContext) -> dict[str, Any]:
            kind = (
                "receipt"
                if isinstance(public, RecordPayment)
                else "allocation"
                if isinstance(public, ApplyPayment)
                else public.direction
                if isinstance(public, AdjustFinancialClaim)
                else "reversal"
            )
            raw = {
                key: value
                for key, value in values.items()
                if key
                not in {
                    "context_id",
                    "operation_id",
                    "preview",
                    "replaces_change_id",
                }
            }
            parameters = service.normalize(
                SettlementSpec(
                    command=kind,
                    parameters=raw,
                    basis_hash="0" * 64,
                )
            )
            required = {
                parameters[key]
                for key in ("target_ref", "payment_ref", "ref", "replaces_ref")
                if isinstance(parameters.get(key), str)
            }
            missing = required - context_targets(context)
            if missing:
                raise DocketError(
                    code="targets_not_in_context",
                    message="Read financial context for the selected records first.",
                    details={
                        "refs": sorted(missing),
                        "next_action": "docket_get_financial_context",
                    },
                )
            for ref in required:
                if ref.startswith(("fdoc_", "exp_")):
                    service.target(ref)
                else:
                    model = (
                        Payment
                        if ref.startswith("pay_")
                        else (FinancialTransaction if ref.startswith("ftxn_") else FinancialEntry)
                    )
                    service.owned(model, ref)
            mutation = FinancialPostingCreate(
                change_id="financial-posting",
                action="create",
                object_type="financial_transaction",
                affected_fields=["financial_ledger"],
                create_spec=SettlementSpec(
                    command=kind,
                    parameters=parameters,
                    basis_hash=FinancialState(self.session).fingerprint(parameters),
                ),
            )
            plan = mutation_plan(context, [mutation_input_json(mutation)], command)
            plan["expected_versions"].update(
                {ref: context.versions[ref] for ref in required if ref in context.versions}
            )
            return plan

        return self.execute(
            command=command,
            arguments=values,
            request_ref=request_ref,
            request_key=request_key,
            build=build,
        )
