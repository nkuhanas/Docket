# Docket — portable invoices and external document materialization

**Status** · Proposed for Operator review, November 22, 2026 at 21:38:05 UTC

**Baseline** · [SPEC 1.2](../SPEC.md), source `599c7d7c43d2b223d2cf15f794dcaa3350b6243b`, native contract v69

**Affected requirements on adoption** · SPEC §§2–6 and 8–10, particularly reporting, claims, settlement, native document reads and storage recovery. This proposal awaits review.

## 1. User outcome and ownership

“Prepare last month's invoice, show me the PDF, and let me choose which copy to issue” should use **one Docket invoice and one invoice number**. The agent can try two layouts, switch document tools, or finish the document by hand. It can show the operator which file goes with which business revision. Drafting a PDF, issuing the invoice, reporting delivery and recording payment each answer a different question.

| Responsibility | Owner | What crosses the boundary |
| --- | --- | --- |
| Work and expense attribution, payer and engagement, effective terms, eligibility, claims, prices, corrections, outstanding balance | Docket | A frozen, portable business snapshot with explicit gaps and source references |
| Number assignment, invoice date, financial revision, issuance, recorded delivery and settlement | Docket | Exact identities, receipts and reported observations |
| Choice of document system, authorization to disclose the necessary fields, mapping, candidate comparison, recovery | Operator or authorized agent | Agent reads Docket, supplies selected facts to a materializer, retrieves the result and reports evidence to Docket |
| Templates, private billing profiles, layout, source code, compilation, validation, file storage and build history | Operator or chosen materializer | Finished bytes or a retrievable copy, checksum and validation evidence |

All exchanges use the agent's separate access to each side. Docket has no materializer HTTP client, SDK, webhook, shared database, foreign ID condition, renderer registry, availability check or build queue. A manual workflow uses the same Docket interfaces. A materializer may have its own document IDs and history. Its numbering and issuance functions have no role in issuing a Docket invoice. Access to one service conveys no permission on the other.

## 2. What exists today

| Existing native tools | Observed behavior | Consequence for the delta |
| --- | --- | --- |
| `docket_prepare_invoice`, `docket_refresh_invoice` | An actual draft reserves remaining labor and reimbursable expense quantities. Preview reserves nothing. Refresh checks the basis, releases/replaces reservations atomically and retains the old revision. Partial work, overtime and prior timesheet claims are accounted for. | Keep the same selection, eligibility, lock and revision logic. Rendering consumes zero additional claims. |
| `docket_update_invoice` | Issue rechecks basis and reconciliation flags, captures a server instant in the proposal, writes an issued revision, transitions claims/allocations and assigns the number in one transaction. Cancellation releases a draft. Reported manual delivery appends a separate observation to an issued revision. | Add an optional earlier number assignment and exact candidate selection. The existing issue transaction remains the sole posting point. |
| `docket_read_document` | Summary, JSON, CSV and Markdown accept `fdoc_` or an exact `fdrev_`. Continuations pin revision, format and UTF-8 byte offset. Invoice JSON already has a header, period, evaluation and policy basis, source lines, claim/allocation refs, flags and totals. Number lookup joins the exact issued revision. Its outer summary can include newer reconciliation flags or delivery observations. | Reuse the same read, owner checks and chunk transport. JSON is a suitable source. Markdown remains a human-readable export. Neither existing format promises a versioned, privacy-filtered byte contract or an exact pre-issue number. |
| `docket_get_compensation`, `docket_query_work_sessions`, `docket_query_expenses` | Read calculations, performed work and expenses with distinct coverage. Expense eligibility, an observed approval on a cost revision and payment are distinct facts. | Use these for investigation. An agent building an invoice reads Docket's selected quantities and amounts from its invoice revision. Requerying current hours, rates or expenses could change the basis. |
| `docket_record_payment`, `docket_apply_payment`, `docket_get_financial_context` | Record money received, allocate it against posted obligations, and report shared claims, partial settlement, credits, unapplied money and outstanding balances. | Preserve these entry points. A later payment or credit changes the reconciliation view independently of the invoice as issued. |

