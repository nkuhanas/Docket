# Compensation, reporting and money

Read an engagement with `docket_get_work_context` for bounded pay/reporting policies.
Use `docket_set_pay_terms` with engagement_ref, effective_from and the changed rate,
basis, amount, currency, payer or eligibility fields. Rate is hourly; amount records
salary/fixed-fee terms. `docket_set_reporting_rules` records explicit reporting periods,
rounding, hour limits and overtime rules. Omitted fields retain each applicable
window's values; null clears nullable terms. Sparse changes preserve future rates.
An expired rate is not extended into an unconfigured gap. These are canonical
configuration records, without claims or payment/provider effects. Reported billability
is unchanged. Use `docket_get_compensation` with an engagement and date range for
effective-rate gross estimates, reportable/eligible quantities and coverage flags.
It keeps currencies separate, evaluates configured rounding/overtime and leaves
unknown or ambiguous earnings unresolved. Salary/fixed fees are configured terms,
not invented hourly earnings. Read the summary before quoting a total; known
subtotals do not establish complete reporting or payment. Follow next_cursor for
bounded details. The export_cursor starts lossless JSON/CSV of the same frozen
calculation, unaffected by later work/rate corrections. Evaluation creates no claim,
submission or payment.
For complete
immutable policy history, follow the returned audit ref and concatenate
policy_json_chunk using text_next_offset until absent.

Use `docket_prepare_timesheet` with an engagement and optional explicit period.
Omitted bounds use its configured current reporting period or the current week.
The canonical draft reserves available work quantities; an optional preview reserves
nothing. The receipt includes the document reference, known time and relevant gaps.
Missing rates do not block a draft or fabricate zero pay. Engagement context links
recent documents; history can find older or abandoned drafts.
Use `docket_refresh_timesheet` after correcting draft work/terms. It replaces that
draft's reservations atomically and retains earlier revisions. Work context derives
reserved/reported/issued/available quantities from stable work lineage, including
after corrections or splits. Do not recreate claim allocation graphs yourself.
`docket_update_timesheet` records verification, cancellation or reported manual
submission/approval/rejection. Verification is optional before recording a valid
manual submission. The command sends nothing to an employer. Record submission or
approval only when supported by an authorized report/evidence, never from export.
Cancelled drafts release reservations; submitted content stays immutable. Use
amends_ref on a new prepared report for later corrections; review its reconciliation
flags and existing claims. A new revision does not make previously claimed hours fresh.
`docket_read_document` reads a document or exact revision as summary, JSON, CSV or
Markdown. Follow cursor until complete; export creates no delivery or payment.

Use `docket_prepare_invoice` with an engagement and optional period, work_refs,
max_work_minutes and expenses. Omit expense_refs for related dated costs, or use
[] to exclude them. Docket derives remaining claim quantities and exact marginal
prices, including overtime, while preserving timesheet references to the same work.
Missing rates can remain in a draft; only affected finalization is blocked.
For a final invoice, set format to json, csv or markdown on prepare. This issues
once and returns documents[0].export_cursor for docket_read_document. For an
existing invoice use docket_update_invoice with its invoice_ref, status=issued
and the requested format. Reuse the exact revision to regenerate another format.
Set status=draft to reopen under the same number. The last issued balance remains
collectible during edits. Reissue the correction to update the balance. Cash already
received is preserved, and excess applied cash becomes available again.
Use `docket_refresh_invoice` after correcting draft work/terms or to change supplied
selection/header fields. Omitted fields retain earlier choices. A stale preview
must be refreshed; applying it never silently claims a different set of hours.
`docket_update_invoice(status="issued")` retains an exact numbered snapshot;
status="cancelled" releases draft reservations. Issued content stays immutable.
status="delivered" records reported manual delivery with optional observed_at/note,
without a provider call or payment. Do not infer it from export. Use the shared
`docket_read_document` for immutable JSON/CSV/Markdown renditions. The document total
rounds once over exact contributions. Select currency or money_decimals when the
draft flags conflicting choices. Work context distinguishes invoice-reserved,
invoiced and uninvoiced quantities. Corrections flag older issued documents for
reconciliation; they do not erase earlier billed units or manufacture payment.

