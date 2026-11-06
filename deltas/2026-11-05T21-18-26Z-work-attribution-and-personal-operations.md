# Docket: work performed, reporting and money reconciliation

Draft timestamp: 2026-11-05 21:18:26 UTC
Status: implemented and deployed; plugin 2.1.0 published; fresh-chat work-tool acceptance pending
SPEC baseline: [1.0](../SPEC.md)
Inspected source: `88ad4ff39526210a8a62464518de1c676e7b4c6e`
Adopted SPEC target: [1.1](../SPEC.md); [implementation evidence](../docs/work-attribution-implementation.md)

## 1. Decision proposed

Make reported work performed a first-class Docket capability. The first useful
release must reliably record, correct, query and export work across several
engagements, including running timers and incomplete reports. P0a starts with
retrospective logging, corrections and informal reports; P0b adds durable timers.
It must work without rates, receipts, payroll configuration or a second spreadsheet.

Build compensation, timesheets, contracting and capacity reasoning on that same
work history in subsequent phases. Preserve those goals in this delta without
making a complete payroll/accounting system a prerequisite for logging time.
Docket is already a personal operations service; this extends its canonical
domain rather than replaces a calendar-only product.

This is a change proposal, not another master specification. Preparing or merging
this draft does not adopt SPEC 1.1, implement the feature, create employment facts,
or authorize a financial/provider action. On an instruction to implement, reconcile
the adopted scope into SPEC, its decisions/history and the forward delta index.
Use the current development-conversation and release authority; add no Discord,
ledger-signature or repeated implementation-approval procedure.

The suggested organizations, engagements and rates in the source proposal are
unverified personal context. This draft uses anonymous examples and seeds none of
them. A program, department, customer and legal payer may be different entities.
In particular, a project name does not establish who pays for its work.

The Operator-supplied review is incorporated: P0 preserves reported compensation
classification and optional event/location context, P0a includes informal CSV/JSON
reports, and finance is split into P1a–P1c. The original reviews were proposals;
the subsequent Operator implementation instruction adopts this scope into SPEC 1.1.

## 2. What the current implementation actually provides

| Existing surface | Reuse | Boundary that needs new behavior |
| --- | --- | --- |
| `Entity` | Organizations, institutions, people and **existing project entities** | No second project registry; validate entity kinds when linking references. |
| `Affiliation`, `Relationship` | Roles, organizational relationships and dates | Affiliation is not an employment contract, payer, rate schedule or reporting obligation. |
| `Item`, `Task` | Workstreams, deliverables, descriptions and context | Task completion records completion, not elapsed effort or earned compensation. |
| `TemporalBinding` | Existing planned/due/effective time semantics | Its closed role set has no actual-work role. Only `window` currently accepts intervals; subjects are Item/Task refs. |
| `Fact` | Reported terms and supporting assertions | Arbitrary `value_json` is not a validated compensation or calculation engine. |
| `Preference` | Operator behavior, suppression and calendar-routing policy | Its current kinds do not implement payroll, monetary claims or work-reporting rules. |
| `Event`, Calendar lanes and temporal projections | Commitments and optional visibility | An event or calendar observation is not proof of attendance or work performed. |
| `ReminderPlan`, inbox and events | Bounded follow-up and notification delivery | Reminder subjects currently accept only `evt_` or `time_`; work and financial deadlines need an explicit extension. |
| Native contexts, named commands, exact prepared handles, ChangeSets | Authentication, versions, corrections, idempotency and atomic effects | New domain mutations/read projections must be wired through these services; the existing 47-effect union does not already contain work or finance. |

The measured 84-tool catalogue is an implementation inventory, not a target count
or proof of these new semantics. No current work-session, engagement, timesheet,
invoice, expense or payment service was found in the inspected source.

An Item with metadata and a `window` could hold an informal note about work. It
cannot enforce timer ownership, actual-time revisions, claim allocation or issued
document integrity. Do not establish that encoding as the new canonical contract
and then require clients to assemble it manually.

## 3. Domain model and ordinary behavior

### WorkSession

Introduce a typed actual-work aggregate with a stable public reference, an
optimistically versioned head and immutable retained revisions. Preserve:

- The operator to whom the record belongs, supplied by authentication.
- Reported work description, optional outcome and category.
- Time information and its precision: bounded intervals, duration-only, or
  explicitly incomplete/unquantified work. Unknown duration is not zero.
- Optional organization, engagement, project, Item, Task and existing Event
  references, with the event link exposed as `event_ref`.
- Optional `reported_compensation_class`: `billable`, `paid_work`, `unpaid` or
  `unknown`, retained as an assertion rather than evaluated eligibility.
- Optional `work_mode` (`remote`, `onsite` or `travel`), `reported_location` text
  and `place_ref` to an existing place entity. Omission leaves location/mode unknown.
- Genuine request/evidence roots, source method and any uncertainty.
- Active segments/breaks, lifecycle, correction and split/merge lineage.

Use normalized typed persistence where constraints and queries require it.
WorkSession revision/segment rows must not be an unvalidated metadata document.
Public `work_` and engagement `eng_` references are proposed names, currently
unused in the inspected prefix registry; finalize and register them on adoption.
Clients never supply internal UUIDs, revision numbers or support-record IDs.

