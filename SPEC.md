# Docket specification

Version: 1.1
Adopted: November 5, 2026
Implementation baseline: `3a578859265431ac0270ab1acb62b48bfb8d6717`

## 1. Status and change control

This is Docket's single current specification. It consolidates the signed ontology
and Operator amendments, including the adopted [context/local CI delta](deltas/2026-11-05T04-27-33Z-single-spec-context-and-local-ci.md).
Adoption and deployment are distinct. SPEC 1.1's C0–P2 and optional work Calendar
visibility are implemented and deployed from `3f8c23b412c3` (November 6, 2026), with
contract v67 and plugin 2.1.0. The tested image, migrations, native boundary and
hosted package read-back pass. This conversation retains its earlier tool cache;
fresh work-tool invocation in ChatGPT remains the separate host check recorded in
[the activation receipt](evidence/work-attribution-release/activation.json).
The prior native command/context baseline used plugin 2.0.0 and successful v51
invocations; its historical evidence remains in
[the original report](docs/native-context-local-ci.md).

Forward changes use dated `deltas/YYYY-MM-DDTHH-MM-SSZ-description.md` records,
identifying the SPEC baseline, changed requirements, rationale, compatibility,
implementation and acceptance. An Operator instruction to implement adopts the
scoped amendment. Reconcile SPEC, its decisions/history and [delta index](deltas/INDEX.md)
together. Readers need not replay every delta to discover current requirements.
Wire models remain machine definitions; AGENTS is an operating guide;
CONTRIBUTING and runbooks describe procedures, not competing specifications.

The Operator instruction to implement the [work-attribution delta](deltas/2026-11-05T21-18-26Z-work-attribution-and-personal-operations.md)
adopts its catalogue, work, reporting/finance and operational requirements.
Implementation, deployment and phase-specific acceptance are tracked in the
[implementation report](docs/work-attribution-implementation.md). P0 alone never
substitutes for finance/intelligence acceptance. Chosen external P3
providers still require the separate concrete provider delta specified there.

Track the new SPEC and forward deltas. Older ignored `specs/` and delta sources
remain private, in their existing provenance chain. Do not bulk import, rewrite
or relocate them. Signed `ONT-DELTA-2026-08-27` remains frozen at SHA-256
`3d744f4d021f8a605086152eb76743a7ec5a7ed2c8754694e38c1a891a14b5e1`.
The August 26 document remains historical, outside that authority chain.

## 2. Purpose and capabilities

Docket is a credential-bearing, single-operator personal operations service.
It retains durable authority, evidence, canonical personal context, triage state
and provider-operation intent. PostgreSQL is the sole durable system of record.
Provider APIs, agent output, transport sessions and logs are observations or
projections, never independent canonical authority.

The native OAuth MCP plugin at `docket.you/mcp/` is primary. Connected agents
supply inference and scheduling; local inference can use the same bounded
workflows. Docket requires no particular inference provider or agent cron.
Hermes, Discord and SearXNG are retired interfaces. Hermes/SearXNG source trees,
gateway tooling and project browser-automation artifacts are removed under the
Operator's additional cleanup instruction. Historical evidence remains in Git.

Capabilities include calendars/occurrences; tasks/items; time/temporal projections;
people, organizations and identity; affiliations, facts, relationships and
interactions; preferences, reminders and lane routing; attention cases, daily
briefs, retained notifications and conflict resolution. Native cutover must
account for all 47 baseline canonical mutation variants. An omitted capability
requires explicit reviewed retirement, not accidental discovery loss.

Extend this domain with typed WorkSession and Engagement aggregates, then
effective compensation policies, formal reports, invoices, expenses, payments and
source-grounded workload/outcome views. Reuse existing Entity projects/places and
Item/Task/Event references. Affiliation does not establish a payer or compensation
terms. Never seed real organizations, engagements or rates from illustrative text.

## 3. Authority and evidence

