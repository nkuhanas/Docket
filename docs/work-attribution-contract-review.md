# Work attribution: contract review and capability extension

Review date: November 5, 2026. Proposal:
[work attribution delta](../deltas/2026-11-05T21-18-26Z-work-attribution-and-personal-operations.md).
Source baseline: `e349f23d557ea7e1b25de17d16932777da7dccb5`.
Status at review: design feasibility checked; proposal was unadopted/unimplemented.
The subsequent Operator instruction adopts [SPEC 1.1](../SPEC.md); actual progress
is in the [implementation report](work-attribution-implementation.md). Measurements
below remain historical design evidence, not implementation or a second specification.

Current source now implements the primary catalogue consolidation and thirteen P0
definitions: 84 advertised tools total, with 28,228 bytes of work definitions.
Every new input is below 4 KiB and complete definition below 6 KiB. Known-time
and mixed-time split/merge are checked; finance/intelligence remain unfinished.
The implementation report records exact evidence and the separate release boundary.

The extension adds actual-work records first, then reporting/financial state and
cross-domain operational views. Those capabilities can use compact business
commands. Claim allocation, immutable revisions and compensation evaluation are
service responsibilities; the calling agent does not construct their graphs.

## Contract size findings

The draft's original size targets were sound, but its switch/split/merge tool
selection and aggregate discovery growth were unspecified. The amended draft names
those operations, sets per-tool and phase budgets, and excludes unimplemented
milestones from discovery. Export shares the query tool; existing preview/apply,
entity/project and history tools are reused.

The [probe](../evidence/work-attribution-contracts/probe.py) registers thirteen
candidate Pydantic input models on a disposable `PluginMCP`, using the same
`model_function` and schema compaction as current tools. It calls only `list_tools`;
candidate command callbacks reject execution. Configuration is synthetic, `.env`
is excluded, and no database, OAuth flow, provider or personal records are used.
The models cover the proposed work fields, source references, breaks, partial
time, optional billability/event/location, sparse corrections, timers and lineage.
They do not implement cross-field/business validation, transactions or persistence.

Measurements are compact UTF-8 JSON, including referenced schema definitions.
Discovery includes descriptions, output schemas, annotations and OAuth metadata;
the JSON-RPC envelope and network encoding are excluded. These bytes are not token
counts. Exact results and source/lock/probe digests are in the
[measurement record](../evidence/work-attribution-contracts/metrics.json).

| Candidate tool | Phase | Input schema bytes |
| --- | --- | ---: |
| `docket_get_work_context` | P0a | 377 |
| `docket_query_work_sessions` | P0a | 1,667 |
| `docket_log_work_session` | P0a | 3,385 |
| `docket_update_work_session` | P0a | 3,711 |
| `docket_create_engagement` | P0a | 1,357 |
| `docket_update_engagement` | P0a | 1,521 |
| `docket_start_work_session` | P0b | 2,267 |
| `docket_pause_work_session` | P0b | 553 |
| `docket_resume_work_session` | P0b | 553 |
| `docket_stop_work_session` | P0b | 641 |
| `docket_switch_work_session` | P0b | 2,456 |
| `docket_split_work_session` | P0b | 1,300 |
| `docket_merge_work_sessions` | P0b | 934 |

All candidates fit the proposed 4 KiB input and 6 KiB complete-definition budgets.
Logging has 29 declared fields and correction 31, including shared command fields;
most are optional. Logging requires context, operation identity and description,
plus whatever time/attribution was actually reported. Correction requires context,
operation identity, the selected work reference and a meaningful change. The probe
does not enforce that last business rule. It introduces no operation-discriminator
union; nested definitions are limited to a break interval or split part.

| Discovery footprint | Tools | Compact bytes |
| --- | ---: | ---: |
| Current source baseline | 84 | 119,691 |
| Additional P0a definitions | 6 | 14,744 |
| Additional P0b definitions | 7 | 11,840 |
| Combined baseline plus complete P0 | 97 | 146,288 |

The baseline is already about 117 KiB; this review does not call that catalogue
small. P0 adds about 26 KiB, or 22%, including list separators. P0a alone adds about
14.4 KiB. The proposed limits are 20 KiB additional discovery for P0a and 32 KiB
for complete P0, including affected existing definitions. These are initial design
budgets, not a permanent tool-count quota. Later finance definitions have not been
designed or measured; do not claim they already fit.

Keep global instructions within 2 KiB (current source: 1,643 bytes), routine context
within 8 KiB and normal responses within 16 KiB. Selected engagements and short
policy summaries belong in work context. Full revisions, receipts, policy histories
and document details require bounded reads. Context/result sizes, actual transport
shapes and first-valid-call usability must be verified during implementation;
this schema probe does not establish those results.