Do not require every session to create an Item, Task, Event or TemporalBinding.
Link existing objects when useful. Actual time lives in the work aggregate; it
must not become a second independently editable copy in a generic time window.
There is no automatic migration of old calendar events or Item metadata into
performed work. Such material can support later explicit adoption of a report.

Useful behavior includes:

- “I worked from 10 to 12:30 for Client A” records 2.5 hours, subject to any
  explicitly reported breaks, without requiring a compensation policy.
- “Three hours on Project A yesterday” records a dated duration. It invents
  neither clock bounds nor an employer association.
- “Three billable hours for Client A” preserves the reported billable classification
  even when no rate or compensation policy has been configured.
- “Actually, that session ended at 6:15” corrects the selected current revision,
  retains its earlier reading and recalculates affected current summaries.
- An unknown payer or project can remain a reported label/unresolved attribution.
  It is visible in queries and can be resolved later without re-entering the work.

Recording a report, verifying evidence and qualifying compensation are distinct.
Source methods distinguish relayed operator reports, timer observations, imports
and inference. Any inference confidence is explicitly client-reported, not a
calibrated assurance, financial verification or grant of authority.
Avoid one status enum mixing `corrected`, `paid`, `verified` and `running`.
Use a small lifecycle such as running/paused/recorded/retracted; represent missing
attribution, precision, review and financial disposition as separate facets.
“Corrected” describes revision history, not a new kind of work.

Reported compensation classification is available in P0a logging, corrections,
queries and exports. `paid_work` means work reported as intended for compensation;
it does not mean wages were received. `billable` does not establish an eligible
claim. Omitted/unknown classification needs no follow-up before recording work.
Later policy evaluation records its own eligibility/result without rewriting the
reported assertion. An explicit classification correction creates a new revision;
the original assertion and its provenance remain readable.

### Engagement and attribution

Introduce a typed engagement for a particular employment, contract, research,
volunteer or personal arrangement. It can reference an existing Affiliation but
does not require an affiliation/person-bootstrap ceremony before recording work.

Its initial useful fields are a label, relationship kind, organization/payer refs
where known, effective dates at their reported precision, and role/reporting notes.
P1a attaches typed compensation and reporting policy revisions. Unknown terms
remain unknown. Personal and unpaid work need no payer or rate configuration.

Organization, sponsoring team and legal employer/payer must remain distinguishable.
Two programs can share a payer; one project can appear under different engagements
or personal work. A future internship is a future engagement, not current paid
activity. Missing or conflicting effective dates flag affected attribution/claims
without erasing a report that work occurred.

Use existing `ent_` project identities and Item/Task links. To attribute time to
multiple engagements, split it or allocate explicit quantities; never clone its
full duration into each employer. New writes must validate referenced kinds and
ownership rather than trust a label or a context handle as authority.

## 4. Time, timers and corrections

### Deterministic time semantics

Store actual instants in UTC, retain supplied offsets and useful timezone context,
and calculate elapsed duration at stored precision. Reporting timezone and physical
location are separate. Offset-bearing start/end can cross midnight, zones or a DST
fold; compare instants, not wall-clock text. Work timezone describes reporting/local
interpretation and does not silently infer a travel location.

Accept ordinary ISO inputs. Resolve offset-free local input only with an applicable
IANA timezone and an unambiguous real local time; otherwise identify the affected
field. Do not copy the current Calendar interval model's single shared fold into
actual-work storage: it cannot express all cross-zone/fold intervals. Existing
Calendar semantics stay intact.

Treat intervals as half-open `[start, end)`. Preserve outer bounds and break/active
segments; net work excludes the union of recorded breaks without subtracting them
twice. Segment order, positive elapsed duration and break containment are validated.
Keep raw elapsed work separate from employer-rounded/reportable quantities.

Duration-only work carries a reported day/period and timezone when known. A missing
day remains unplaced, not “today.” Date-only attribution must not create midnight
start/end timestamps. Cross-period allocation remains unknown until supported by
the report or an explicit allocation; do not distribute hours evenly as fact.

### Timer transitions

Start, pause, resume and stop are canonical commands. Capture server “now” once
under the durable command identity and retain it for unchanged retries. Record
request/effective time clearly; a retry or delayed apply must not move the boundary.
If a preview of a timer transition is supported, show its exact bound time and
apply that proposal, never re-evaluate “now.” A preview alone starts no timer.

P0 supports one active primary timer per operator, enforced transactionally on
PostgreSQL. Starting another returns the existing timer and a usable next action.
An explicit switch can stop one and start another atomically. A restart, disconnect
or forgotten timer does not close it at an invented time. A running duration is an
as-of estimate from a timer report, not independent proof of continuous work.

Across sessions, detect intersecting intervals and likely duplicates. Preserve
the reports and surface the conflict; do not globally reject all work logging.
Correction, explicit concurrent-activity disposition or quantity allocation can
resolve the ambiguity. Financial readiness is evaluated separately.

### Correction, split and merge

Sparse updates use context-held versions. Corrections retain immutable originals
and genuine roots. A split/merge is one atomic domain operation with lineage and
conservation of known elapsed/reported duration. Superseded parents stop contributing
to current totals; their children do not count in addition to the parents.