The current head is a `FinancialDocument(kind="invoice")` with immutable `FinancialDocumentRevision` records. `InvoiceNumberSequence` is keyed by operator and series. PostgreSQL locks that scope, enforces unique operator/number and operator/series/ordinal, requires an exact issued revision for `InvoiceNumber`, and checks complete priced allocations at commit. An `amends_ref` already links a later document in the same engagement. It does not, by itself, reverse the original obligation. The delivery-observation table admits only reported delivery. Attachment evidence has its own authority and retention rules. Neither table represents an externally produced invoice file. [Invoice commands](../src/docket/services/invoices.py) · [Claim pricing](../src/docket/services/invoice_claims.py) · [Allocation and numbering guards](../migrations/clean_versions/20261105c9d0_document_allocations.py) · [Delivery guards](../migrations/clean_versions/20261105d0e1_invoice_observations.py)

**Verification during drafting.** The connected Docket plugin advertised all ten tools above. Bounded live reads of overview, work context, compensation, work sessions, expenses and financial context succeeded. The financial-document history search returned no document to export in that scope. Exact invoice reads and write behavior therefore come from the source and isolated integration tests. The published contracts live under `plugin/docket/contracts/`. [Native contract](../plugin/docket/contracts/plugin.md) · [Read implementation](../src/docket/services/financial_document_reads.py) · [Invoice tests](../tests/integration/test_invoices.py)

As a replaceability check, a read of Aphelion's live invoice authoring context showed a supplied number and issue date, private issuer/client/payment configurations, a USD-only calculation policy, optional asserted totals and PDF validation. Its tool catalogue exposes build and checksum-bearing download separately from its formal issuance and historical-number reservation. An agent could use the build/download side where the facts are representable. A currency, line arithmetic or required-party-field mismatch must surface to the agent. The agent can choose another tool. No private profile values were copied, and no build, reservation, issuance or cross-service transfer was exercised in this review.

The 15 existing invoice, document-allocation, financial-document, expense and settlement integration tests passed on isolated fixtures during drafting. They exercise the current behavior. The proposed handoff has separate acceptance criteria in §8.

## 3. Portable manifest v1

Add one read representation, `docket_read_document(format="manifest_v1")`, for invoices. A read has no financial side effects. Reading an ordinary draft yields an unnumbered snapshot until an explicit write assigns a number. Reading a numbered revision returns the same bytes across retries, paging, restarts, later source edits and deployment of a future schema version.

| Payload member | Meaning |
| --- | --- |
| `schema`, `invoice_ref`, `source_revision_ref`, `revision` | Schema identifier plus canonical invoice and exact financial source revision. The stable manifest identity is the tuple (schema, source revision, SHA-256 of content). One v1 payload per source revision. |
| `engagement_ref`, `parties`, `bill` | Known canonical organization/payer refs and issuer/client display labels. The client-facing `bill` contains the number, business invoice date, exclusive-end service period and zone, due date/terms, currency, precision, ordered bill lines, signed in-snapshot adjustments and total. Optional values can be unknown. |
| `line_basis`, `basis` | Key each displayed line to its retained allocation(s). Preserve original work or expense revision, claim and policy/evaluation refs as applicable, actual and reportable quantity with explicit units, partial quantities, effective rate components including overtime, and exact value. Pin relevant expense approval ref/amount/currency **only if observed for that revision**. Include genuine provenance roots through public refs. |
| `correction_of`, `coverage` | Pin the earlier invoice and issued revision for a financial correction. Identify unknowns and exclusions with field/line refs and distinguish a blocker for issuing, a blocker for exact rendering and an informational gap. Preserve partial currency buckets explicitly for unresolved drafts. A missing amount remains unknown. |

The bill is the financial assertion set for this version. It contains the financial labels/descriptions appropriate for the recipient. Headings, logos, fonts, template preferences, working notes and document-specific instructions stay outside it. Select fields by allowlist. A party's internal operator ID or an engagement label fallback is insufficient evidence of a legal billing name. Surface missing labels and let the operator correct the source or supply permitted private contact details separately. Keep bank/routing data, tax IDs, addresses, inbox content, raw receipts, general-purpose notes and private file links out of the portable contract by default. Notes and descriptions may themselves disclose private information. The agent sends only recipient-appropriate fields, keeping internal source refs on its side where possible. A private profile may add an address or payment instructions. A different payer, amount, currency, due date or term requires a Docket correction.