Verify OAuth issuer, audience, approved client, token dates and local owner grant.
`docket.read`, `docket.write`, `docket.triage` and `docket.events` remain separate.
Both verified token and current local owner grant must permit the operation.
Schema changes do not require owner reapproval or a grant-epoch bump. Other
service credentials retain their distinct roles and limits.

The approved native connection has full delegated write authority in live chats,
scheduled runs and event-triggered runs. Interactive role denotes capability,
not proof of human presence. No foreground attestation, separate approval click,
Discord sign-off or ledger ceremony is required. Untrusted content and model
inference cannot grant permission. Restricted triage produces intelligence or
bounded suppression under an existing Preference, never inherited write authority.

Before write execution, admit durable authenticated `req_` attribution and audit.
Every authenticated invocation creates `call_`, including validation, permission,
conflict and service rejection. Canonical mutations carry public `basis_refs`
tracing operator-owned work to its authenticated request. Historical work retains
its genuine utterance roots and original constraints. Public references cross
tool/audit boundaries; internal UUIDs do not.

Transcript, attachment and response capture is optional, separately committed and
honestly agent-reported. Missing archives do not block valid work. Never fabricate
`utt_` evidence for relayed text. Later input does not destroy earlier evidence.
Evidence truth and canonical truth remain distinct. Never persist raw arguments,
results, authorization headers, Gmail bodies or transcripts in invocation logs.

## 4. Canonical workflow and concurrency

Handlers authenticate and validate; services own business rules/transactions.
Resolved intent compiles to an immutable `chg_`. Canonical state and required
provider Operation intents commit in one PostgreSQL transaction; external calls
run afterward. Public commands compile, stage and commit through this shared
engine rather than introducing alternate direct-write paths.

Draft corrections retain immutable revisions within admitted effect/target scope.
Missing values, stale versions, invalid occurrence scope and unresolved domain
conflicts block affected work. Optional archives and fine first-reading semantics
do not introduce global gates. Conflict resolution uses the same guarded service.

Use expected versions, stable idempotency identities and exact public references.
Applying prepared work commits its exact immutable proposal, never whichever
draft is latest. Unchanged retries return the original receipt. Corrections
retain request/audit roots and receive new exact proposals. Current permission
and target checks still apply. Handles/IDs grant no authority. Preserve unfinished
historical requests and bounded recovery of exact saved drafts; never delete or
silently translate unknown intent.

### Performed work and engagement integrity

WorkSession has a stable public reference, operator ownership, optimistic head,
immutable revisions and normalized active/break segments. It supports bounded,
duration-only and incomplete/unquantified reports; unknown is not zero. Preserve
description, optional outcome/category, attribution to organization/engagement/
project/Item/Task/Event, optional reported location/place and remote/onsite/travel
mode, genuine request/evidence roots, source method and uncertainty. No support
Item/Event, rate, receipt or affiliation bootstrap is required to record work.
Unresolved attribution can remain a label. An Event link proves neither attendance
nor actual bounds and never edits the event. Validate referenced kinds/ownership.

Retain optional reported compensation class (`billable`, `paid_work`, `unpaid`,
`unknown`) separately from evaluated eligibility and money received. `paid_work`
reports expected compensation, not a payment. Policy evaluation never rewrites the
operator's assertion; explicit corrections retain the earlier revision. Calendar,
email, commits and model confidence are observations/candidates, not proof of work,
billability or payment. Candidate adoption requires authorized intent/policy and
does not add a live-user-presence requirement.

Engagement records a particular employment, contract, research, volunteer or
personal arrangement, known organization/legal payer, effective-date precision,
role/reporting notes and later policy revisions. Projects may span engagements;
project labels alone do not select a payer. Personal/unpaid/future arrangements
remain useful without pay configuration. Split or allocate quantities for multiple
engagements; never clone full hours into each one.