Merging separated sessions preserves their segments and gaps: 10–11 and 13–14
remain two hours, not four. Incompatible attribution requires an explicit choice
for that operation. Retracting a duplicate preserves its history. Concurrent stale
updates reject affected changes with current-context guidance.

A session outcome does not automatically complete a linked Task or attest that an
artifact was delivered. A separately requested Task completion can share the same
guarded transaction when the command explicitly includes that effect.

## 5. Authority, evidence and incomplete work

Reuse `docket.read`, `docket.write`, `docket.triage` and `docket.events`. Both verified
token and current local owner grant remain required. The approved agent's full
write delegation continues to include chat, scheduled and event-triggered runs.
Work tracking introduces no foreground attestation or additional approval click.

Natural-language interpretation stays with the connected agent. Docket supplies
bounded choices, deterministic validation/calculation and persistence; it acquires
no mandatory inference provider or background agent. Client cron/local inference
can use the same inbox/context flow.

An authenticated report can be recorded without transcript/attachment capture.
Relayed reports remain honestly agent-reported and trace to `req_`; never invent
an `utt_`, require archived words, or label the relayed text as verified verbatim
operator evidence. Source/receipt links are optional supporting evidence.

Calendar entries, commits, emails and idle timers may suggest missing work. They
do not automatically establish actual hours, a payer, billability or payment.
Keep inferred candidates in bounded intelligence/proposal state until adopted by
an authorized write under an operator instruction or applicable standing policy.
That adoption need not occur in a live user turn. A model-supplied “confirmed” flag
is neither new authority nor proof of human presence. Extend typed triage/case
contracts where needed; do not pretend existing CaseItem types already describe
every new work/finance candidate. Restricted triage remains noncanonical.

Missing rates, labels, optional receipts or review flags do not stop valid session
recording, informal work exports, unrelated tasks or Calendar operations. They
affect only calculations or formal claims/documents that require those facts;
informal reports retain the gaps. No activity evidence or empty work history
proves inactivity or complete coverage.

## 6. Compensation, reporting and financial integrity

These are downstream capabilities over work revisions, not P0 prerequisites.
P1a supplies compensation evaluation and formal timesheets, P1b adds contractor
invoices and reimbursable expenses, and P1c adds settlement and reconciliation.
P0a's informal work report is already useful without any of these milestones.

| Question | Canonical basis |
| --- | --- |
| What work was reported/performed? | WorkSession revision and its evidence/precision |
| What is compensable or billable? | Applicable engagement/policy and explicit eligibility |
| What was reported to an employer or client? | Exact timesheet/report/invoice snapshot and submission evidence |
| What is owed? | An established obligation/claim with its adjustments |
| What was received or reimbursed? | Recorded payment/credit and settlement allocations |

### Effective-dated policies and calculations: P1a

P1a introduces typed, versioned compensation/reporting policies. Facts and free-text
Preferences can supply supporting terms, not silently become executable policy.
Policies specify applicable engagement/employer, dates, currency, basis, reporting
timezone/week/period boundaries, rounding and any configured hour-limit/overtime
rules. A change in rate is not retroactive merely because it is the newest record.
Split calculations at applicable rate/period boundaries without rewriting actual
work. Shared-employer rules can aggregate across engagements when configured;
do not assume every engagement is a separate payroll employer.

Worked time, compensable time and billable time are separate quantities; unknown
eligibility is not unpaid work. Hourly, salary, fixed-fee and unpaid arrangements
remain distinct. Salary/fixed-fee earnings are not invented by multiplying logged
hours by an inferred hourly rate. Record configured terms; do not infer legal
entitlements, student hour limits, taxes or deductions from an employer name.

Use exact decimal monetary arithmetic and explicit currencies, quantity precision
and rounding policies. Specify rounding scope (segment, session, day or period);
sum raw quantities before the applicable rounding step rather than rounding every
fragment introduced by a reporting/rate boundary.
Do not use binary floats, combine currencies, assume exchange rates or round away
an unexplained balance. Return estimates, policy coverage and unresolved amounts
separately. Gross compensation, expense reimbursement and received net money must
not collapse into one “earnings” number.

### Formal reporting and claim integrity: P1a–P1b

P1a prepares reporting periods from engagement rules or explicit bounds. A timesheet
can be drafted with missing rates; identify missing/unplaced time, classification
and overlap issues. Its own preparation/review/submission/approval/rejection state
is independent of payment. Preparing/exporting a report is not external submission.

Introduce the contribution/claim identity and quantity protections with P1a's
formal timesheets, before they can represent a submitted compensation claim.
Pin exact contributions, retain corrections and prevent duplicate claims for the
same purpose. This dependency does not pull invoices, expenses or settlement into
P1a. P1b extends the same mechanism to contractor invoices, partial billable
allocations and reimbursable expenses; it must not create a second claim ledger.

Use a typed claim/allocation mechanism before introducing claim-bearing documents.
Bind contributions to exact work/expense revisions, quantities, policy revisions
and economic obligation/purpose. Atomically prevent duplicate reserved/issued claims
for the same eligible quantity. Timesheet and invoice references may legitimately
describe the same work; they must not manufacture two receivables for one obligation.
Partial allocations and distinct explicit compensation components need their own
lineage, rather than a global `session.invoiced` or `session.paid` boolean.