**Money and units.** Encode money and rates as base-ten strings, never JSON floats. Encode potentially large integer quantities as strings, and non-terminating exact amounts as reduced signed integer numerator/positive denominator strings. Pin rate units and the original allocation ranges. Docket calculates each posted currency total by rounding once over the exact sum at the retained precision. Individually rounded line estimates may differ by a cent in aggregate. For printable lines, apportion that rounded total over the exact amounts at the declared display precision, using the existing largest-remainder arithmetic with a stable allocation-ref tie break. Preserve the original exact amounts alongside the printable ones. Expose marginal rate components or a priced service amount when one unit rate cannot describe a slice. Never invent an hourly rate to satisfy a renderer. Signed correction amounts must retain their original economic meaning. Keep subsequent credits, payments and current balance in financial context. v1 introduces no editable taxes, charges or ad hoc financial line items. [Pricing and distribution](../src/docket/domain/financial_pricing.py) · [Settlement source valuation](../src/docket/services/financial_sources.py)

**Wire and digest.** Return canonical UTF-8 JSON as the read's existing `content` chunks. Each page identifies the schema, source revision, total bytes, `manifest_sha256` and `bill_sha256`. The first digest covers precisely the concatenated bytes. The second covers the canonical JSON encoding of the `bill` object. Exclude both digest fields, cursors, live status and call times from the payload they frame. Reuse Docket's sorted-key, compact JSON serializer with Unicode preserved, no BOM or trailing newline and no non-finite numbers. Pin v1 ordering, field presence and decimal/UTC-date representations. Arrays use frozen row order, any tie resolved by public ref. Unavailable optional members are omitted under v1 rules and explained in coverage when material. Transport escaping is decoded before hashing. Foreign clients can hash the original bytes directly. Readers reject unknown versions and restart an expired cursor against the same exact revision. [Canonical serializer](../src/docket/domain/canonical.py) · [Document cursor](../src/docket/services/financial_document_reads.py)

**Synthetic numbered draft.** This pretty-printed complete fixture represents one hour at USD 80. Its invoice date is the date printed on the bill. Actual canonical issuance time is a later lifecycle fact.

```json
{
  "schema": "docket.invoice.manifest.v1",
  "invoice_ref": "fdoc_00000000000000000000000001",
  "source_revision_ref": "fdrev_00000000000000000000000002",
  "revision": 2,
  "engagement_ref": "eng_00000000000000000000000003",
  "parties": {"payer_ref": "ent_00000000000000000000000004"},
  "bill": {
    "number": "DEMO-000007", "invoice_date": "2026-11-02",
    "issuer": "Example Studio", "client": "Example Client",
    "service_period": {"from": "2026-10-01", "to_exclusive": "2026-11-01", "timezone": "America/Los_Angeles"},
    "due_on": "2026-11-16", "payment_terms": "Net 14",
    "currency": "USD", "money_decimals": 2,
    "lines": [{"kind": "labor", "description": "Bench validation", "quantity": "1", "unit": "hour", "rate": "80.00", "amount": "80.00"}],
    "adjustments": [], "total": "80.00"
  },
  "line_basis": [{
    "line_index": 0, "allocation_ref": "alloc_00000000000000000000000005", "claim_ref": "clm_00000000000000000000000006",
    "work_ref": "work_00000000000000000000000007", "revision_ref": "wrev_00000000000000000000000008",
    "origin_ref": "worg_00000000000000000000000009", "start_us": "0", "end_us": "3600000000",
    "raw_us": "3600000000", "reportable_us": "3600000000", "pay_term_ref": "cptr_0000000000000000000000000A",
    "exact_amount": {"numerator": "80", "denominator": "1"}
  }],
  "basis": {"evaluation_ref": "ceval_0000000000000000000000000B", "invoice_calculation_version": "invoice-v1", "changeset_ref": "chg_0000000000000000000000000C"},
  "coverage": {"complete": true, "issues": []}
}
```

