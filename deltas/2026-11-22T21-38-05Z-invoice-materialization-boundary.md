# Docket — invoice numbering and external artifact evidence

**Proposed for Operator review** · November 22, 2026 · [SPEC 1.2](../SPEC.md), native contract v69, source `599c7d7c43d2`. Affects SPEC §§3–6 on adoption.

“Prepare the invoice, show me two layouts, and issue the one I choose” needs one Docket invoice. The agent can already read its financial revision and construct a document in another tool. The first draft proposed a portable manifest, shared serialization and three hashes to coordinate that work. This revision drops those requirements. The only proposed checksum identifies the finished file.

| Owner | Responsibility |
| --- | --- |
| Docket | Engagements and payers, work and expenses, prices and claim quantities, invoice identity, numbers and revisions, issuance, reported delivery, payments and reconciliation. |
| Agent | Read the exact revision, translate its facts into the chosen tool's native inputs, supply authorized private details, coordinate builds, report what was checked and recover interrupted work. |
| Operator or materializer | Author, revise, validate, retain and hand back the document. A separate instruction authorizes sending it to the client. |

The agent is the sole intermediary. Each side uses its own permissions and recovery mechanisms. No shared schema, service registry, adapter, webhook, database or cross-service transaction is required.

## 1. Can an agent build the invoice from today's read?

**Yes, for an invoice whose number and billing facts are known.** Use `docket_read_document(ref=<fdrev_…>, format="json")` and follow its cursor to completion. The JSON is pinned to the exact revision. The outer summary can separately report current reconciliation flags or later delivery observations. In the existing integration fixture the second hour is an overtime allocation worth USD 120.00, a reimbursable expense adds USD 10.00, and the document total is USD 130.00. All three are available from the invoice. [Document reader](../src/docket/services/financial_document_reads.py) · [Invoice fixture](../tests/integration/test_invoices.py)

| Financial question | Available in the revision | Smallest change needed |
| --- | --- | --- |
| Who and when? | Invoice, revision, engagement, organization/payer refs and labels, service dates (exclusive end), timezone, terms and due date. Number and `issued_at` exist at issuance. | Expose the assigned number and distinct business date on the revision that first records them and its successors. Identify fallback issuer/client labels from retained header inputs. An operator ID or engagement name alone does not establish legal billing identity. |
| What was billed? | Labor and expense lines carry descriptions, source revision and allocation refs. Labor includes raw/reportable microseconds for the slice, its exact amount, a priced estimate and the quote with rate/OT basis. Expense lines give the reimbursable portion, currency and cost revision. Claim refs, calculation version and policy basis are retained. | Document the units and distinguish the quote's whole-contribution quantities from the billed slice. If a partial slice needs a regular/overtime breakdown, expose the components from its retained allocation and price basis in the same JSON. Identify an expense as approved only with evidence for that exact revision. Missing approval stays unknown. |
| What is the total, what changed, and what is unresolved? | Exact fractions, rounding precision, document totals by currency, flags/counts, provenance and `amends_ref`. Earlier revisions are readable by ref. Later credits, charges, partial payments and balances belong to settlement. | Surface already-retained excluded work refs alongside the unknown count. Keep as-issued amounts separate from later adjustments. A correction links back to its source invoice and retains the original obligation until an explicit financial action adjusts it. |

Preserve Docket's exact arithmetic, marginal pricing, document-level rounding and existing allocation/distribution rules. Do not introduce a new printable-line rounding policy. If adding rounded line estimates yields a different total, the agent can show the exact basis or use a format that supports the document total. It cannot change a rate or hide a cent to make a template balance. Work, compensation and expense queries help investigate a discrepancy. They do not substitute today's values for the frozen revision. Financial context remains the source for current balances. [Claim planner](../src/docket/services/invoice_claims.py) · [Settlement valuation](../src/docket/services/financial_sources.py)