Deduplication follows the stable work lineage across corrections, splits and
merges, not just the newest revision ID. A corrected 90-minute claimed session
becoming 120 minutes does not make all 120 minutes newly uninvoiced. Carry the
existing obligation forward and surface the 30-minute adjustment; reductions
similarly require reconciliation. Unresolved overlap affects the relevant claim's
finalization, not the ability to preserve actual-work reports.

A noncanonical preview reserves nothing. On committing a canonical document draft,
reserve its applicable claim quantities under locks/constraints. Concurrent drafts
must not both reserve the same entitlement. Cancelled drafts release reservations
with retained history. Finalization/issue binds the exact document and allocation
snapshot, number, client/payer, rates, currency, terms and due date.
Numbers are unique within the configured issuer/series; an unchanged finalization
retry does not issue another number or document.
Queries distinguish draft-reserved, reported, issued and still-available quantities;
an abandoned draft is discoverable and can be cancelled without losing the work.
Unknown rates/currency prevent affected invoice finalization, not timesheet drafting
or work recording. Never substitute a zero rate for missing policy.

An issued invoice or submitted timesheet is immutable as issued/submitted. A later
session/rate correction updates current reports and marks affected documents for
reconciliation. It never silently changes an earlier invoice, official timesheet
or its PDF. Use retained replacements, amendments, credit notes and adjustments.
Draft regeneration reports its changed basis and requires current versions.
P1a/P1b must retain reconciliation flags from the outset; financial credit and
settlement workflows arrive in P1c. Their absence never permits an issued snapshot
to be rewritten.

Invoice issuance/finalization and delivery are different operations. Due/overdue,
outstanding and partially paid amounts derive from obligations, dates and settlement
allocations, rather than another independent collection of status flags. Export
deterministic CSV/JSON and a usable document rendition with stable references,
currency, calculation basis and revision identifiers. Export alone transmits nothing.
The operator can record that a manual submission, approval or delivery occurred;
retain it as a reported observation without inventing successful provider execution.
An external reporting integration is not required for useful manual bookkeeping.

### Reimbursable expenses: P1b

Expense records are separate from hours and wages. Preserve amount/currency,
incurred date, vendor/category and attribution where known, reimbursement eligibility,
optional receipt/source, and ownership of equipment when reported. A reimbursed
flight does not establish paid travel time or ownership of purchased equipment.
Missing optional receipt capture does not prevent recording an expense; a configured
claim requirement can affect the reimbursement claim only.

P1b tracks expense eligibility, claim amounts and reported submission/approval
separately from money received. P1c adds refunds, credits, partial reimbursements
and unapplied amounts. Use stable external transaction identities for reliable
deduplication; matching date/vendor/amount alone is only a duplicate candidate.
Financial evidence is sensitive: reuse protected source/blob retention, avoid
unnecessary receipt copies, and never place raw documents or payment details in
invocation logs or webhooks.

### Payments, credits and reconciliation: P1c

A payment record represents reported or externally observed money received, with
that distinction preserved. Settlement allocations connect it to obligations and
must not exceed available amounts. Support partial payments, overpayment/credits,
unmatched payments and reversals without deleting history. A sent invoice or a
“payment coming” email is not evidence that money was received.

A net payroll deposit alone does not reconcile a gross wage obligation. Known
withholdings/fees/adjustments need explicit components/evidence; otherwise retain
the unresolved difference. Reimbursements stay separate from wages. No bank
transfer, payroll run, tax filing or net-pay advice is part of P1.

## 7. Calendar, capacity and professional history

Planned activity, commitment firmness and reported actual activity are separate
dimensions. A planned block may itself be a commitment; do not force them into
mutually exclusive time types. Link actual work to related plans/events without
silently rewriting their original schedule or assuming attendance.

P0a exposes optional `event_ref` on log/update/read contracts. It must resolve to
an existing owner-accessible Docket `evt_` record; linking it neither creates nor
edits the event and is not independent evidence of attendance or completion.
Work may differ from the event's schedule. The link never supplies missing actual
start/end bounds automatically.

P0a also preserves reported `work_mode`, `reported_location` and optional
`place_ref`; validate the latter against existing `ent_` entities of kind `place`.
These fields are optional context, independent of reporting timezone, payer,
compensation eligibility and expense reimbursement. Remote/onsite/travel is a
reported mode, not inferred physical-location evidence or proof of paid travel.
No place-creation ceremony is required to retain a location label or log valid work.

Logging actual work creates no Calendar/provider intent by default. Optional
actual-time visualization is an explicit projection with stable correlation,
clear actual/planned labeling and cancellation/update recovery. It neither adds
hours to accounting totals nor requires a duplicate editable time binding.
The implementation boundary is a revision-pinned snapshot: the title identifies
its source revision and net active hours; its span may include reported breaks.
Corrections, retraction and split/merge durably mark enabled displays for refresh
in the source transaction. Explicit refresh/hide uses the normal guarded provider
path. A blocked refresh cannot block or roll back an otherwise valid work command.
Superseded/retracted source snapshots can be hidden; displaying successor work is
a separate choice. Duration-only, incomplete or composite work without one known
bounded interval cannot invent one for display. Managed snapshot Events are read-only
through ordinary event editing. Stable wcal_ correlation and retained revisions
preserve earlier displays and delivery evidence. See SPEC ADR 009.