Store actual instants in UTC with supplied offsets/timezone context. Support
midnight, DST folds and travel between zones without Calendar's shared-fold limit.
Validate real local times, half-open intervals, positive durations and break
containment. Subtract the break union once; preserve raw elapsed time separately
from report rounding. Duration-only date/period attribution does not invent clock
bounds or evenly distribute work across periods. Unknown dates stay unplaced.

Timer start/pause/resume/stop captures one effective server instant under a durable
command identity; retries or exact-preview application cannot recapture now.
PostgreSQL enforces one active primary timer per operator. Switch is atomic.
Disconnect/restart never fabricates an end. Running durations are as-of estimates.
Overlaps flag affected totals/claims without globally blocking reports or tasks.
Sparse corrections preserve history. Atomic split/merge conserves known quantities
and gaps; superseded parents no longer contribute to current totals. A reported
outcome does not complete a Task without an explicit effect.

### Reporting, claims and settlement

Use effective-dated typed compensation/reporting policies with explicit payer,
currency, basis, period timezone, rounding scope and configured hour/overtime
rules. Split calculations at policy/period boundaries, not actual work history.
Sum raw quantities before applicable rounding; use exact decimals. Keep worked,
compensable, billable, reported, owed and received quantities distinct. Missing or
conflicting terms yield partial estimates, not zero pay. Salary/fixed fees are not
inferred hourly wages; legal/tax/student rules are never inferred from names.

Formal timesheets have explicit reporting periods, exact contributions, preparation/
review/submission/approval/rejection state and independent payment state. Establish
stable contribution/claim identity and duplicate-quantity protection before formal
claims, extending that same lineage for contractor invoices/expenses. Bind work or
expense revision, quantity, policy revision and economic purpose. Prevent duplicate
reservations/issued entitlements transactionally, including across corrected or
split sessions; 90 claimed minutes becoming 120 creates a 30-minute adjustment,
not 120 fresh unclaimed minutes. Distinct reporting references do not duplicate
one obligation. Unresolved overlap affects the relevant finalization only.

Noncanonical previews reserve nothing. Canonical claim-bearing drafts reserve
applicable quantities; cancellation releases reservations with history. Finalization
pins exact contributions, monetary basis, document number unique per issuer/series,
client, terms and due date. Retrying never issues another document/number. Unknown
rates/currency block the affected invoice finalization, not logging or report
drafting. Submitted timesheets and issued invoices are immutable snapshots;
corrections flag reconciliation and use retained amendments/replacements/credits.
Draft-reserved, reported, issued and available quantities remain queryable.

Expenses are separate records with date, exact amount/currency, vendor/category,
attribution, reimbursement eligibility, optional protected receipt and reported
equipment ownership. Missing optional capture does not block recording. Travel
reimbursement does not establish paid travel time. Track claim/submission/approval
separately from partial reimbursement, refunds and credits. Stable external IDs
deduplicate transactions; matching amount/date/vendor only suggests a duplicate.

Payments preserve reported versus externally observed origin and exact settlement
allocations, with partial, unmatched, overpaid, credit and reversal states retained.
Allocations cannot exceed available money/obligations. Net payroll deposits do not
silently settle gross wages without known deduction/adjustment components; retain
the unexplained balance. Derive due/overdue/outstanding from dates and settlement.
Invoice delivery or expected-payment messages do not prove receipt. Gross earnings,
reimbursements, expected net and received money remain separate.

Informal work summaries/export work in P0 without finance setup. Formal CSV/JSON
and usable document renditions retain stable references/calculation/revision basis.
Export creates no submission/provider intent. Operator-reported manual submission,
approval or delivery is useful without an integration and is not fabricated
provider success. External submission/sending requires a concrete supported,
authorized target/action. No bank transfers, payroll execution or tax filing is
included in this amendment.

## 5. Native context and commands

Overview context supplies configured timezone/current date, actual capabilities,
defaults and short next-tool guidance. Domain packets supply selected targets,
exact references, observed versions/state and allowed actions. General contexts
are durable owner- and purpose-bound snapshots. Existing triage ContextPackets
retain their run/source semantics; normal editing never invents a triage run.
Reuse suitable context without arbitrary TTL or mandatory reread gates.