## 4. Artifact evidence and selection

Add one flat native command, provisionally `docket_record_invoice_artifact`. The agent names an exact financial revision and manifest digest, supplies the media type and SHA-256 of the **final file bytes**, and reports validation. One field, `observed_bill_sha256`, expresses checked financial assertions without sending Docket a second invoice or a PDF. For a match, the attester checks the finished file against **every** bill field, including number, date, parties, quantities, rates, terms and total. It normalizes equivalent displays to the bill's units/order (e.g. an inclusive printed end date) and hashes the reconstructed v1 bill. If a field cannot be checked or a format loses precision, report `unchecked` or `mismatch`. Echoing the input digest is insufficient evidence of that check.

Synthetic write for the preceding manifest. The file checksum below is illustrative.

```json
{
  "context_id": "nctx_0000000000000000000000000D",
  "operation_id": "example-invoice-candidate-1",
  "revision_ref": "fdrev_00000000000000000000000002",
  "manifest_sha256": "4b0cf0cf532dfafee7e46e234a2b839c69e27a88ffe772693291e43fd45ecbb8",
  "media_type": "application/pdf",
  "artifact_sha256": "a114e37cdcca8013db903d46104b376ae9b004d7735c7446004ec4c22ee6caf2",
  "validation": "matched",
  "observed_bill_sha256": "c1ec9de1e5a2962a0a2e24ab7f17c8759f6aec8b7b055975c81254e543e61fcd",
  "generated_at": "2026-11-02T18:30:00Z",
  "locator": "archive:example-invoice-copy-1"
}
```

Docket verifies ownership, revision, manifest hash, result consistency and the reported assertion hash. It records receipt version 1, server receipt time, authenticated request/call and ChangeSet provenance. The agent is the evidence source. A known generation time or opaque, non-secret archive locator is optional. Skip arbitrary metadata bags, signed URLs, credentials, file bytes and foreign build IDs as required keys. Never fetch or execute a locator. A successful receipt identifies the source, exact file digest, public receipt ref and selection eligibility. It is an **attestation**. Docket cannot independently prove that an agent inspected the bytes or that the external archive will retain them.

Allow several receipts for distinct bytes from one revision. An unvalidated file or a reported mismatch may be retained with its status and is ineligible for selection. A failed build that produced no file has no artifact receipt. A later validation is a new attributable observation. Byte-identical retries with the same operation recover the original result. Conflicting evidence remains visible, and an unresolved contradictory validation of the same bytes blocks their selection. Never substitute the last successful upload. Expose candidate refs, validation, checksum, selection and current/stale status in a bounded document read with continuation. Historical candidates remain discoverable after refresh. Locator loss never changes an issued invoice. Whoever retains the original file is responsible for retrieving bytes that match its checksum.

## 5. Number, date and lifecycle

| Action | Result in Docket | Recovery or concurrent action |
| --- | --- | --- |
| Prepare or refresh normally | A draft reserves eligible quantities. Its exact revision is exportable with known fields. | The ordinary manual workflow can still issue directly. Preview and pure export consume no number. |
| Prepare or refresh with `reserve_number=true` | One deliberate write obtains a number using the existing operator/series allocator. Pin a business `invoice_date` from an explicit date or the proposal's captured local date in the invoice timezone. Both facts belong to the new immutable draft revision. | Reuse that number on the same invoice through refresh and issue. A numbered invoice cannot switch series. The date stays pinned across midnight and retries. A deliberate date correction creates a fresh revision. |
| Build and register candidates | The financial head stays draft. Only receipt evidence accumulates. The operator can inspect the exact file and choose one, including after a disconnected session. | Failed builds and passage of time never issue, cancel, release claims or burn another number. A stale upload may be retained against its original revision, with selection disabled. |
| Refresh or cancel | Refresh keeps the identity/number, replaces quantities atomically and yields a new financial revision. Cancel is a terminal draft revision and releases claims. A committed number on a cancelled invoice remains occupied. | Old bytes remain attributable. Refresh with identical visible terms still has a new revision. Never auto-rebind a receipt on visual similarity. No timeout silently cancels a numbered draft. |
| Issue with selected `artifact_ref` | Lock the owned head and recheck versions, claim basis, flags, exact current manifest, receipt eligibility and applicable artifact policy. Atomically post existing allocations/claims, create the issued revision, finalize its number and pin the single selected receipt and its source. | A conflicting selection or changed basis fails explicitly. Same operation ID and arguments return the original issuance. Never issue a second time after a lost response. |
| Issue without a file | Existing direct issuance succeeds under the default optional policy. Numbering happens in the same issue transaction if the invoice was unnumbered. | A draft explicitly set to require a matched candidate can still be worked on and read offline. Only issuance of that invoice waits for a selection. |
| Deliver, settle, correct or reprint | Record actual reported delivery with existing observation. Record/apply money or credits through settlement. Issue a linked new correction document where needed. Later cosmetic copies can be recorded as renditions. | Delivery requires evidence independently of generation or issue. Original issued snapshot, number and selected receipt stay immutable. A credit or replacement affects the balance only through the established economic actions. |