Capacity views combine known commitments, deadlines, travel constraints, protected
time, work-hour limits and optional project budgets. State the queried range,
timezone, source coverage and assumptions. Use interval unions so overlapping
meetings are not subtracted twice. An open calendar slot is potential availability,
not guaranteed usable capacity; unknown schedules and unplaced durations remain
visible. Future-capacity estimates and retrospective actual-hour totals are not
the same calculation. Never optimize every free hour by default.

Work description records activity; an outcome records an asserted result. Retain
both and link existing tasks/projects/artifacts where available. Accomplishment
summaries can group this history without a second journal or automatically turning
every activity into a verified achievement. Corrections support current and
historical/as-of views; absent reports do not mean nothing was accomplished.

## 8. Native context, commands and follow-up

Extend the current native context pattern, not the retired generic compiler.
A work packet supplies timezone/date, available engagements/projects, relevant
event/place references when useful, active timer, observed target versions,
coverage, reported classification/mode choices, relevant policy summaries and
useful actions. Optional context choices are not a mandatory questionnaire.
Reuse suitable context without a TTL/reread ceremony. Do not invent TriageRuns
to obtain an editing context or return the whole professional/financial history.

Proposed public vocabulary, to be verified with actual model-generated schemas:

| Phase | Tools/workflow |
| --- | --- |
| P0a reads/reports | `docket_get_work_context`, `docket_query_work_sessions` with informal JSON/CSV output; reuse entity/project/task/history reads |
| P0a recording | `docket_log_work_session`, `docket_update_work_session` including explicit retraction |
| P0a attribution | `docket_create_engagement`, `docket_update_engagement` |
| P0b timers | `docket_start_work_session`, `docket_pause_work_session`, `docket_resume_work_session`, `docket_stop_work_session` |
| P0b switch/lineage | `docket_switch_work_session`, `docket_split_work_session`, `docket_merge_work_sessions` |
| P1a | Work/reporting context, effective policy commands, eligibility evaluation, reporting periods and formal timesheets with contribution/claim protection |
| P1b | Contractor invoice drafting/finalization and expense recording, extending the same claim-allocation basis |
| P1c | Payment recording, partial settlement, credits and financial reconciliation |
| P2 | `docket_get_workload_context`, work/outcome summaries and bounded exception reconciliation |
| P3 | Only concrete separately specified provider operations |

Avoid a generic `manage_engagement`/`manage_invoice` action union or exposing every
storage table as CRUD. Names above are proposals, not new tools available today.
There is no replacement fixed tool-count requirement.

Ordinary writes use one `operation_id`, optional context-held targets and flat
business fields. All corrections and claim operations retain exact versions and
idempotency. Use existing preview/read/apply for meaningful previews; multi-object
work compiles through the same atomic engine. Do not assume the current distinct-
target prepared-batch adapter already implements work split/merge or claim locking.

### Contract size and discovery discipline

The initial design has six P0a tools and seven additional P0b tools. Export stays
on the query tool; previews/apply, entity/project lookup and history reuse existing
tools. This is a reviewed starting scope, not a permanent catalogue quota. Justify
any additional tool in the implementation notes instead of exposing every table.
Expose only implemented milestones; no placeholder P1/P2/P3 tools in discovery.

Apply these checks to the actual MCP `tools/list` definitions, including changes
to existing tools, under the same granted capabilities:

| Surface | Proposed implementation limit |
| --- | --- |
| Each new input schema, including referenced definitions | 4 KiB |
| Each complete new tool definition, including description, output schema and metadata | 6 KiB |
| Additional discovery for P0a / complete P0 | 20 KiB / 32 KiB |
| Shared instructions / routine work packet / normal result | Existing 2 KiB / 8 KiB / 16 KiB limits |

Measure UTF-8 compact JSON, not Python source size or an estimated token count.
The [contract and capability review](../docs/work-attribution-contract-review.md)
records synthetic feasibility measurements: logging 3,385 bytes, correction 3,711
bytes, six P0a definitions 14,744 bytes and all thirteen P0 definitions 26,584
bytes before their list separators. These are candidate schemas through the native
registration/compaction path, not evidence of implemented domain validation or a
production deployment. Later finance schemas remain unmeasured and must meet the
same per-tool limits when specified. Check context/result sizes during real MCP
acceptance rather than assuming this probe proves them.

Keep routine inputs flat. Bounded homogeneous break intervals or split parts are
allowed; a family-wide mutation union, arbitrary patch/filter dictionary or full
WorkSession/claim graph is not. Keep every accepted business field visible in the
discovered schema. Nullable scalar fields are ordinary optionals, not separate
business-operation variants. Docket derives versions, provenance, timer transitions,
derived totals and claim lineage; the agent never reconstructs those internals.

P1 tools must accept the fields for one business action, such as an engagement and
period for a timesheet or an invoice reference for finalization. They must not
repeat policy history, all work revisions and allocation graphs in each request.
Keep full history in bounded reads and return compact receipts. Domain guidance,
selected choices and relevant policy summaries belong in context, not expanded
global instructions or the generated reference catalogue. Page context choices
and result details within byte limits while retaining full-set totals/coverage.
These are engineering checks, not additional operator approval steps.