Distinguish canonical facts, provider observations and reported context. Include
freshness and incomplete coverage. Reading a packet implies neither provider
refresh nor absence from an incompletely observed calendar. Changed state
invalidates affected optimistic preconditions, not unrelated work.

Expose strict flat business tools for routine operations across supported domains.
Clients supply ordinary business fields and one stable `operation_id` for a command
and its retries. Docket derives request/execution identities, support records,
internal action grammar, compiler scope and exact draft bindings. Normal native
discovery contains neither nested generic mutation unions nor the three-field
`work` envelope. There is no fixed tool-count invariant.

Calendar context returns known destination references. Event creation accepts
`context_id`, `operation_id`, `title`, ISO `start`/`end`, `timezone`, `calendar_ref`
and optional `location`/`notes`. Accept offset timestamps and normalize server-side.
Conflicting zone/offset, nonexistent local time and reversed intervals yield
public-field errors; offsets can disambiguate DST folds. Provider UUIDs, raw
calendar IDs, compiler timing kinds, import/support IDs and fold flags are not
ordinary client requirements. Names in notes do not send invitations; additional
provider effects require explicit supported operations.

Updates use context-held versions and field-level provider patch guards. Recurring
work specifies occurrence/series scope. Errors identify the public field, expected
shape and useful next action, rather than only a Python class or union failure.
Correcting input does not require renewed authority.

Optional prepare/read/apply supports requested previews, imports and atomic
multi-object work. Preparation returns bounded preview and immutable opaque
handle; apply uses the handle's durable retry identity. The internal compiler
and isolated historical recovery can remain without public compiler aliases.
Batching composes owned ready previews for distinct targets. Applied members
recover the parent receipt instead of executing again. Conflict decisions can
atomically include an exact business preview; attention dispositions use observed
case versions/revisions. Bounded saved-work reads preserve exact historical JSON
and authentic roots without silently translating or executing retired drafts.

Global instructions briefly explain authority, trust, retries and delivery.
Domain guidance lives in packets. Never inject this SPEC or a giant generated
catalog. Initial engineering budgets: 2 KiB global instructions, 8 KiB routine
context and 4 KiB routine write schema, measured on actual transport. Normal JSON
output caps at 16 KiB; explicit audit caps at 64 KiB with pagination. Default
lists use 25 items, maximum 100. Omit null optionals, internal UUIDs, duplicate
snapshots and unsolicited verbatim history. Never rely on transport truncation
or silently discard required facts.

Work context supplies bounded engagements/projects, relevant event/place choices,
active timer, observed versions, coverage, short policy summaries and actions.
Connected agents interpret natural language; Docket validates/calculates/persists.
P0 named tools log/correct/query/export work and manage minimal engagements, then
start/pause/resume/stop/switch timers and split/merge. Ordinary interval or duration
inputs are flat; bounded homogeneous break/split lists are permitted. Do not expose
internal revision/claim graphs, an action union or every new storage table as CRUD.

Each new input schema including definitions is at most 4 KiB; each complete new
tool definition at most 6 KiB. Gross additional P0a/complete-P0 discovery budgets
are 20/32 KiB. Measure compact UTF-8 actual MCP definitions, including affected
existing tools, descriptions, output schemas and metadata. Keep the 2/8/16 KiB
instruction/context/normal-result budgets. Publish only implemented milestones;
future finance schemas must independently meet the same per-tool budgets.

Consolidate twelve legacy retraction wrappers into one closed typed-reference
command with the existing context/operation/target/preview/correction fields. It
selects the existing domain handler, not generic deletion. Task completion uses
existing task update state; conflict detail uses existing reference context.
Preserve all 47 baseline effects and unchanged surviving inputs. Rich person/org/
Item detail and event cancellation wrappers may retire only after equivalent
facets/bounded detail access and exact cancellation/provider/recurrence behavior
are verified. Keep distinct create/update, update/supersede, identity unbinding,
provider observations, restricted triage and historical recovery. Preserve cached
client aliases with original permission/retry checks during refresh, and genuine
old receipts/exact saved drafts. No count reduction justifies more complex inputs.