The previous review verified live Docket work, compensation, expense and finance reads. Its history search returned no invoice in scope for a live export. Fifteen existing finance integration tests passed on isolated fixtures. Aphelion's then-current authoring interface accepted a supplied number/date, produced a PDF without issuing it, and supported USD. That illustrates a renderer limit. The agent may use another tool for a different currency. Authorized billing addresses and remittance details can come from a private profile. Forward only what the recipient needs.

## 2. Native tool changes

| Tool | Proposed change |
| --- | --- |
| `docket_prepare_invoice`, `docket_refresh_invoice` | Optional `reserve_number=true` and `invoice_date`. Use the existing series. Prepare may also set the invoice's `artifact_policy`, default `optional`. Refresh retains that policy. |
| `docket_read_document` | Keep existing formats and cursor. Add the revision-specific facts above. In the bounded summary expose the applicable policy, selected receipt if any, and a route to paginated evidence for the invoice/revision via existing history. Keep later observations outside frozen financial content. |
| **`docket_record_invoice_artifact`** (new) | Record one observation against an exact owned invoice `revision_ref`. Accept a media type, reported check result, optional file SHA-256, optional non-secret opaque locator, optional generation time and a short evidence note. Derive invoice, reporter, request, ChangeSet, receipt identity and recorded time server-side. Use the existing `context_id` and `operation_id`. |
| `docket_update_invoice(status="issued")` | Optional `artifact_ref` selects one Docket observation. A supplied selection must succeed exactly or issuance fails. Ordinary issuance, cancellation and reported delivery keep their existing commands. |

One new tool. Keep input fields flat and measure the implemented MCP definitions against SPEC's 4 KiB input, 6 KiB definition and 16 KiB normal response limits. The existing `docket_get_compensation`, `docket_query_work_sessions`, `docket_query_expenses`, `docket_get_financial_context`, `docket_record_payment` and `docket_apply_payment` need no new inputs.

## 3. Early numbering and exact selection

**Numbering.** Committing an explicit reservation assigns one number to one invoice under the existing operator/series lock and uniqueness rules. Preview, reads and builds spend no number. Use the supplied business date or capture the proposal's local date in the invoice timezone when first numbering. Refresh retains number, series and date. An explicit date correction creates a new revision. A cancelled draft releases its economic claims and retires its number. An abandoned draft can resume with the same number. Direct issuance of an unnumbered invoice still assigns its number atomically. The actual `issued_at` records the separate issue transition.

**Evidence.** Every receipt is an authenticated agent report. Record the strongest supported check as `unchecked`, `producer_passed`, `contents_compared` or `failed`. A producer pass records the materializer's reported result within its stated scope. For `contents_compared`, identify in the note who actually compared the final file with the exact Docket revision and what was checked. A financial comparison covers number, parties, dates, terms, billed lines and totals. A layout check or repeated source values cannot establish that comparison. Docket checks ownership, field consistency and its own financial state. It neither inspects file contents nor dereferences locators.

**Selection policy.** Any candidate can be recorded with its honest evidence level, including a manual file with unknown checksum. Selecting an *exact original* requires a known final-file checksum, a numbered current draft revision and no unresolved reported failure for those bytes on that revision. Choose the minimum when preparing the invoice:

| Policy | Issue condition |
| --- | --- |
| `optional` (default) | May issue without a candidate. An explicit selection still requires an identifiable file. |
| `identified` | Require a selected, identifiable file. Its content-check status remains visible, including unchecked. |
| `reviewed` | Also require a reported successful financial comparison of those final bytes to that revision. |

At commit Docket checks the policy, current grants, expected version, claim/expense basis and selected observation under the invoice lock. It pins the source draft revision and selected receipt on the **new issued revision** in the same transaction that posts claims and finalizes the number. A refresh makes the earlier candidate stale even if its text looks identical. Later reprints and historical candidates remain readable. They cannot replace the issued original. A draft can collect multiple candidates without changing its version or reserving another minute or expense. Existing chat and delegated write authority apply. Rendering or registering a file grants no authority to issue, deliver or record a payment.