### Consolidate the existing catalogue without more complex inputs

The [existing-catalogue review](../docs/catalogue-simplification-review.md) proposes
an independent C0 slice before or alongside work attribution:

- Replace twelve `docket_retract_*` wrappers with `docket_retract_record`, retaining
  exactly the same fields: context, operation identity, target reference and the
  existing preview/correction options. Derive the existing typed effect from a
  closed reference-prefix map. Add no object/action selector or generic payload.
- Use existing `docket_update_task(state="completed")` for task completion and
  existing `docket_get_context(refs=[conflict_ref])` for conflict detail. Their
  surviving input schemas do not change.

This projects 84 existing tools to 71 while retaining routes for all 47 baseline
mutation variants. The new retraction schema is 612 bytes. Gross P0a/P0 additions
remain six/thirteen tools: after C0, projected totals are 77/84. These are measured
design projections, not an implemented inventory or a fixed-count invariant.

Four further reductions are conditional. Person, organization/institution and Item
detail reads can move into reference-based context only after their useful domain
facets, provenance and bounded detail access are preserved. Generic row reflection
is insufficient. Event cancellation can use the existing update `status` and scope
fields only after the service dispatches the exact cancellation semantics and
proves provider/recurrence equivalence. Retain those wrappers until then; added
input modes or lost capabilities are not an acceptable trade for a count.

Create/update and update/supersede retain their distinct business contracts. Keep
identity unbinding separate from handle retraction: both use `idn_` but represent
different intent. Preserve query/detail, provider-observation, profile, triage,
capture, prepared-work and historical-recovery boundaries. Normal event/task
commands continue deriving required support records internally; do not remove
independent temporal/routing/projection functionality just because it looks like
low-level storage.

C0 changes dispatch and discovery, not permissions or history. Preserve current
domain handlers, `req_`/`call_`, versions, immutable roots, exact prepared proposals,
atomic provider intent and unknown-delivery recovery. Maintain a capability
disposition for every old effect/read. Compatibility aliases may preserve cached
clients during refresh; they keep original permission/retry checks and never
rewrite old receipts or silently re-execute work. Update generated guidance, action
names and the same hosted plugin together. Report gross and net discovery sizes;
consolidation does not relax the per-tool budgets or block work attribution.

Illustrative P0 inputs, using references returned by the proposed work context:

```json
{
  "context_id": "<returned-work-context>",
  "operation_id": "client-a-work-20261105",
  "engagement_ref": "<returned-engagement>",
  "start": "2026-11-05T10:00:00-08:00",
  "end": "2026-11-05T12:30:00-08:00",
  "timezone": "America/Los_Angeles",
  "reported_compensation_class": "billable",
  "work_mode": "onsite",
  "reported_location": "Client A lab",
  "description": "Investigated intermittent device enumeration"
}
```

```json
{
  "context_id": "<returned-work-context>",
  "operation_id": "project-a-duration-20261104",
  "project_ref": "<returned-project-entity>",
  "work_date": "2026-11-04",
  "duration_minutes": 180,
  "timezone": "America/Los_Angeles",
  "description": "Project development"
}
```

These are future flat contracts, not payloads for today's Item/Time tools.
Either time shape may also carry optional `event_ref`, `place_ref`, work mode
and reported compensation classification; none requires the others. Context
supplies existing references when needed. Updates can explicitly clear these
optional fields, retaining their prior values in immutable revisions.
Accept the two time shapes with clear field rules rather than a giant mutation
union. Public errors identify the field and next useful action. Return a compact
work reference, recorded precision, duration/active-timer as-of state, attribution
and affected readiness issues, with reported classification separate from evaluated
eligibility. Preserve the existing 2/8/4 KiB instruction/context/routine-schema
targets and 16 KiB normal output budget.

Queries compute totals over the full matching set, separately from paginated
samples. Report recorded quantities, unique timed coverage, unplaced/duration-only
work, open-timer estimates, unresolved overlaps and eligibility uncertainty.
Never present a page subtotal or potentially double-counted sum as complete verified
hours. Support engagement/project/day/week views and explicit period timezone.
Queries over several years use indexed server aggregates and bounded pages, not
model-side summation of every historical session.

### Informal work reports: P0a

Return ordinary daily/weekly summaries by engagement/organization and project, with
unresolved attribution in its own bucket and a total of reported hours. Make the
same filtered report available as JSON or CSV through the work-query surface;
no finance setup or separate reporting lifecycle is required. Preserve the period,
timezone, as-of basis, known quantities and incomplete/overlap flags in either
format. Detail exports retain work references, reported classification and optional
event/mode/location fields. Read authority is sufficient; exporting creates no
claim, submission, payment or provider intent.

For example, a synthetic weekly report could show Client A 12.5 h, Employer B
8.0 h and Employer C 6.5 h, totaling 27.0 reported hours. These numbers are
illustrative, not operator records. Label this an informal reported-work summary,
not a verified or submitted timesheet. Unknown durations remain unknown, not zero;
open estimates and potentially overlapping time remain distinguishable from
recorded completed time and unique timed coverage.

JSON, CSV and ordinary query totals must use the same calculation basis over the
full matching set, independent of detail-page size. Support bounded continuation
for larger exports under existing output limits without silently truncating rows
or changing the report basis between pages. Formal document snapshots and financial
exports remain P1 capabilities; do not defer this basic export until then.