**Two exact revision refs matter.** Issuing a draft creates a new `fdrev_` today. For a numbered materialization workflow, the issued revision records the *source* draft revision, manifest hash, and selected receipt if present. Reading its v1 manifest replays the same pre-issue source bytes. The response envelope identifies both revisions and the actual `issued_at`. No circular “predict the issued revision/hash” requirement. Direct issue of an unnumbered draft creates its final manifest using the actual issued revision and assigned number. Earlier exports continue to resolve to their original bytes. Reprints of a historically issued invoice may acquire new attestations, never a fabricated original selection.

An unnumbered PDF can document a draft. It cannot be selected as the issued original because it lacks the final number. A failed attempt to issue with a selected file must never fall back to artifact-free issuance. A manually retained issued file should live in an archive independent of any expiring download link. On retrieval, compare its bytes to the selected receipt before calling it the original.

**Approval.** Show number, date, parties, total/currency, source revision and exact candidate checksum or a link verified against it before asking for a selection. A request to “make a PDF” authorizes document preparation only. When the operator directs issuance, the approved native agent targets that selection. Existing `preview=true` and exact `docket_apply_change` are available for a two-turn review. The receipt alone conveys no write authority. Scheduled or delegated action still follows SPEC §3 and the scope of the operator's instruction. Do not assert observed human presence or add another OAuth scope, signature ritual or approval flag.

## 6. API and persistence impact

| Entry point | Proposed wire change |
| --- | --- |
| `docket_prepare_invoice`, `docket_refresh_invoice` | Optional flat `reserve_number` and `invoice_date`. Omission preserves existing callers. A committed numbered draft retains its assignment even when the flag is subsequently omitted. Prepare alone may declare `artifact_required=true` for that document, default false. Pin it at creation so a racing issue or refresh cannot silently relax it. |
| `docket_read_document` | One new format literal. Keep existing JSON, CSV and Markdown byte behavior and timesheet reads. Summary advertises manifest availability, source revision, a bounded candidate page and optional issued selection. Reuse the same read for receipt-history continuation with an opaque purpose-bound cursor. |
| `docket_record_invoice_artifact` | One new write with the flat fields in §4 and existing command controls. Version the evidence independently of MIME type. The server derives the invoice, owner, immutable receipt ref and evidence roots. |
| `docket_update_invoice` | One optional `artifact_ref` accepted on `status="issued"`. It identifies an exact candidate and implies its exact revision/hash. Direct issue, cancellation and delivery retain their current meanings. |

**Catalogue impact** · One proposed new tool and four existing tools extended. Keep the manifest and examples in the finance reference, outside MCP input definitions. Measured baseline input/whole-definition sizes in compact UTF-8 bytes were 2,420/3,074 for prepare, 2,213/2,785 for refresh, 910/1,504 for update and 429/969 for read. Measure the eventual wire contracts against the current 4 KiB input / 6 KiB definition, 2 KiB global guide, 8 KiB ordinary context and 16 KiB result budgets. Paginate the complete content and receipts. [Native command registration](../src/docket/plugin/work_tools.py) · [Wire models](../src/docket/schemas/invoices.py)

**Storage and migration after adoption**

