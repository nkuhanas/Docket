# Docket — issue an invoice when preparing its export

**Adopted in SPEC 1.5, November 23, 2026.** Baseline [SPEC 1.4](../SPEC.md), source `452613e`. This replaces the November 22 artifact-registration proposal in place.

“Prepare my Ghost invoice as Markdown” should produce a numbered, issued invoice in that format. “Give me the CSV too” should return the same invoice, number and total. The operator can edit it and prepare a corrected version under the same number. A PDF or other external rendition can stay wherever the agent created it. This workflow adds zero tools and requires no return trip with the finished file.

## What changes

| Request | Proposed behavior |
| --- | --- |
| Prepare a final invoice as JSON, Markdown or CSV | Through the existing authorized invoice write path, set the desired state to issued and return the requested export from its exact committed revision. Number and post eligible work and expenses once. An explicit draft or preview remains available. |
| Regenerate it or switch formats | Use the invoice reference or the exact revision from the receipt. An already-issued, unchanged revision produces the same financial basis across formats. Re-reading a prior revision or following a cursor is an ordinary read. |
| Reopen it to make a correction | Set the desired state to draft. Keep the invoice reference, assigned number, original issue timestamp, prior issued snapshots and their reported delivery history. Repeating the request while it is already a draft succeeds without another transition. |
| Issue the edited invoice | Recheck the current work, rates, expenses and expected version. Retain the corrected issued revision under the same number. Adjust the collectible amount by the difference once. Formatting changes alone need no financial revision. |
| Record delivery or payment | Use the existing delivery and settlement commands when those events actually occur. Preparing an export only establishes issuance. |

## Existing interfaces

Today `docket_prepare_invoice` reserves a draft, `docket_update_invoice(status="issued")` assigns the number and finalizes the claims, and `docket_read_document` exports a pinned revision in any of the three formats. Issued invoices currently reject refresh. The shared reader also serves timesheets and historical drafts. [Invoice commands](../src/docket/services/invoices.py) · [Document reader](../src/docket/services/financial_document_reads.py)

Extend the existing invoice write commands to return a final export when a format is requested. Preserve draft-only preparation for existing callers that omit the format. For an existing invoice, use its reference and a desired state (`issued` or `draft`), never a “flip” action. A read-only caller keeps read/export access to authorized snapshots. Issuance requires write authority. Return the invoice reference, number, exact revision and the first bounded export page or its read cursor. Reuse the ordinary document reader for subsequent pages and retries after a lost response.

## Bookkeeping and acceptance

- **One issuance.** With a stable creation operation ID, interruption after commit recovers the same invoice. For the same invoice and financial revision, repeated or concurrent requests to issue yield one number and one set of claims, even with different operation IDs. Changing formats can reuse the revision. A stale edit cannot win a race with another correction.
- **Editable history.** Reopening creates a working draft. The last issued revision continues to support the receivable while edits are in progress. Existing source quantities stay protected against duplicate billing. Reissuance replaces the effective financial basis and preserves exact prior revisions and exports. An unpaid invoice and a partly paid invoice both support correction.
- **Payment example.** On a USD 100 invoice with USD 40 received, re-export leaves USD 60 due. Reopening, correcting the total to USD 90 and issuing again leaves USD 50 due. Each prior export still shows its original amount. If the correction takes the total below payments already applied, account for the excess through existing unapplied-money or credit semantics and preserve the cash receipt. [Settlement](../src/docket/services/settlement.py)
- **Integrity checks.** Exercise all three formats, read-only draft inspection, failed financial validation, concurrent issuance/reopening, interrupted export, immutable historical reads and migration of previously numbered invoices on isolated PostgreSQL. Preserve exact partial-work pricing, expense eligibility, rounding and number uniqueness. The existing invoice and settlement fixtures provide the baseline. [Invoice tests](../tests/integration/test_invoices.py) · [Settlement tests](../tests/integration/test_settlement.py)