### Selective follow-up

Follow-up is exception-driven: forgotten timers, material overlap, reporting cutoff,
unclassified claimable work and overdue obligations. Reuse retained inbox,
Preferences and connected-client scheduling/inference. Deduplicate/suppress low-value
alerts and attach current subject refs/coverage. New reminder subjects, event types
and notification payloads need explicit schema/service changes. A reporting deadline
may use a genuine due-time record; do not fabricate Calendar events just to satisfy
the old reminder subject constraint. A weekly reconciliation is optional.

## 9. Implementation and rollout

P0a is the first feature implementation target: create a minimal engagement when useful,
record incomplete work, correct it, and retrieve/export hours across engagements.
P0b completes timer and lineage workflows. P1a–P1c/P2 retain the direction and
integrity requirements here; their concrete calculation/document contracts should
be settled after the work primitive is exercised. External P3 integrations need
a separate concrete provider delta. These are delivery boundaries, not a new manual
sign-off gate for each ordinary command.

| Milestone | Deliverable and completion condition |
| --- | --- |
| C0, independent/parallel | Consolidate twelve retraction wrappers and the task-completion/conflict-detail aliases without expanding surviving inputs. Preserve all baseline effects, authority, history and provider semantics; projected existing catalogue 84 → 71. Conditional rich-context/cancel reductions require their own equivalence evidence and do not block P0a. |
| P0a | Typed work/revision/segment and minimal engagement persistence; flat retrospective/duration-only logging, corrections, reported compensation classification, optional event/mode/location context and daily/weekly queries with overlap flags and informal CSV/JSON export through real MCP/PostgreSQL. No money prerequisite or external spreadsheet. |
| P0b | Durable timers, pause/resume, explicit switch and split/merge lineage for resolving overlaps and attribution. Together P0a/P0b complete core attribution. |
| P1a | Effective-dated rates/policies, compensable-time evaluation, reporting periods and formal timesheets. Exact contribution identity, immutable submitted snapshots and duplicate-claim protection exist before formal claims are recorded. |
| P1b | Contractor invoices and reimbursable expenses, extending P1a's contribution/claim basis to partial billable/expense allocations and reusing atomic quantity reservations. Employee reports and contractor invoices cannot double-charge the same obligation. |
| P1c | Payments, partial settlement, credits, adjustments and financial reconciliation. Received money stays distinct from earned, reported, invoiced and reimbursable amounts. |
| P2 | Source-grounded workload/outcome views, exception detection and selective inbox follow-up. Missing coverage remains explicit; no global daily administration requirement. |
| P3 | Chosen external reporting/delivery/observation adapters with scoped credentials, provider intent, idempotency and uncertain-outcome reconciliation. |

Implementation must extend canonical mutation families, ChangeSet grouping/handlers,
public-reference/history/context registries, query indexes and contract generation
deliberately. Add domain services; keep MCP handlers thin. New native work tools
should be purpose-built rather than auto-generated low-level timer/finance CRUD.
Update the existing capability disposition and hosted plugin/skill together.

Every canonical mutation admits authenticated `req_`/`call_`, carries `basis_refs`
and commits its revision, derived state and any required outbox/provider intent in
one PostgreSQL transaction. Current permission, idempotency and observed-version
checks apply to every phase. Typical work writes require no external operation.

Use ordered additive Alembic migrations, PostgreSQL concurrency/constraint checks
and migration-specific recovery. Preserve all old states and signed provenance;
no destructive clean reset, invented backfill or conversion of scheduled work into
actual work. Any changed derived cache must be invalidated in the same transaction
or durably marked for refresh so stale money/hours are not reported as current.

Keep the compact local gate and run affected storage/provider/release groups.
Prefer a few meaningful native journeys and PostgreSQL races to a new family of
duplicated wording/count snapshots. Release through source-bound local validation,
the tested image, drain, backup and health checks. Deployment and host refresh have
their own evidence; adoption alone does not prove them.

Canonical finance records are covered by the existing approved write delegation.
External submission, invoice sending and financial transfers need concrete supported
provider actions, credentials, exact intended targets and applicable operator
authorization. A draft/export, source email, `docket.events` grant or generalized
money-tracking request does not authorize those external effects. No banking/payroll
credentials or external submission are requested by this draft.

## 10. Acceptance evidence for implementation