1. Reuse `FinancialDocument`, its revisions, `WorkClaim`, `DocumentAllocation`, expense observations, compensation evaluations and settlement. Store pinned v1 bytes (or an immutable byte-equivalent payload with a retained serializer), schema version, manifest and bill hashes keyed by exact source revision. Compute them from allowlisted snapshot fields and retained revision-specific basis. The read cannot consult current entity names, mutable expense approval, live settlement, or today's date to fill a historical gap.
2. Introduce one authoritative number-assignment relation, unique by document, owner/number and owner/series/ordinal, shared by early assignment and direct issue. Keep `InvoiceNumber` as the existing immutable issued binding. In one ordered migration, seed assignments for existing issued rows **only from their recorded number, exact revision and existing provenance**. Do not manufacture earlier reservation events. Verify both directions and the counter high-water marks before enforcing the binding to assignments. Both paths take the same advisory/sequence locks, advance once and use the current number format. Retain the deferred issued-number, precision, currency, quantity and observation checks. Existing allocations, issued rows and sequence values keep their meaning. A rolled-back transaction consumes nothing. A committed abandoned assignment explains a gap.
3. Retain immutable, owner-scoped artifact attestations linked to source revision and digest, with media/checksum, result, reported/observed fields and genuine provenance. Store the source and selected attestation relationship on the issued revision or an immutable one-per-issued-revision association. Use composite ownership/revision/hash constraints, restricted deletion, guarded inserts and append-only history. Enforce the current-head and required-selection check at **commit**, including exact-preview paths. Receipt registration must not increment the financial version or re-reserve work.
4. Existing drafts keep ordinary issue semantics and optional artifacts. Historical issued records can export their recorded financial facts and number. Mark unavailable original evidence/fields as unknown. Renders prepared later cannot retroactively establish which file was sent. Rehearse migration and restore on isolated PostgreSQL with pre-existing issued, cancelled, unnumbered and amended histories. Verify former exports, numbers, allocations, balances and historical request roots. Delay early number reservation until every writer uses the shared allocator. During rollback disable new writes and preserve assigned numbers, selected receipts and new-format reads. An older writer cannot safely resume numbering against a partially upgraded database.

## 7. Agent workflow and recovery

1. Get owned work context, prepare the invoice with the desired period and early numbering, and retain the command ID and receipt. Inspect coverage. Resolve blocking financial facts in Docket or continue working on the draft.
2. Read the exact manifest, concatenate all chunks and verify digest and total length. Persist its identity in the agent's own work record. Choose any materializer able to represent the facts and approved private fields.
3. Provide the client-facing data. Render, independently inspect the final bytes and compare the financial assertions. Keep the checksum and a durable retrievable copy if selected-file retention matters. A second template can generate a second candidate. External operation IDs and build recovery belong on that side.
4. Register each candidate's evidence. Read back the receipts. Show the exact selection for approval. If the source changed, fetch the new revision and build against it. A different renderer can resume from the same manifest.
5. Issue in Docket under the exact selected basis. On a timeout, replay the same operation with identical arguments or inspect its history before taking further action. Only separately authorized, evidenced delivery or money received advances those records.

| Interruption | Observable result and retry |
| --- | --- |
| Agent stops after reservation or halfway through export | Draft, claims, number and exact revision persist. Resume at the cursor or reread from byte zero. Explicit cancellation is available. |
| External build times out, or its download link expires | Recover the existing attempt/checksum through the materializer or provide identical archived bytes. If recovery fails, render another candidate. No automatic Docket effect. |
| Agent loses registration or issuance response | Retry unchanged against its original operation identity. Changed intent under the same ID conflicts. A fresh issue against an already issued head reports final state. |
| Two agents race on the same draft, different series, selections or a work correction | Lock/recheck at commit. A loser sees the exact stale basis or occupied assignment. A claim or expense change requiring reconciliation blocks only the affected invoice. |
| Materializer validates the layout but changes a cent, date, payer or number | Retain a mismatch/unchecked receipt or discard the candidate. Resolve the mapping, select a compatible materializer, or explicitly correct Docket's financial source. |