Queries aggregate the full matching set independently of detail pages, with explicit
timezone, period, as-of basis, precision and coverage. Distinguish recorded totals,
unique timed coverage, duration-only/unplaced reports, overlaps and timer estimates.
CSV/JSON informal exports use the same basis and preserve reported classification,
optional event/location context and continuation without truncation or invented
completeness. Unknown attribution has its own bucket. Multi-year reads use indexes
and server aggregation, not model-side summation of history.

Planned activity, commitment firmness and actual work remain separate dimensions.
Optional actual-time Calendar visibility is an explicitly requested correlated
projection, never duplicate accounting time or a default provider effect. Displays
pin an exact work revision and label its net active hours separately from its span.
Source corrections, retraction and split/merge invalidate enabled displays in the
same transaction. Refresh/hide uses normal provider readiness; a blocked display
operation never rolls back valid work recording or correction. Display Events are
read-only through ordinary event tools. Workload
views combine commitments, deadlines, travel, protected time, configured limits and
budgets with interval unions and explicit assumptions/coverage. Free calendar space
is potential availability, not guaranteed capacity. History/outcome summaries
distinguish activity from asserted achievement. Missing work does not prove
inactivity. Selective exception follow-up for timers, overlaps, cutoffs, unclassified
work, money and reimbursements reuses inbox/preferences/client scheduling; extend
typed reminder/event/case subjects explicitly. No fabricated Calendar events to
satisfy an old reminder type; no compulsory daily maintenance or constant optimizing.

## 6. Providers, storage and recovery

Google OAuth credentials are protected local material. Calendar/Gmail ingestion
is bounded evidence capture. Adapters project committed intent, never canonical
truth. Report queued, successful, failed and uncertain delivery honestly.
Unknown-after-transmission outcomes reconcile before retry; never blindly resend
or invent success. Disabled production gates pause/fail closed. A canonical
commit does not prove provider delivery.

Use SQLAlchemy models/services and ordered forward Alembic migrations for durable
changes. PostgreSQL is authoritative for locking, constraints, triggers and JSON;
SQLite provides fast behavior checks only. Preserve append-only/immutable rows.
A migration may suspend a trigger only around narrowly scoped reversible work.
Never edit production rows to pass checks or add compatibility domain backfills.

Historical clean-ledger export/reset rules retain their original authority; this
amendment authorizes no destructive reset/restore. Image rollback cannot reverse
a migration. Retain a verified backup, prior image and migration-specific recovery.

Work/engagement revisions, normalized segments and issued/submitted document
snapshots are immutable in PostgreSQL as well as application code. Mutable heads,
indexes and derived totals change atomically with the admitted canonical revision;
invalidate affected caches durably rather than returning stale hours/money as
current. Use locks/constraints for timer ownership, quantity reservations and
settlement limits. New tables use ordered additive migrations and preserve existing
rows/provenance; no semantic calendar backfill or destructive reset is implied.

## 7. Local validation and release

Local validation is release authority. GitHub hosts source/review; Actions does
not gate merge/deploy or automatically duplicate releases. Preserve history
protections. No manual attestation, acceptance phrase, signing infrastructure or
production ledger approval is required for local execution evidence.

The fast native gate runs a compact justified suite, Ruff and strict mypy.
Extended provider, storage/migration and release groups run for affected changes.
Delete retired gateway scaffolding and duplicate assertions; merely relabeling
all tests or hiding them in giant loops is insufficient. Initial targets are
roughly 100–200 native cases and a one-minute warm gate, measured honestly as
engineering targets. Distinct safety failures matter more than a numerical quota.
Checks never use production `.env` or credentials.