## 4. Storage and recovery

* **Number rows.** Reuse the immutable `InvoiceNumber` row and `InvoiceNumberSequence`. Generalize the row's required revision link from “issued at” to “first assigned at,” allowing an owned numbered draft or the issued revision. Existing rows already identify their real first assignment at issue. Keep number/ordinal uniqueness and append-only guards. Adapt the deferred issuance check to require that same invoice's assignment and exact final revision. Reads of earlier unnumbered drafts must stay unnumbered. Verify historical issued links, numbers, sequence high-water marks and allocation guards on isolated PostgreSQL before enabling early assignment. Old writers must be drained for the transition. [Number allocator](../src/docket/services/document_allocations.py) · [PostgreSQL guards](../migrations/clean_versions/20261105c9d0_document_allocations.py)
* **Document evidence.** Add append-only artifact observations with an owner-scoped invoice/revision link and genuine request/ChangeSet provenance. Store the optional selection immutably on its issued revision and enforce same-invoice/source-revision membership at commit. Keep delivery observations separate. Existing drafts default to optional. Historical issues keep their original numbers and unknown file/date evidence stays unknown. File custody and download credentials stay outside the observation.
* **Agent handoff.** Prepare or resume the draft, optionally number it, then read its exact revision. Map the facts into the chosen authoring tool, retrieve the finished candidate and available checks, and register the evidence. Show the operator the number, total, revision and file choices. Issue the selected receipt under authorized intent. Report client delivery separately when evidenced. Record and allocate actual money through the settlement tools.
* **Interruption.** After number assignment, resume from the Docket receipt. After a build, inspect the agent's records and the materializer's existing history or retained file. A new rendering is another candidate. After registration or issue, recover the Docket receipt or replay the unchanged operation ID and arguments. Changed intent conflicts under the same key. A lost response, failed selection or unavailable renderer causes no automatic issue, fallback, cancellation or payment.

## 5. Acceptance after adoption

| Scenario | Proof |
| --- | --- |
| A · Direct issuance | An ordinary draft issues once without a file, with the same number, claims and settlement behavior on replay. |
| B · External document | An agent produces a final-numbered file from revision JSON and issues against its receipt. Read-back identifies draft revision, issued revision, number, file and actual evidence level. Delivery and payment stay independent. |
| C · Two layouts | Two receipts for one revision. Identical financial totals and reserved quantities. Issue pins exactly one. Reprinting preserves that selection. |
| D · Correction or race | A concurrent refresh, stale preview, wrong-owner or failed receipt cannot win selection. Recheck underlying work/expense changes even if the draft ref is unchanged. Two issue attempts yield at most one posting. |
| E · Replaceable renderer | A manual workflow and a second renderer can register candidates using only their own native interfaces and Docket's ordinary tools. |
| F · Interrupted agent | Stop after reservation, build, registration and issue. Recover each independently. Cancellation releases quantities and leaves the committed ordinal occupied. Race early numbering against ordinary issue across invoices in one series. |
| G · Offline renderer | With every document service unavailable, Docket can number, read, refresh, cancel, record evidence, issue and settle. A required-file invoice can issue with already eligible evidence. Without it, only that issuance waits. |
| H · Incompatible finances | Exercise partial overtime, fractional rounding, non-default precision, expenses, unresolved terms and unsupported currency. A tool that changes the total is rejected for that job. Historic JSON, shared claims and settlement remain faithful to their original basis. |

**Operator review.** Two defaults affect day-to-day use. For an early-numbered draft with no supplied business date, use the local date at number assignment or require the operator to supply it? Recommend the local date, retained across retries and midnight. For an invoice that must have an original file, is identifying the selected bytes sufficient, or must someone also compare their contents? Recommend `identified` for routine work and `reviewed` when a financial comparison matters. Ordinary invoices default to `optional`. If another issuer has occupied numbers in the same series, choose a distinct series before using it for those clients.