| Area | Required observable result |
| --- | --- |
| First valid call | An independent MCP client logs 10:00–12:30 as 2.5 hours from flat fields; one canonical work record/revision, no forced Item/Event, no provider intent. Retry returns the same receipt. |
| Incomplete reports | Three hours on a project/day remain duration-only with no invented timestamps or payer. Missing rates/receipts do not block recording. Unknown date/duration stay unknown and visible. |
| Attribution | One project appears under two engagements and personal work without a global inferred payer. Unknown/future arrangements do not automatically become paid work. |
| P0a reported classification | Reported `billable` survives logging, corrections, queries and exports without a rate policy. Later eligibility evaluation preserves that assertion separately. `paid_work` never marks payment received or eligibility verified. Omitted classification remains recordable. |
| P0a event/location context | Optional existing Event/place links and remote/onsite/travel reports round-trip and can be corrected or cleared. Missing fields do not block logging; supplied references validate kind/ownership. No attendance, payer, compensation or provider effect is inferred. |
| Clock math | Midnight, offset/zone changes, DST folds, pauses and breaks produce elapsed-UTC totals. Adjacent bounds do not overlap; ambiguous/nonexistent local input yields a field error. |
| Timers and races | Crash/restart preserves an open timer. Duplicate start/stop and concurrent stop/correct/switch produce one valid transition or a useful stale conflict. Retrying does not recapture now. |
| Corrections | Prior revisions remain readable; split/merge preserves known time and gaps; current totals exclude superseded parents. No unrelated workflow blocks on an overlap. |
| Aggregation | Bounded pages and full-set totals agree; duration-only, open and overlapping reports are identified separately. Coverage never implies inactivity or completeness. |
| P0a informal export | Query, JSON and CSV agree on a synthetic 12.5 + 8.0 + 6.5 = 27.0 reported-hour week across engagements, including full totals across detail pages. Additional unknown/open/overlapping reports preserve their flags and calculation basis. Export is useful without rates and never implies a formal timesheet, claim or submission. |
| Contract usability/size | Record actual per-tool and aggregate discovery sizes against section 8, including descriptions/output schemas and affected existing tools. Real MCP first-attempt interval/duration logging, sparse corrections and optional metadata succeed with visible fields and field-specific errors. Context/results stay bounded without losing coverage or totals. Unimplemented phases stay out of discovery. |
| Catalogue consolidation | Every baseline effect/read has an equivalent retained route; surviving inputs do not acquire selectors, conditional requiredness or patch unions. Retries, stale/revoked/owner checks and domain-specific retraction/provider effects hold. Rich context and cancellation wrappers disappear only after coverage/behavior equivalence, with exact historical recovery and cached-client compatibility preserved. |
| P1a policy/timesheets | A rate change affects only its effective scope; a boundary-crossing session uses both appropriate calculation segments. Raw time and reported classification stay unchanged. Missing/conflicting policy leaves a partial estimate. Formal submissions pin exact contributions, retain prior snapshots and prevent duplicate same-purpose claims before P1b exists. |
| P1b claims/documents | Concurrent drafts cannot double-reserve eligible quantities. Timesheet/invoice reporting shares P1a lineage without double charging. A corrected/split work revision does not reset prior claims; it flags adjustments to an issued snapshot rather than editing it. Expense claims stay distinct from hours/wages. |
| P1c settlement | Partial payments/reimbursements, credits, reversals and unexplained net/gross differences remain exact and auditable. A sent invoice or expected-payment message never marks paid. |
| P2 intelligence | A scheduled meeting can suggest missing work but cannot create performed/paid hours automatically. Capacity states source coverage and assumptions. Optional outcomes and low-noise exception follow-up remain useful without minute-by-minute tracking. |
| Authority/privacy | Token/local-grant revocation and owner isolation hold for new contexts/records/exports. Restricted triage cannot write work or money. No fabricated utterance or raw source/payment data appears in invocation logs. Full approved automation retains its existing capability. |
| Operations | Fresh and populated PostgreSQL migration/recovery rehearsals pass as appropriate. Existing native calendar/task flows still pass; the same registered plugin exposes the implemented flat surface after refresh. |

Use synthetic engagements, rates, receipts and transactions for automated checks.
Do not create real work records, submit a timesheet or issue/send a real invoice
as a deployment smoke test. Document the actual implemented milestone and remaining
roadmap; do not label all phases complete after P0.

## 11. Proposed SPEC reconciliation and source references

On adoption, update SPEC sections 2, 4–6 and 8 for the implemented work model,
commands, time/claim semantics and acceptance. Clarify section 3's evidence/adoption
boundary without expanding restricted credentials or adding a human-presence gate.
Keep section 7's compact local validation model. Add architecture decisions for
typed performed work, independent financial dispositions, and immutable issued
snapshots; advance the version/history and this delta's indexed status together.
Later concrete finance/provider adoptions must identify their actual SPEC baseline.

Inspected primary implementation sources:

- [Entity/project identity](../src/docket/models/entities.py) and
  [registry schemas](../src/docket/schemas/registry.py).
- [Tracked-context models](../src/docket/models/tracked_context.py),
  [time/input contracts](../src/docket/schemas/tracked_context.py) and
  [tracked-context service](../src/docket/services/tracked_context.py).
- [Preferences/routing](../src/docket/models/policy.py) and
  [protected attachment evidence inputs](../src/docket/schemas/evidence.py).
- [Canonical mutation contracts](../src/docket/schemas/authority.py),
  [shared ChangeSet service](../src/docket/services/change_sets.py),
  [assembly grouping](../src/docket/services/changeset_assembly.py) and
  [prepared batches](../src/docket/services/prepared_batches.py).
- [Native context service](../src/docket/services/native_context.py),
  [native command service](../src/docket/services/native_commands.py),
  [public business schemas](../src/docket/schemas/business.py) and
  [current capability disposition](../docs/native-capabilities.md).

Drafting used repository inspection, the supplied proposal and its four-amendment
review. No production personal/financial records or external legal/payroll rules
were queried during drafting. Adoption and implementation status now live in SPEC
and the implementation report; earlier design probes are not runtime evidence.