For a standalone cost the operator reports directly, use `docket_record_expense`
with amount, currency, description and manual_entry_requested=true. An attached
bill always uses original-file intake. Stable external_source
plus external_id prevents duplicate imports; similarity alone is a suggestion.
Use `docket_update_expense` for corrections or a reported submission_status and
optional claim_amount/observed_at. Omit an unknown decision time. Earlier approvals
keep their exact cost revision, amount and currency after a correction. A reported
approval is not money received. Use `docket_query_expenses` for full-set currency
totals, explicit undated/unknown amounts and captured JSON/CSV exports. Follow
totals_next_cursor for additional currencies and next_cursor for expense details.
Use export_cursor to restart the same retained basis in another format. Export
chunks concatenate in order; none of these commands sends a reimbursement claim.

Use `docket_get_financial_context` for posted claims, received/applied/unapplied money,
due balances and reconciliation flags. Document balances may overlap; use the
currency totals, which count shared work/expense sources once. `docket_record_payment`
records money actually received and can match target_ref in one command.
`docket_apply_payment` matches an existing payment_ref. Docket derives allocations;
never construct source graphs or infer payment from delivery. Record an explicit
deduction_amount/deduction_kind only when known; net deposits do not prove gross
wages were settled. `docket_adjust_financial_claim` records a credit or charge;
`docket_reverse_financial_record` corrects retained bookkeeping without sending
money. A credit can leave received money unapplied; it does not imply a refund.
Receipt balances describe their retained transaction; context reads current state.
Fully reverse a wrong receipt before recording its replacement with replaces_ref.
Missing optional evidence/date remains a gap. Financial JSON/CSV uses the same
cursor/chunk workflow; export_cursor restarts the captured basis. These commands
create no bank transfer, payroll submission or provider delivery.

Use docket_get_funding_context to manage non-secret funding accounts and cards.
For an uploaded receipt or statement, select the original attachment as the file input to
docket_submit_financial_evidence. Let the client bind the file ID and temporary download
URL. Never construct a link from a file ID, chat link or sandbox path. If the original
cannot be transferred, stop and report the error. Do not request another upload,
transcribe it into an expense or set manual_entry_requested. Report acceptance
only when the receipt returns source_accepted and a source_ref. For “60% business, Savor
card,” pass business_use_percent=60 and reported_instrument="Savor". Docket treats
that 60% as intended for claim. For “60% business, do not claim,” also pass
write_off_intent=false. An omitted or zero percentage supplies no default intent.
Use docket_get_financial_context
with its intake_ref for processing status. When asked to log an expense, follow
pending extraction through to its purchase outcome. Check for an existing cost
before adopting or linking it with docket_update_financial_candidate.

For an accepted PDF that returns evidence_only, use docket_read_attachment_text
with the retained source_ref and follow its page cursor. If the document clearly
supports one purchase, use its vendor, issue date, description, total and explicit
currency. Check existing expenses for the same invoice. Link a match. Otherwise
call docket_record_expense with receipt_ref=source_ref and source_method="import".
For one purchase in the whole file use external_source="financial_receipt_sha256"
and external_id=original_sha256 for retries. Link that expense to the candidate
using intake context with the expense in refs. Read back the linked declarations.
Leave ambiguous costs for review. A mixed or unreadable document needs its own
review. Amount due is separate from a payment observation.
A posted charge, reviewed deduction and mailbox declaration each require their own basis.


For “reconcile my year and show what needs my decision”, read
`docket_get_financial_year` with the tax_year. Its default section is decisions.
Use the returned context and report_ref with `docket_create_financial_year_review`
to refresh the shared attention cases. Reopen the same report_ref to page through
its expenses, allocations, income, totals, recurrence or evidence in JSON or CSV.
Use a fresh report after a correction. Unobserved periods have unknown coverage.

Supply refs to the year context before recording a match, making an expense
treatment decision, designating tax payments or recording income evidence. Use
the current finrev reference as prior_ref for corrections. A financial withdrawal
retires an attribution while preserving its source. Record receipts and apply
payments through the existing settlement commands. Match the posted bank
observation against the applied reimbursement. An expected subscription creates
a review finding when a covered period warrants it.

Upload a paystub, W-2 or 1099 with evidence_role set to income_document. Retain
the protected source and record supported period or annual amounts against that
source and engagement. Intake can suggest explicitly labelled amounts with source anchors. Preserve reported taxable wages separately from gross pay. Use statement for card statements. Keep declared
business use, reviewed deductions, gross wages, net payments and corroborating
annual figures distinct. Report unresolved amounts and coverage.