The subsequent [existing-catalogue review](catalogue-simplification-review.md)
adds a separate C0 consolidation proposal: 84 existing tools project to 71, with
unchanged surviving input schemas. P0a then projects to 77 tools and 120,338
discovery bytes, only 647 bytes above today's source inventory. Complete P0
projects to 84 tools. Four further removals depend on rich-context and cancellation
equivalence; their savings are not assumed for C0. Gross P0 measurements above
remain valid and are not rewritten as implemented results.

## Capability classes and interaction

These are domain capabilities, not new OAuth scope names. Existing `docket.read`
and approved `docket.write` cover their reads and canonical writes. `docket.triage`
stays restricted; `docket.events` supports notification subscriptions rather than
granting work or financial authority. Approved scheduled/event-driven clients keep
their existing delegation. No extra human-presence or capture requirement is added.

| Milestone | Capability added | Illustrative operator interaction | Result |
| --- | --- | --- | --- |
| P0a | Actual-work attribution and informal reporting | “I worked four hours for Client A yesterday; billable, remote.” | Durable duration-only work with the reported date/classification/mode; no invented clock times, rate or payment. Corrections and CSV/JSON totals work without finance setup. |
| P0b | Live time tracking and conserved work history | “Start Project A.” “Pause.” “Switch to Client A.” “Done.” | One durable primary timer; retry-stable transitions, breaks and optional outcome. Split/merge preserves elapsed quantities, gaps and lineage. |
| P1a | Compensation evaluation and formal employer reporting | “Prepare Employer B's timesheet for this pay period.” | Effective policies calculate eligible/reportable quantities and gross estimates; a formal snapshot distinguishes preparation, submission and approval from receipt of money. |
| P1b | Contractor and reimbursement claims | “Draft Client A's invoice through Sunday and include this expense.” | Invoice/expense drafts use eligible unclaimed quantities and exact rates; duplicate claims are prevented. Drafting/issuing does not send or pay the invoice. |
| P1c | Payment and financial reconciliation | “Record this partial payment against that invoice.” | Received money is allocated exactly; unpaid balances, reimbursements, credits and unresolved differences remain visible. Expected money is not treated as received. |
| P2 | Capacity, work history and exception-focused operations | “What needs attention this week?” | Source-grounded commitments/capacity, unreported work candidates, cutoffs, unpaid obligations and accomplishments; gaps stay explicit. Calendar events do not prove actual work. |
| P3 | Chosen external reporting/delivery adapters | “Submit this exact prepared timesheet through the configured provider.” | Only separately specified, authorized provider operations with delivery/reconciliation evidence. No automatic payroll/banking integration is implied. |

## Ordinary user workflow

The operator speaks to the connected agent in normal language. The agent resolves
dates/timezone and available references from compact context, interprets intent,
and sends a named command with one stable operation identity. Docket validates,
calculates and commits, then returns the work reference, known quantity/precision,
attribution and any relevant unresolved issue. No stage/compile/commit grammar,
internal UUID, evidence binder, allocation graph or transcript upload is required.

- Retrospective report: “Three hours on Project A yesterday.” Read suitable context
  if needed, then log a dated duration. Missing engagement/rate can stay unknown or
  a reported label; creating an engagement is useful when requested, not a gate.
- Exact report: “Client A, 10 to 12:30, with a 15-minute break.” Log the reported
  interval/breaks; Docket calculates net duration. Event/location links are optional.
- Correction: “That session ended at 12:45.” Resolve the intended current work
  reference and update only the changed fields. Prior revisions remain readable;
  an ambiguous target needs a choice, not a guess or renewed authorization.
- Query/export: “Hours by employer this week, and give me CSV.” The query tool
  returns full-set totals plus bounded details in the selected format. It labels
  unknown attribution, open estimates and overlaps; no official timesheet or claim
  is created by this read.
- Later finance: request a timesheet, invoice draft or reconciliation by its
  business meaning. Docket selects applicable policy/contributions server-side;
  external delivery remains a separate supported operation.

Valid ordinary commands commit directly. Preview is optional when requested;
existing exact prepare/read/apply handles cover it. Reuse context while suitable,
refresh affected versions after changes, and retry unchanged operations using the
same identity. Missing optional metadata never becomes a global execution gate.

## Reproduce and implementation follow-through

From the repository root:

```sh
.venv/bin/python evidence/work-attribution-contracts/probe.py
```

The probe asserts candidate input/definition/aggregate bounds and rejects generic
operation unions. The implementation must repeat these checks on its real models,
include altered existing tools, align generated contracts/skill, and verify useful
first-attempt calls over MCP on isolated PostgreSQL. No generated plugin contract,
permission, migration, SPEC adoption or production deployment changes in this review.