There is no cross-service transaction. A durably created external file can be orphaned if an agent stops. It has no claim, issuance, delivery or payment effect in Docket.

## 8. Acceptance and delivery plan

| Gate | Required proof after adoption |
| --- | --- |
| M1 · Portable read | Exact v1 JSON bytes and hashes from an existing issued fixture and a draft. Independently recompute the example digests. Exercise Unicode, multiple pages, old cursor after refresh, new serializer release, source correction and later settlement. Frozen bytes survive all of them. Unknown historical fields and expenses without approval stay explicit. Export contains only approved fields and zero provider intents. |
| M1 · Financial fidelity | Include overtime, partial work already reported on a timesheet, expense-only and mixed invoices, precision other than two, equal fractional-cent remainders, incomplete rate, mixed currencies and a linked adjustment. Displayed line amounts sum to the posted total at the invoice precision. Currency/precision conflicts still block issue. Query/export/registration cannot claim work or costs twice. |
| M2 · Numbering | In isolated PostgreSQL race early assignment against direct issuance in the same series and simultaneous requests against one document. Cover new series, retries, cancellation, failed transaction, preview, midnight crossing and series-change attempts. Prove historical numbers/claims unchanged and an abandoned committed ordinal never reused. |
| M3 · Candidates and authority | Two layouts, same manifest, distinct checksums, both discoverable. Fail wrong-owner refs, wrong document, changed hash, unverified output, stale receipt selection and read-only/triage writes. Fail same operation ID with changed args. Reject an untrusted locator as authority. No PDF fetch or materializer call from Docket. |
| M3 · Issuance and corrections | Two agents try different candidates, one with a stale preview. Exactly one issued revision, number, selected file and economic posting. Later reprints, delivery, partial payment, credit, correction and superseding invoice preserve the original selection, number, bytes identity, claim lineage and settlement evidence. An issued invoice never silently reverts to draft. |
| M4 · Replaceability and rollout | A synthetic external build and a manual or second independent tool consume identical v1 bytes, return distinguishable evidence and agree on the financial assertions. A materializer outage leaves prepare/read/refresh/direct issue/payment usable. A required-artifact invoice can finish with any qualifying file, including a manually produced one. Rehearse lost responses and restore. Measure genuine MCP schemas/packets and verify agent discovery with synthetic data. |

Ship M1 as an additive export first. M2 adds the common allocator and opt-in numbered drafts after migration proof. M3 adds candidate registration and optional exact selection, activating required-artifact policy only once issue-time enforcement is available. M4 validates the complete user journey before calling the boundary accepted. At adoption reconcile the single SPEC, ADR/history and forward index. At implementation align generated contracts and plugin guidance and use normal local release checks. Ordinary finance remains independently usable at each stage. [Allocation tests](../tests/integration/test_document_allocations.py) · [Settlement tests](../tests/integration/test_settlement.py) · [PostgreSQL race checks](../scripts/work-postgres-check.py)

## 9. Decisions for Operator review

| Decision | Recommendation and trade-off |
| --- | --- |
| When to spend a number | Opt in when asking for a final-number rendering. Keep it across refresh and retire it on cancellation. Existing issue-first/manual workflow still works. This accepts explainable gaps. If old external invoices used the same series, establish a distinct Docket series or reconcile historical occupancy before using it for those clients. No implicit historical import. |
| Which date appears on a pre-issue PDF | Freeze the invoice's business date at reservation, allow an explicit revision for a changed date and retain the actual issue instant independently. Requiring them to match would require a fresh rendering after a date rollover. |
| When a file is required | Default optional. Permit a per-invoice election at preparation to require one exact matched receipt. Selection is part of the authorized issue command. A stricter engagement-wide default or mandatory human-presence ceremony would be a separate policy decision. |
| Where private details and original bytes live | Keep remittance/contact profiles and file custody with the operator/materializer. Docket keeps a digest, selection and optional non-secret locator. This supports attribution and later byte comparison. Guaranteed archival retrieval or independent PDF inspection would require a different custody model. |

Receipt OCR, tax accounting, payroll/banking integrations, money transmission, external invoice sending, named-provider adapters, résumé/recruiting, and calendar/workload behavior are outside this delta.