Release validation uses a stable tracked-source snapshot excluding ignored private
provenance/runtime data. Automatic evidence binds source-tree digest, lock/check
policy identity, required groups/outcomes and tested immutable image digest.
Changed source, missing checks or a different image invalidates it. Identical
content after rebase/merge may reuse evidence. Deploy the tested image without
rebuilding it. Documentation-only iteration does not require database rehearsal.

Releases require clean synchronized `main`, production configuration and valid
local evidence. Preserve durable drain, pre-barrier execution completion, idle
checks, pre-migration backup, schema upgrade and postdeploy health/authentication.
Queued operations/reconciliation survive restart and do not block drain; timeout
leaves active work intact.

Explicit development instructions authorize local implementation. Standing release
authority covers related branches/PRs, locally validated merges/deployments,
required ingress and approved-owner configuration. Unrelated provider effects,
sending messages, destructive reset/restore and new authority grants remain
outside that release authorization.

## 8. Acceptance

Verify actual schemas and real MCP transport on disposable PostgreSQL: context
then first-valid-attempt flat event creation; retries without duplicate event or
provider intent; update/cancel and task workflows; unknown provider delivery;
stale/timezone errors; exact prepared proposal; permission/revocation/owner
isolation. Preserve delegated automation and restricted service credentials.
Cutover accounts for 47 effects and unfinished drafts, updates the same registered
plugin/skill and reports schema/instruction/context sizes, removed tests and
measured runtimes. No OAuth reapproval is planned. Local evidence must reject
stale/missing checks and reuse content-identical merge evidence without Actions.

For the 1.1 amendment, prove each phase's declared behavior against the
[implementation matrix](docs/work-attribution-implementation.md): interval and
incomplete work, corrections/timezone/breaks, timer/lineage races, full-set exports,
effective-policy rounding, immutable formal claims/documents, concurrent quantity
reservations and exact settlement, source-grounded workload and selective follow-up.
Measure actual MCP contracts and first-valid-attempt user journeys on isolated
PostgreSQL; verify owner/revocation/restricted-role and privacy boundaries. Report
implemented and deployed milestones independently; work logging alone does not
complete this amendment. Real financial/provider effects are not smoke fixtures.

## 9. Architecture decisions

| ADR | Decision | Reason |
| --- | --- | --- |
| 001 | One tracked SPEC plus dated delta/index adoption | Current rules remain readable without replaying scattered amendments. |
| 002 | Source-bound local validation and tested-image releases | Remove duplicate remote waits while retaining release safety. |
| 003 | Owner-bound context and flat business commands | Agents supply intent rather than operate an internal compiler grammar. |
| 004 | Full native delegated write authority and optional archives | Authority comes from verified grants, not presence/capture ceremonies. |
| 005 | Remove Hermes/SearXNG trees and browser artifacts | Plugin and Google integrations own the supported user path. |
| 006 | Typed performed work and independent engagement/policy state | Actual effort and reported attribution remain useful before finance, without overloading generic metadata or schedules. |
| 007 | Stable quantity/claim lineage and immutable formal snapshots | Corrections cannot duplicate claims or silently rewrite issued/submitted history. |
| 008 | Reference-dispatched shared retraction and equivalent alias consolidation | Reduce discovery without adding action unions, conditional input modes or lost domain effects. |
| 009 | Opt-in revision-pinned work Calendar snapshots with durable source invalidation | Provider readiness affects display refresh, preserving usable actual-work records and one accounting source. |

## 10. Version history

| Version | Change |
| --- | --- |
| 1.1 | Adopts the November 5 work-attribution/personal-operations delta including catalogue consolidation. C0–P2 and optional visibility are implemented/deployed, with separate phase evidence; external provider choices remain a concrete follow-on delta. Implementation clarifies revision-pinned visibility and nonblocking invalidation (ADR 009). |
| 1.0 | First current SPEC synthesized from signed ontology and Operator amendments; adopts November 4 context/local-CI delta and retirement cleanup. No invented prior version. Implemented, deployed and verified through live native-tool use. |
