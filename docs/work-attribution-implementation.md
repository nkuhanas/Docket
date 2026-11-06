# Work attribution and personal operations implementation

Requirements: [SPEC 1.1](../SPEC.md), adopted from the
[November 5 delta](../deltas/2026-11-05T21-18-26Z-work-attribution-and-personal-operations.md).
Starting source: `3a578859265431ac0270ab1acb62b48bfb8d6717`.
Status: C0–P2 and optional Calendar visibility implemented and deployed from
`3f8c23b412c3`; the same private plugin is published as 2.1.0. Fresh-chat work-tool
acceptance is Operator-confirmed and corroborated by successful v67 server calls in the
[activation receipt](../evidence/work-attribution-release/activation.json).
Design probes and catalogue projections are not implementation acceptance evidence.

## Scope and completion tracking

The full objective includes C0, P0a/P0b, P1a/P1b/P1c and P2. A completed foundation
does not complete the finance or operational-intelligence requirements. Conditional
wrapper retirements require equivalent behavior, not a target catalogue count.
P3 explicitly requires a separately chosen concrete provider delta; this release
does not invent a payroll/banking integration or require its credentials.

The table is the current acceptance state. Detailed phase entries below retain
their historical source checkpoints; the November 6 activation section records
their subsequent tested-image validation and deployment. Runtime code is unchanged
by later documentation receipts. Current source contract sizes are recorded
[separately](../evidence/work-attribution-release/contracts.json).

| Requirement | Required evidence | Current state |
| --- | --- | --- |
| Adoption | Single current SPEC, ADR/history and delta index reconciled | SPEC 1.1, ADRs and index reconciled; activation receipt distinguishes deployment from fresh-host acceptance |
| C0 catalogue | Shared retraction through existing typed handlers; completion/context aliases; all baseline effects; no larger surviving inputs; cached-client permission/retry preservation | Implemented/deployed: 84 → 71 existing tools; typed-plan/native retry/alias checks and exact-image PostgreSQL native smoke pass |
| Conditional consolidation | Rich person/org/Item facets and bounded detail remain reachable; exact recurrence/provider cancellation parity | Retain these four wrappers: generic context lacks the rich facets, and update/cancel use distinct canonical effects; no mandatory count target |
| P0a domain | Typed owner/versioned Engagement and WorkSession, immutable revisions and normalized segments; additive migration; no forced Item/Event/payer/rate | Implemented/deployed: six native recording/context/query tools; first-call MCP journeys and exact-image PostgreSQL retry/correction/immutability checks pass |
| P0a reporting | Flat interval, duration-only and incomplete recording, sparse audit-preserving correction/retraction; classification/event/mode/location; field-level validation | Implemented/deployed; native work/report/lineage journeys cover optional metadata, null clearing, unknown quantities and field-specific errors |
| P0a queries/export | Full-set daily/weekly/engagement/project totals, unique/overlap/unplaced/unknown coverage, stable bounded JSON/CSV exports independent of page size | Implemented: sealed revision membership, server aggregation, bounded lossless continuation; MCP and PostgreSQL checks pass |
| Time math | UTC elapsed/break union, midnight/travel/DST folds, no fabricated bounds or period allocation | Pure math, command and aggregate/query integration checked |
| P0b timers | Retry-stable instant, exact preview, start/pause/resume/stop/switch, restart survival; PostgreSQL single-active and races | Implemented/deployed: five commands; exact-image PostgreSQL start/switch/correction races and fresh-process recovery pass |
| P0b lineage | Atomic split/merge preserves known quantities/gaps/attribution; superseded parents excluded; stale races reject affected operation | Implemented/deployed; MCP and exact-image PostgreSQL quantity-growth, mixed-precision, sealed-report and lineage checks pass |
| P1a policies | Effective dates, exact rates/currencies, hourly/nonhourly/unpaid distinction, explicit rounding/period/limits; unknown eligibility/estimates remain partial | Implemented/deployed; native policy/evaluation journeys and exact-image PostgreSQL effective timelines, concurrent edits, frozen evaluations and privacy checks pass |
| P1a formal timesheets | Exact contributions, reporting lifecycle, immutable submitted snapshots, same-purpose duplicate-quantity protection, manual submission observations | Implemented/deployed; native document journeys and exact-image PostgreSQL claim races, conserved amendment and immutable history checks pass |
| P1b claims/invoices | Stable lineage across corrections/splits, partial reservations/concurrent drafts, cancellation release, exact numbered issued snapshots, adjustments/reconciliation flags | Implemented/deployed; native invoice/shared-source journeys and exact-image PostgreSQL partial-allocation races, numbering, issue/refresh/delivery observation and lineage checks pass |
| P1b expenses | Independent exact money/currency/date/attribution/eligibility/ownership, optional protected receipt, submission/approval and distinct reimbursement claim | Implemented/deployed; native expenses and exact-image PostgreSQL import deduplication races, exact totals, frozen approval, owner isolation and report immutability pass |
| P1c settlement | Partial/unmatched/overpaid payments, allocations/credits/reversals, external-ID dedup, gross/net differences and outstanding/due balances without false receipt | Implemented/deployed: five commands/context; native and exact-image PostgreSQL receipt/allocation races, conservation, history and owner isolation pass |
| Formal export | Deterministic CSV/JSON and usable invoice/timesheet rendition with references, policy/calculation/revision basis; no implicit sending | Implemented/deployed; native financial document/invoice journeys exercise JSON/CSV/Markdown and exact-revision continuation |
| P2 operations | Source-grounded workload/capacity/interval unions, outcomes/professional history, explicit assumptions/coverage and suggested omissions without invented performed work | Implemented/deployed; native workload journeys and exact-image PostgreSQL preference races and project reports pass |
| P2 follow-up | Typed work/finance reminder/case/event subjects, bounded exception reconciliation, retained inbox, suppression/preferences and client scheduling | Implemented/deployed; native late-once delivery/review/preference journeys and exact-image PostgreSQL owner/history/concurrent-review checks pass |
| Optional visibility | Explicit correlated actual-time Calendar projection, no duplicate editable time/accounting or default provider effect | Implemented/deployed; native unknown-delivery/refresh/hide journeys and exact-image PostgreSQL atomic intent race and nonblocking source invalidation pass |
| Authority/provenance | OAuth/current local grant, req_/call_, owner isolation/revocation, full approved automation, restricted triage, optional honest capture and sensitive-log protection | Native admission/revocation/context isolation/report-cursor checks pass; every new command family has read-only denial coverage. Exact-image PostgreSQL compensation/expense/settlement privacy and owner checks pass; real provider/business records were not smoke fixtures |
| Contracts/host | Actual schemas and budgets, first-valid-attempt MCP journeys, generated reference/skill, same registered plugin identity and actual advertised capabilities | Source/runtime v67 advertise 108 tools; all 37 added definitions meet budgets. Same private plugin 2.1.0 read-back matches source and preserves App configuration. Operator confirms fresh-chat work context; four successful v67 server calls corroborate acceptance |
| Storage/operations | Fresh/populated PostgreSQL migrations, constraints/locking/races, protected history and migration-specific recovery | Exact tested image passes fresh migration/model parity, empty round-trip, populated constraints/races/history and retained downgrade refusal; supported deployment upgrades production to 20261105a3b4 without business backfill |
| Release | Local gate/affected groups, clean-source tested image, reviewed PR/merge, synchronized source, drain/idle/backup/migration/postdeploy and host evidence | PR 64 merged with 17 commits retained; tested-image validation and supported deploy pass. Receipt pins source/image/backup/schema. Hosted package published/read back; fresh-chat work context is accepted with matching successful server calls |

## Implementation evidence

### C0: catalogue consolidation

Implementation checkpoint: `ce6edbdbfe6ef461304edf8ba98da58e966c1112`.

The native catalogue now advertises 71 tools and retains routes for all 47 baseline
mutation variants. Shared retraction uses the existing typed business planner and
canonical handlers. Old retraction/completion/conflict aliases remain registered
with original permission/retry behavior outside discovery. Context actions and the
generated reference/capability disposition use the surviving tools.

- `scripts/docket check`: 192 native cases passed in 17.05 seconds; Ruff and strict
  mypy passed for 193 source files. Includes twelve domain-plan equivalence cases
  and real MCP retraction/retry/cached-alias behavior without provider intent.
- `scripts/docket check --extended storage`: 123 cases passed in 18.17 seconds,
  including existing mutation, occurrence, typed registry and migration behavior;
  Ruff/strict mypy passed. These fast fixtures do not replace PostgreSQL evidence.
- Actual source discovery: 105,588 bytes, 68,574 input-schema bytes, largest input
  3,508 bytes; retraction input 612 bytes; global instructions 1,643 bytes. The
  measurement uses synthetic settings and compact UTF-8 JSON, not a production call.
- The release's PostgreSQL native smoke now includes shared retraction/retry and
  task completion through the advertised update; execution is pending release
  validation. No deployment or hosted-plugin refresh is claimed yet.

Work, finance and intelligence requirements above remain outstanding. Synthetic
fixtures never create real work sessions, financial claims, payments or unrelated
provider effects.

### Typed storage and time foundation

Migration `20261105a1b2` adds Engagement/WorkSession heads, immutable typed revisions
and normalized segments, with registered `eng_`/`erev_`/`work_`/`wrev_` references.
No old schedule is converted into performed work. Reported compensation class,
event/place links and work mode remain optional, independently typed columns.
An engagement can retain an unknown arrangement instead of inventing employment
or contracting terms. One primary open timer per operator has a partial unique
index; actual timer transition commands are still pending.

The performed-time normalizer uses exact integer microseconds, independent supplied
offsets and an IANA reporting zone. It preserves incomplete quantity as unknown,
accepts duration-only reports without bounds, rejects ambiguous/nonexistent naive
times and date-only clock bounds, and subtracts overlapping breaks once.

- `scripts/docket check`: 200 cases passed in 17.47 seconds, with Ruff and strict
  mypy passing for 195 source files. The compact native gate includes eight
  performed-time cases covering folds, travel/midnight, break union, incomplete
  reports, duration precision and public-field errors.
- Focused time/migration checks: 11 passed in 1.84 seconds, including clean SQLite
  upgrade/downgrade and model/table coverage. SQLite does not substitute for the
  PostgreSQL checks below.
- `scripts/docket check --extended storage`: 123 cases passed in 18.85 seconds
  against the new model/migration baseline, with Ruff and strict mypy passing.
- `scripts/docket check --extended release`: 52 cases passed in 1.92 seconds;
  shell syntax, Ruff and strict mypy passed. Tested-image validation and actual
  deployment are still pending; these unit checks are not a release receipt.
- `scripts/work-postgres-check.py` passed on disposable PostgreSQL 16.9: new-table
  columns/types/nullability match models; empty migration downgrade/upgrade works;
  direct SQL cannot update/delete retained revisions/segments or delete their heads;
  synchronized transactions cannot create two primary timers; populated downgrade
  refuses to discard history. Fixture provenance uses a real synthetic authenticated
  request/ChangeSet, not invented utterance roots. This is storage evidence, not
  completed native work-session or timer-command acceptance.
- The extended tested-image smoke now creates its own `docket_work_smoke` database
  and runs these checks. The standalone local rehearsal used only loopback test
  databases without production mounts or credentials.

Recovery: the migration is additive and contains no semantic backfill. Retain the
pre-migration backup and matching runtime. Image rollback alone does not reverse
schema. Downgrade refuses populated work tables; preserve their history and use a
forward repair or separately authorized backup restoration when necessary. Never
delete work rows to make a rollback/release check pass.

No work-session commands, finance capabilities, production migrations or host
refresh were claimed by that foundation checkpoint. The subsequent slice below
connects canonical effects, provenance/history/context and flat recording commands.

### Native recording and corrections

Five new tools are implemented in source: work context, log/update work, and
create/update engagement. They compile typed effects into the existing immutable
ChangeSet/proposal engine. Corrections append revisions and retain earlier time,
attribution and request roots; context holds optimistic versions. PostgreSQL row
locks recheck the head version before any revision/head update.

Work can be reported with interval/break inputs, positive minutes, or unknown
quantity. An unresolved arrangement may remain a label. Reported billability,
event/place links and location/mode remain independent optional fields. Recording
work creates no support Item, calendar Event or provider operation. Supplied bounds
replace prior unbounded date/period placement internally; ordinary corrections do
not require the client to reconstruct that cleanup. Explicit conflicting inputs
still receive business-field errors.

Context supports engagement-label search and selected work/attribution references.
It exposes compact state, revision references, coverage and allowed actions; exact
past revisions are readable through the existing history tool. Cached old aliases,
existing schemas and full approved native automation authority are preserved.

- Actual MCP journeys cover first-attempt recording, DST/breaks, duration-only and
  unknown time, billability, engagement correction, exact preview/apply, unchanged
  retry, sparse null clearing, stale-version rejection, logical retraction, old
  revision inspection, public-field errors and read-only write rejection.
- Native gate: 202 cases passed in 18.80 seconds, with Ruff and strict mypy.
  Storage cases: 123 passed in 19.52
  seconds after preserving configured-owner scope for internal historical readers.
  The final Ruff and strict mypy checks passed after a test-line formatting fix.
  These measurements are local synthetic fixtures, not a release receipt.
- Disposable PostgreSQL 16.9 verified canonical duration logging/retry and two
  simultaneous exact corrections: one commits, the other rejects on version
  preconditions, with exactly one new retained revision. Fresh/empty migration
  round-trip, direct-SQL immutability, primary-timer uniqueness and refusal to
  downgrade retained work history also passed.

Actual source discovery has 76 tools (the consolidated 71 plus these five), with
routes for 51 canonical effects (the original 47 plus four typed work/engagement
effects). Compact UTF-8 discovery is 118,040 bytes; input schemas total 78,629
bytes. The largest existing/new input remains 3,508 bytes, and global instructions
remain 1,643 bytes. The five new complete definitions total 12,383 bytes:

| New tool | Input bytes | Complete definition bytes |
| --- | ---: | ---: |
| Work context | 295 | 703 |
| Create engagement | 1,380 | 1,805 |
| Update engagement | 1,531 | 1,961 |
| Log work | 3,281 | 3,852 |
| Update work | 3,494 | 4,062 |

These are actual `PluginMCP.list_tools()` definitions using synthetic configuration,
including OAuth metadata, annotations and output schemas, measured as compact JSON.
The native transport tests enforce the same per-tool and gross phase budgets.

At that checkpoint, aggregate queries/exports and later milestones were still
pending. The subsequent query slice is recorded below. No production migration,
tested-image acceptance or hosted-plugin refresh was claimed by that checkpoint.

### Full-set work queries and informal exports

The query/export slice adds `docket_query_work_sessions`, completing the six P0a
native tool routes in source. Default summaries group by engagement, organization,
project, local day or local week. Integer-microsecond quantities remain exact;
hours are display-only. Known reported quantities, time allocable to the query
period, unallocated quantities, unique timed coverage, overlaps, unknown quantity
and open-timer as-of estimates are independently visible. An empty history proves
neither inactivity nor complete coverage. Period-only work is never evenly spread
across days. A matching session with only break time in the selected window remains
visible with zero active time.

Migration `20261105b2c3` adds derived, owner/principal-bound report snapshots and
exact work/engagement revision membership. One SQL INSERT/SELECT captures the
visible revision set; PostgreSQL seals it against later additions or modification.
Later corrections or new matching sessions do not enter continuation pages.
The source window's UTC bounds, IANA reporting zone, cutoff and calculation version
are retained. Unsupported calculation versions reject rather than silently rebase
an old report. Reports are read-derived artifacts, not canonical financial claims
or new authority roots.

PostgreSQL computes totals and interval unions over the full retained set; bounded
detail pages never determine the total. Daily/week allocation and union calculations
run in SQL, including multi-year reads. SQLite uses equivalent deterministic test
functions for its fast fixtures; PostgreSQL remains the storage/math evidence.
Normal summaries and explicit JSON/CSV exports share the same basis and totals.
Exports stream content chunks, including lossless fragments of large records,
within the 16 KiB serialized response limit. report_cursor restarts that same basis
in another format. No export creates a submission, claim or provider intent.

Verification:

- Three real MCP journeys verify the synthetic 12.5 + 8.0 + 6.5 = 27.0-hour week,
  full totals across pages, corrections after capture, JSON/CSV agreement, optional
  classification/location fields, DST/midnight/break unions, unplaced and partial
  period quantities, zero-active coverage, read-only access, principal isolation,
  OAuth revocation and long Unicode/quoted/multiline records without truncation.
- Native gate: 205 cases passed in 21.51 seconds with Ruff and strict mypy.
  Storage gate: 123 cases passed in 19.64 seconds with Ruff and strict mypy.
  Subsequent focused report journeys/static checks also pass.
- Disposable PostgreSQL 16.9 verifies both additive migrations and model parity,
  empty downgrade/upgrade, actual command/query/export execution, day/week and
  organization/project totals, a ten-year query, overlap unions, running estimates,
  zero-active matching records, sealed membership, immutable rows, correction/timer
  races and refusal to discard populated retained history. It uses no production
  mounts, credentials or provider effects. The same rehearsal is wired into the
  extended tested-image smoke; tested-image execution remains a release step.
- Actual compact MCP discovery: 77 tools and 120,036 bytes. Query input is 1,360
  bytes; its complete definition is 1,995 bytes. All six P0a definitions total
  14,378 bytes, below the 20 KiB budget. The largest new input remains 3,494 bytes.
  Instructions and surviving write schemas have not grown.

Recovery remains additive. Retain the pre-migration backup and matching runtime;
image rollback does not reverse schema. Populated work report tables cannot be
downgraded by discarding retained membership. Use a forward repair or separately
authorized restoration if necessary.

At the query checkpoint, timers and later milestones were still pending. The
subsequent timer slice is recorded below. This source evidence does not establish
activation of SPEC 1.1 or completion of the full delta.

### Durable primary timers

Five named flat tools now start, pause, resume, stop and switch the primary timer.
Default effective time and timezone come from the immutable authenticated request,
so retries and delayed preview application do not recapture now. Previews include
request/effective time and proposed timer state; a preview changes no timer. An
explicitly reported past stop is accepted through the ordinary end field and keeps
reported provenance. It cannot silently become a future stop or precede the retained
current segment. Outcomes do not automatically complete linked tasks.

The canonical engine has typed start/transition/switch effects. Switch performs the
old stop and new start under one effect/transaction and one effective instant.
PostgreSQL advisory locking plus the existing partial unique index enforce one
primary open timer, including a paused one. Another start returns the existing
reference and a useful context/switch action. Mutable heads and immutable revisions/
segments persist transitions; no background process closes unattended timers.
Metadata edits remain available while active. Explicit retraction abandons a timer
with its unknown end retained, freeing the primary slot without fabricating work.
An abandoned record can later be explicitly corrected into reported work with a
known end or quantity; its earlier retracted revision and unknown end remain genuine.

Compact work state/time summaries are retained in canonical commit receipts,
including their as-of basis for running estimates. Retries recover those same
receipts even after subsequent timer changes. The existing recording tables/index
already support this state; no additional timer migration or authority grant is
introduced. Split/merge lineage is still a separate pending requirement.

Evidence:

- Native gate: 208 cases passed in 22.80 seconds with Ruff and strict mypy.
  Storage gate: 123 cases passed in 19.61 seconds with static checks. Focused
  timer journeys also pass after the reported-end and receipt refinements.
- Actual MCP journeys prove preview isolation, admitted-time equality, pause/resume
  exclusion, atomic switch, unchanged receipt recovery, metadata edits, stale
  proposal rejection, explicit abandonment without an invented end, and reported
  zero-duration stopping. A DST fold case verifies independent offsets and elapsed
  UTC quantities through pause/resume/stop.
- Disposable PostgreSQL 16.9 proves concurrent native starts produce one primary
  timer and a typed existing-timer result, concurrent exact switch previews produce
  one atomic replacement and one version rejection, and old end equals new start.
  A separate Python process recovers the running state without auto-closing it.
  Existing schema round-trip, report/export, immutable-history, correction-race,
  unique-index and retained-downgrade checks also pass. Only synthetic databases
  and credentials are used; the containers are removed after verification.

Current source discovery has 82 tools and 54 canonical effect variants, including
all 47 original effects. The eleven implemented work definitions total 22,663
compact UTF-8 bytes, below the full P0 32 KiB budget. Timer inputs are 1,982 bytes
(start), 553 (pause/resume), 804 (stop) and 2,171 (switch); each is below 4 KiB and
each full definition below 6 KiB. Discovery totals 128,326 bytes. P0a's six-tool
budget and the unchanged global instruction budget remain enforced separately.

At the timer checkpoint, lineage, finance/intelligence and release work remained
pending. Subsequent source progress follows; source evidence never substitutes for
activation.

### P0b: known-quantity split/merge and stable work-unit identity

Two flat commands accept homogeneous time parts or selected work references.
They compile to typed effects inside the existing exact-preview/ChangeSet path,
lock observed parents, supersede them and create linked children atomically.
Replacement records appear first in the bounded receipt, including a 25-parent
merge; count/truncation fields describe omitted parent summaries. Work context
links direct parents/children and advertises actions only on editable leaves.

Interval splits inherit original breaks and conserve every active instant. Merging
separated intervals records the gap as excluded time for that work session, never
global inactivity. Overlapping active intervals require an affected-record correction
or retraction. Duration-only splits conserve exact quantities; merges currently
require the same reported date/period and reporting timezone. Timers must be stopped.
Mixed/incomplete time and differently placed duration reports remain useful as
separate records; composite lineage remains an explicit unfinished P0 requirement.

Stable private origin coordinates retain units across revisions. A 90-to-120-minute
correction adds 30 new minutes; shrinking and regrowing reuses earlier coordinates.
Children inherit disjoint spans, merges retain their origins, and concurrent growth
allocates separate tails under locks. Ambiguous growth after retired lineage is
marked for later financial review without blocking the work correction. Adjacent
spans compact internally; clients never construct coordinates, revision graphs or
claim allocations. This is a foundation for P1, not implemented financial claims.

Migration `20261105c3d4` adds quantity origins, immutable allocations/bases and
lineage edges. PostgreSQL enforces append-only origins, one-way sealing, exact
revision quantity at sealing, owned/nonoverlapping coordinates per basis and
immutable allocations/edges. No semantic migration backfill occurs. An authorized
mutation may initialize missing derived quantity mappings for retained work history.
The prior work revisions and genuine roots remain unchanged.

Verification at this source checkpoint:

- Final native gate: 211 passed in 26.29 seconds; Ruff and strict mypy passed.
  Includes compact context, replacement receipts and review-marked growth after
  retired quantities. Five focused MCP cases also passed in 4.40 seconds.
  Storage: 123 passed in 20.32 seconds with static checks.
- Disposable PostgreSQL 16.9 verifies migration/model parity and empty round-trip,
  one winner for concurrent exact split proposals, concurrent child growth with
  disjoint units, atomic merge, original retained totals, sealing/immutability and
  populated-downgrade refusal. A fresh final rehearsal also rejects presealed
  inserts and sealing a basis whose allocated quantity differs from its revision.
  Release tooling: 52 cases passed in 2.13 seconds with static checks. Disposable
  rehearsal containers are removed after verification.
- Real transport checks include duration corrections, timed breaks/gaps, stale
  exact proposals, unchanged retries and a 25-session context under 8 KiB.
- Actual discovery: 84 tools, all 47 baseline effects plus nine work/engagement
  effect variants. Thirteen P0 definitions total 28,292 compact UTF-8 bytes, within
  32 KiB. Split input is 1,975 bytes; merge is 2,622 bytes. All work inputs remain
  below 4 KiB and complete definitions below 6 KiB. Discovery totals 133,957 bytes;
  the 1,643-byte global instructions remain unchanged. These are source measurements,
  not deployed inventories or token estimates.

Recovery: retain the pre-migration backup and prior image. Populated quantity/lineage
tables refuse downgrade; prefer a compatible forward repair. An image rollback does
not reverse this migration or establish compatibility with newly retained work.
Never delete work/evidence or reset the database to make rollback succeed.

At this checkpoint composite lineage, finance/intelligence and release remained
pending. The next section records subsequent source progress. No production rows,
OAuth grants, finance/provider effects or hosted publication changed here.

### P0b: mixed precision and nested composition

Mixed intervals, duration-only reports with different date placement, and incomplete
reports now merge without discarding known quantities, dates, offsets or uncertainty.
A composite work revision references sealed immutable source periods internally.
Nested composition retains a finite source graph; the connected agent does not
construct that graph. Each split part selects an ordinary source_work_ref from work
context, optionally dividing its known bounds/minutes. Per-source and aggregate
conservation reject duplicate/lost quantities or incomplete reports with a public
parts error. Parents remain superseded in current totals and readable in history.

Unknown total quantity stays null/omitted; known_duration_us is a partial subtotal.
Current reports expand exact time contributions in SQL, filter each source to the
selected period and count matching work independently of contribution count. Timed
coverage, dated quantities, open estimates and unquantified reports remain distinct.
An unknown component cannot erase known hours or become zero completed work.
Normal work context reads at most five source details with explicit more coverage;
explicit report exports include all components and original source provenance.

Report calculation/export v2 adds component detail to JSON/CSV. Retained v1 reports
remain readable and their CSV shape/content stay unchanged. Immutable source refs,
dates and versions pin both formats through later corrections and split/merge.
Metadata-only composite corrections reuse the source basis. A deliberate complete
time correction can replace that reported time while retaining earlier history.

Migration 20261105d4e5 adds immutable work_time_components and extends the precision
constraint. PostgreSQL requires owned, recorded, previously sealed source revisions,
blocks late inserts and history edits, and checks known/unknown quantities at sealing.
There is no source-data backfill. Populated composite history refuses downgrade;
use compatible forward repair and retained backups rather than deleting history.

Evidence:

- Final native gate: 212 passed in 28.53 seconds, Ruff and strict mypy passed.
  Storage: 123 passed in 21.31 seconds; release tooling: 52 passed in 2.21 seconds,
  both with static checks. Nine focused work/report/contract cases also passed.
- Disposable PostgreSQL 16.9 passes fresh migration/model parity and empty rollback,
  nested mixed-time merge, period-specific totals, preserved unknown contributions,
  immutable/sealed composition, split recovery, existing timer/quantity races and
  populated-downgrade refusal. Synthetic containers are removed after verification.
- Real MCP verifies 60 timed + 90 duration-only minutes alongside an unknown report;
  narrower reads exclude the other dates, JSON/CSV agree, corrections and nested
  merge/split preserve quantities, and an earlier v1 CSV remains byte-identical.
- Source discovery remains 84 tools / 56 typed effects. The thirteen work definitions
  total 28,228 bytes; discovery is 133,893 bytes. Split input is 2,168 bytes, merge
  2,351 bytes, largest new input 3,494 bytes. Merge derives placement rather than
  accepting redundant date overrides; explicit corrections use work update.
  Per-tool 4/6 KiB and full-P0 32 KiB
  budgets remain satisfied. The 1,643-byte global instructions are unchanged.

Core P0 behavior is implemented in source. P1a–P1c, P2, full release acceptance,
tested-image validation, reviewed publication/merge, deployment and same hosted
plugin refresh remain unfinished. This checkpoint adds no finance/provider effect
or production-data change, and does not complete the full amendment.

### P1a: effective compensation and reporting configuration

Two small native commands set effective pay terms and reporting rules under an
owned engagement. The commands have one uniform target and required effective date;
they avoid conditional create/update envelopes. They compile to typed engagement
effects through the existing request/preview/ChangeSet path. The engagement version
guards concurrency and policy streams retain their own immutable revisions.

Pay terms distinguish hourly rates, salary/fixed-fee amounts, unpaid/unknown basis,
explicit currency/payer, eligibility assertions and monetary precision. Reported
WorkSession classification and original time stay unchanged. Reporting rules retain
explicit period anchors, week boundaries, rounding scope/mode, configured hour limits,
overtime threshold/multiplier and optional employer grouping. No legal entitlement,
student limit, payer, exchange rate or nonhourly earnings is inferred from a name.

Sparse edits apply to every existing window within the requested range. Changing
only currency/payer preserves differing future rates; bounded corrections resume
prior terms outside the range. A gap uses unknown/default terms rather than copying
an expired rate. Effective windows use explicit UTC boundaries with reporting timezone.
Unchanged terms outside the edit reuse their stable references. Earlier snapshots
remain genuine and immutable. Configuration is not compensation evaluation or a claim.

Migration 20261105e5f6 adds policy heads, sealed revisions, immutable terms and ordered
revision membership. Decimal quantities are canonical strings, avoiding binary-float
storage. PostgreSQL protects owned stable heads, completed sealed revisions, ordered
nonoverlapping windows, source-preserving clipped terms, exact numeric syntax, and
immutable/closed history. No historical or provider data is backfilled. Populated
policy history refuses downgrade; retain the backup and use compatible forward repair.

Work context derives short policy views; explicit history audit reads expose lossless
policy_json_chunk continuation over a selected immutable revision. Owner filters apply
to context, detail and history lists. No new OAuth scope or owner reapproval is needed.

Evidence:

- Native gate: 215 passed in 31.67 seconds with Ruff/strict mypy. Storage: 123 passed
  in 20.69 seconds with static checks. Release checks: 52 passed in 2.11 seconds.
  Final focused checks: 5 passed in 3.62 seconds. Three new MCP journeys cover effective/bounded
  and sparse corrections, expiry gaps, exact preview/retry/stale handling, policy
  history continuation, unchanged work/classification and read-only rejection.
- Disposable PostgreSQL 16.9 passes migration/model parity and empty rollback,
  effective and sparse timeline changes, concurrent exact proposals with one winner,
  invalid-money rejection, late/history edits, incomplete-head and skipped-revision rejection,
  overlap protection, other-owner context/audit denial and invocation privacy.
  Existing work/timer/report concurrency and retained-downgrade checks still pass.
- Source discovery: 86 tools / 58 typed effects, 139,358 bytes. Pay input is 2,048
  bytes / definition 2,576; reporting input 2,354 / definition 2,882. Both are below
  4/6 KiB limits; no operation-discriminator union is exposed. Global instructions
  remain 1,643 bytes. The original thirteen work tools remain within their P0 budget.

This slice implements policy configuration. P1a evaluation and formal timesheets,
the shared claim/reservation mechanism, P1b/P1c, P2 and full activation remain
unfinished. No real rates, employers, work, claims or payments were seeded; no
production data, provider state, OAuth grant or hosted release changed here.

### P1a: deterministic compensation evaluation

`docket_get_compensation` accepts an optional engagement, date range, timezone and
component, with summary/JSON/CSV output and bounded continuation. Natural-language
interpretation remains in the connected agent. The service captures exact work and
policy revisions, derives employer-wide context internally, then evaluates the
requested subset. Later work/rate corrections do not change the retained result.
Explicit as-of reads select historical revisions rather than today's policy head.

The calculator preserves integer microseconds and rational monetary arithmetic until
the configured monetary rounding boundary. It splits actual active segments at rate,
rule and civil-day boundaries without changing work history. Configured rounding
sums the applicable segment/session/day/period quantities before rounding; rate
boundaries do not independently round each fragment. Rounded quantities are allocated
proportionally with exact largest-remainder microseconds, without inventing clock
bounds. Overtime uses configured day/week thresholds and multipliers; reported dates
establish inter-day order, while ambiguous within-day allocation at different rates
remains unresolved. Explicit shared-payer rules group applicable engagements. Currency
and precision buckets remain separate; there is no exchange-rate, tax or legal inference.

Hourly eligibility, reported classification and payment remain distinct. Missing
rates/currency/eligibility, open timers, unplaced/cross-period quantities and policy
changes within an imprecise report yield partial results with coverage flags. Configured
salary/fixed-fee amounts do not become hourly earnings. Overlaps, configured limits,
engagement-date issues and lineage review remain visible. No estimate creates a claim,
timesheet submission, invoice, payment or provider effect.

Migration 20261105f6a7 adds immutable owner/principal-bound derived calculation
snapshots, with genuine retained work-report and policy references. PostgreSQL and
ORM guards reject rewriting/deleting these snapshots. A populated downgrade refuses
to discard them; retain compatible source/backup and use a forward repair.

Evidence:

- Final native gate: 228 passed in 32.34 seconds; Ruff and strict mypy pass for
  214 source files. Storage: 123 passed in 20.27 seconds. Release tooling: 52 passed
  in 2.24 seconds. Focused calculation/MCP journeys: 13 passed in 2.67 seconds.
- Disposable PostgreSQL 16.9 passes full migration/model parity and empty round-trip,
  actual MCP exact-money evaluation, immutable snapshots, cross-owner and same-owner/
  different-principal continuation denial, retained downgrade refusal, and all earlier
  work/timer/lineage/report/policy concurrency checks. The disposable container was removed.
- Real MCP checks exercise direct first-call evaluation, stale-independent retained
  pages, explicit historical/as-of calculation, rate/work corrections, read-only
  access and lossless JSON/CSV exports over multiple byte-limited responses. Normal
  detail pages shrink to the result budget while full-set totals remain available.
- Actual discovery: 87 tools, 58 typed mutation effects, 140,883 bytes. Evaluation
  input is 900 bytes / complete definition 1,524 bytes. Pay and reporting definitions
  remain 2,576 / 2,882 bytes. No new public action union, graph or internal UUID is
  required; shared instructions remain 1,643 bytes.

Formal timesheets and shared claim/reservation protection are next. P1b/P1c, P2,
optional actual-time visibility, complete release acceptance, reviewed publication,
deployment and the same hosted plugin refresh remain unfinished. These are source
checks, not production activation or a completed full amendment.

### P1a: formal timesheets and shared work claims

Three flat commands prepare, refresh and update timesheets; one document read serves
summary/JSON/CSV/Markdown. Current-period preparation derives explicit reporting
anchors/week boundaries, while explicit bounds remain available. Receipts return the
document, known time, status and relevant gaps directly. Optional preview reserves
nothing. Canonical drafts reserve raw work units; refreshing replaces reservations
atomically and retains earlier document/claim transitions. Missing rates remain
unknown rather than preventing valid report preparation or manufacturing zero pay.

The shared claim ledger uses conserved work-origin coordinates and explicit economic
components. Changing an engagement label/reference does not make the same base units
claimable twice. PostgreSQL serializes reservations on origins and rejects overlapping
active claims. Already reported units can be referenced by a later report without
creating another entitlement. A 90-minute reported session becoming 120 creates only
30 newly reservable minutes. Cancellation releases draft reservations with retained
events; split/merge/correction lineage keeps current availability coherent.

Timesheet verification is optional before recording a valid reported manual
submission. Submission/approval/rejection create retained revisions; the submitted
snapshot stays immutable. Draft refresh requires current versions. Source/rate
changes produce reconciliation flags, without rewriting earlier formal reports.
The comparison uses applicable values/coverage rather than term IDs, so a future
rate change does not flag an unaffected historical report. Newly configured terms
surface as changed coverage. Configured employer grouping with an unknown payer
leaves the affected rounding/overtime amount unresolved.
Missing/unplaced time and relevant overlap/lineage issues affect finalization of the
report, while ordinary work recording and unrelated commands continue. None of these
operations submits externally, records payment or creates Calendar/provider effects.

Migration 20261105a7b8 adds typed financial document heads, immutable revisions,
work claims and retained claim transitions. Deferred PostgreSQL guards require a
matching revision/event for each head version, forbid late claim insertion outside
the retained draft membership, protect source quantities and exact monetary fractions,
and preserve submitted content. No existing data is backfilled. Populated downgrade
refuses to discard documents/claims; retain compatible source and backup and use a
forward repair. Image rollback does not reverse this schema.

Bounded work context derives reserved/reported/issued/available quantities; engagement
context links recent documents and owner-filtered history covers older drafts. Exact
revision JSON/CSV/Markdown exports pin immutable work descriptions, quantities, policy
and calculation basis. UTF-8 continuation preserves entire records across chunks.
Previous-revision links and bounded claim audit chunks retain navigable history.
Reported approval remains independent of payment. Invoice issuance/settlement remain
future work over this same ledger.

Evidence:

- Native gate: 232 passed in 35.18 seconds, with Ruff and strict mypy for 219 source
  files. Storage: 123 passed in 21.69 seconds. Release tooling: 52 passed in 2.53
  seconds. Focused journeys: 3 passed in 3.31 seconds, including configured
  week boundaries, authority rejection, stale proposals, reported approval, UTF-8
  JSON/CSV/Markdown exports, immutable submitted content and split availability.
- Disposable PostgreSQL 16.9 passes migration/model parity and empty round-trip,
  concurrent prepared drafts with one reservation winner, 90→120-minute conserved
  amendment, immutable submitted revisions/claim quantities/events, required transition
  evidence, cross-owner read denial and canonical same-owner reads from another
  connection. Additional checks verify current 90-minute reported/30-minute available
  quantities and compact durable financial receipts. Containers were removed.
- Actual discovery: 91 tools / 61 typed mutation effects, 146,128 bytes. Prepare input
  is 1,101 bytes / definition 1,680; update 846 / 1,399; refresh 563 / 1,124; document
  read 429 / 969. All fit 4/6 KiB limits. No client allocation graph, UUID, family-wide
  action union or new OAuth scope is required. Global instructions stay 1,643 bytes.

P1b invoices/expenses, P1c settlement, P2 workload/follow-up, optional actual-time
visibility and full release/host activation remain unfinished. These source checks
create no real employer/work/payment data and do not complete the full amendment.

### P1b: reported expenses and retained reimbursement observations

Three flat tools now record, correct and query/export reported expenses. Recording
requires amount, currency and description, without a forced engagement, date,
receipt, rate or financial document. Optional links use existing organizations,
projects, engagements and protected source records. Unavailable receipt capture
retains a gap instead of blocking the cost. Similar costs suggest duplicates;
only an exact external source/transaction identity deduplicates reliably.

A cost revision and a reported submission/approval/rejection are separate immutable
records. Corrections preserve the earlier approval's exact cost basis, amount and
currency, and expose a reconciliation flag. A conflicting imported decision asks
for correction of the existing expense rather than silently recording approval.
Unknown decision dates, incurred dates and reimbursement amounts remain unknown.
No received reimbursement, provider submission or paid travel time is inferred.

The query captures owned revisions and observations, then seals its exact currency
aggregates. Full-set totals distinguish dated and unplaced costs, known eligible
amounts, reported claims and reported approvals, including unknown-amount counts.
Currencies page independently from detail records. JSON/CSV exports retain the
captured basis after later corrections and continue within UTF-8 records without
losing fields or duplicating expense rows. Earlier approval currency remains
separate when the current cost currency changes. Export is an informal retained
report, not an issued claim or proof of settlement.

Migration `20261105b8c9` adds five tables, stable `exp_` heads, immutable `exrev_`
costs and `exobs_` observations, plus principal-bound `exrep_` report membership.
PostgreSQL guards owned/versioned heads, complete revision history, exact money,
observation basis and one-way report sealing. Populated downgrade refuses before
dropping anything. Recovery is roll-forward under the retained history; an image
rollback does not remove the schema. No historical schedule/cost backfill occurs.

Verification for this source slice:

- Native gate: 235 passed in 40.25 seconds; Ruff and strict mypy passed for 223
  source files. The focused expense journeys also pass after the final sparse-input
  and large multibyte export checks.
- Storage: 123 passed in 21.77 seconds. Release tooling: 52 passed in 2.19 seconds.
- Disposable PostgreSQL 16.9: fresh metadata parity and empty schema round trip;
  concurrent exact-ID imports create one cost; competing prepared corrections have
  one winner; approval snapshots retain earlier amounts; exact nine-place currency
  sums; protected ownership/history, deferred complete revisions, sealed membership,
  cross-principal continuation rejection and populated downgrade refusal. The broad
  existing work/timer/policy/timesheet proofs also passed. Containers were removed.
- Actual discovery: 94 tools / 63 canonical mutation variants, 155,959 compact
  UTF-8 bytes. Expense record input/definition: 3,380/3,983 bytes; update:
  3,310/3,889; query: 1,336/1,901. Each stays below 4/6 KiB. Global instructions
  remain 1,643 bytes. Generated reference, context actions and packaged skill align.

This finishes the reported-cost/observation slice, not all of P1b. Contractor
invoice allocation/issuance, settlement/adjustments, operational intelligence and
actual-time visibility still require implementation and release acceptance. No
real expense, financial claim, provider effect or hosted-plugin change was made.


### P1b: shared-source invoice allocation and numbering foundation

Invoice-purpose allocations now reference the existing conserved WorkClaim source,
with expense sources supported by the same document-allocation mechanism. This
adds document purpose and frozen pricing to the shared source basis; it does not
create another independent work entitlement or require the client to construct a
claim graph. Stable source coordinates prevent corrected or split work from
becoming newly billable. A timesheet may still reference that work without
multiplying the economic quantity.

The deterministic pricing kernel retains raw/reportable time, marginal overtime,
exact rational prices and explicit unknowns. It prices a selected source range
instead of prorating an entire session's average hourly price. Expense quantities
use exact nanounits, including amounts too large for signed 64-bit nanounits.
Currency conversion is not implied. Repricing an expense in another currency
requires reconciliation with any earlier active claim.

Migration `20261105c9d0` adds document allocations/events and retained invoice
numbers with locked per-owner series. Reservations, issue and release retain
immutable transition evidence. PostgreSQL protects source ownership/attribution,
range bounds, overlap exclusion, exact draft membership and immutable quotes.
Issuance requires numbered, priced, single-currency allocations and evaluated
billability; affected expenses require their current eligible basis and any
configured receipt. Issuing neither submits an invoice nor records payment.
Populated downgrade refuses before dropping tables or guards; use roll-forward
recovery. No historical claims or invoice numbers are synthesized.

Verification:

- Native gate: 240 passed in 38.72 seconds; Ruff and strict mypy passed for 226
  source files. Includes exact partial-overtime/rounding tests and a synthetic
  shared-claim reservation, issue, cancellation and number-reuse journey.
- Storage: 123 passed in 21.94 seconds; release tooling: 52 in 2.33 seconds.
- PostgreSQL 16.9: fresh metadata parity/empty round trip, one winner for contested
  partial invoice quantities, concurrent distinct sequential numbers, unchanged
  shared timesheet source, exclusion of already issued quantities, immutable quote/
  transition/number history and populated downgrade refusal. The broader existing
  work/policy/timesheet/expense proofs also pass. Disposable containers were removed.
- The native catalogue remains 94 tools / 63 mutation effects. The owned history
  read gains allocation detail; no placeholder invoice commands are advertised.
  Actual discovery is 155,981 compact UTF-8 bytes; largest input remains 3,508
  bytes and global instructions 1,643. Contract metadata is v62.

This is internal service/storage evidence. Native invoice preparation, refresh,
issuance, usable invoice exports and financial settlement remain unfinished,
alongside P2, optional actual-time visibility and release/host activation. The
source services are not evidence that the connected plugin can invoice yet.


### P1b: native invoice commands and immutable renditions

Three flat commands now prepare, refresh and update invoices. Preparation takes an
engagement plus optional period, selected work/expenses, a maximum work quantity,
and ordinary invoice terms. The default period includes earlier related dated
costs and work through today; it does not silently omit an old uninvoiced cost
just because there is no current-month work. Selected owned work/amendment links
are validated internally instead of demanding redundant context reads. The calling
agent does not construct claims, allocation graphs or normalized generic entries.

Drafts reserve exact remaining work and expense source units. Noncanonical previews
reserve nothing and pin their financial basis: another claimant makes a preview
stale rather than changing which hours it will bill. Cancelling a draft releases
reservations with history. Submitted timesheets can share the same conserved work
source with invoices without creating another WorkClaim. Draft reservations remain
exclusive until their owner is resolved or cancelled. Work context distinguishes
invoice-reserved, invoiced and uninvoiced quantities independently from reporting.

Prices use effective retained policy, exact marginal overtime and document-level
rounding. A maximum work quantity selects only that quantity, preserving the
remainder for another invoice. Currencies, explicit monetary precision and known
payers are kept distinct. Unresolved money/billability, affected overlap/review,
required receipts or reported expense rejection block the affected invoice's
issuance, without blocking valid work or expense recording.

Issuance pins a unique series number, client/issuer, due/terms, exact source and
calculation basis. Retry returns the same receipt/number. Submitted/issued bodies
remain immutable after work, rate or expense corrections and expose reconciliation
flags. Refresh replaces only a draft's reservations. A later amendment invoices
only newly available units; financial credits/reversals remain P1c work.

The shared document reader renders immutable invoice JSON, CSV and usable Markdown
with source/revision identifiers, exact calculation basis, terms and rounded total.
A reported manual delivery is a separate immutable `fobs_` observation; unknown
actual delivery time remains unknown. It creates neither provider success nor
money received. Migration `20261105d0e1` protects that exact issued-revision link,
reported-rejection/precision checks and expense currency reconciliation. Populated
downgrade refuses before dropping history. Recovery remains roll-forward.

Concurrency verification found a retained-report foreign-key lock interaction.
Immutable head writers now use PostgreSQL `FOR NO KEY UPDATE`: competing writers
still serialize, while retained report key-share locks no longer prevent a work
correction. Preview source capture takes no canonical quantity locks; actual
application validates versions and the exact financial basis under locks. A
concurrent join returning fewer captured work heads is rejected rather than
silently omitting the missing version check.

Evidence for this source slice:

- Native gate: 244 passed in 46.04 seconds, Ruff and strict mypy for 230 source
  files. Storage: 123 in 21.67 seconds. Release tooling: 52 in 2.15 seconds.
- Real MCP journeys verify $80 regular / $120 overtime partial invoices from one
  two-hour timesheet source, an additional $10 expense, number retry, cancellation,
  stale previews, unavailable receipt/rate handling, draft refresh and unchanged
  issued exports after work correction/delivery. Earlier dated expense defaults,
  read-only rejection and actual 4/6 KiB schema budgets are checked.
- Fresh PostgreSQL 16.9 rehearsal: migration/model parity and empty round trip;
  invoice preview race has one winner; refresh, numbered issuance/retry and manual
  delivery retain history; a later work extension exposes only its 30-minute
  remainder. A correction completes while retained report/preview capture remains
  open. Existing timer, lineage, policy, timesheet, expense and allocation races,
  immutable history/numbering, and populated downgrade refusal also pass.
- Actual discovery: 97 tools / 66 canonical mutation variants, 163,380 compact
  UTF-8 bytes. Invoice prepare input/definition: 2,420/3,074 bytes; refresh:
  2,213/2,785; update: 910/1,504. All fit 4/6 KiB. Global instructions remain
  1,643 bytes. Contract metadata v63, context actions, generated reference and
  packaged skill are aligned. No new scope, UUID or client claim graph is required.

This is source acceptance, not production/host activation. No real invoice,
expense, payment or provider message was created. P1c settlement/adjustments, P2
workload/follow-up, optional actual-time visibility and the full release/host
refresh remain unfinished parts of the adopted objective.


### P1c: receipts, shared settlement and financial context

Five flat operations now record actual receipts, apply received money, adjust a
claim, reverse bookkeeping and read financial context. Recording a receipt can
match its target in the same command. The agent supplies ordinary references and
amounts; Docket derives source allocation, current balances and retained ledger
entries. Existing OAuth/local-owner checks, authenticated request/call attribution,
exact previews, retries and the canonical ChangeSet transaction are reused. No
financial scope, foreground attestation or capture requirement was introduced.

One source capacity is shared across timesheets, invoices and expense claims.
Exact matching stays within each recorded target's source range, including when a
whole-timesheet receipt and a narrower invoice receipt overlap. Credits/deductions
remain separate from cash. A credit releases excess allocations as retained
reversal entries, leaving money unapplied without inventing a refund or moving it
to an unrelated claim. Partial receipt reversal first releases sufficient cash
allocation. Unknown deductions never silently clear the net/gross difference.

Payments retain reported/imported origin, optional dates/evidence and stable
external identities. Missing optional evidence does not block receipt recording.
A corrected receipt is fully reversed and replaced; the external identity follows
its linear replacement chain rather than creating duplicate cash. Mutable payment
heads and affected financial versions are checked under locks. Exact financial
previews reject changed bases. Receipt balances are explicitly transaction
snapshots; financial context supplies current balances.

Financial context distinguishes work claims, reimbursement claims, classified
receipts, applied cash, explicit deductions, credits, outstanding and unapplied
money by currency. Shared document balances are not summed twice. Unknown dates,
unmatched allocations and source-change flags remain visible. Current explicit
document reads compare retained basis with source heads; broad/as-of reads label
their narrower reconciliation coverage. Frozen JSON/CSV reports are private to
owner and principal, retain exact basis across continuations and remain bounded.
PostgreSQL capture uses repeatable-read isolation across its source/ledger queries.

Migration `20261105e1f2` adds payments, immutable transactions/entries/events and
private financial reports. PostgreSQL enforces exact positive money/currency,
receipt limits, bounded reversals, owned source ranges, complete version events,
and immutable transaction entry membership. A later append cannot alter an old
transaction. Empty downgrade round-trips; populated downgrade refuses before
dropping any history. Preserve the prior image/backup and use forward repair;
this slice authorizes no production row edits, reset or restore.

Evidence:

- Native gate: 253 passed in 54.76 seconds; Ruff and strict mypy passed for 238
  source files. Storage: 123 in 23.68 seconds; release tooling: 52 in 2.67 seconds.
- Real MCP journeys cover shared claims, partial/excess receipts, external-ID
  retries, credits, reversals, gross/net gaps, explicit withholding, stale context,
  nanounit reimbursements, corrected cost/approval basis, replacement identity,
  missing optional archives, frozen exports and read-only rejection.
- Disposable PostgreSQL 16.9: fresh migration/model parity and empty round trip;
  concurrent imports yield one receipt; contested payment previews have one
  winner; credits/reversals conserve money. Direct SQL cannot mutate receipts,
  omit head events, exceed reversals, cross currencies or append to retained
  transactions. Principal-bound reports and owner isolation hold. The broader
  work/timer/policy/claim/invoice rehearsals also pass. A populated financial read
  additionally verifies actual repeatable-read isolation and the 8 KiB context cap.
- Source discovery: 102 tools / 67 canonical mutation variants, 172,557 compact
  UTF-8 bytes. Settlement input/complete-definition bytes: record payment
  2,336/2,941; apply 1,374/1,953; adjust 986/1,568; reverse 835/1,401; context
  627/1,257. All fit 4/6 KiB. Global instructions remain 1,643 bytes. Metadata v64,
  generated reference, context actions and packaged skill agree.

These are local source checks, not a tested-image or deployment receipt. The
same deployed plugin remains on the prior release. P2 workload/follow-up,
optional actual-time visibility, complete release validation and host refresh
remain required. P3 still depends on a separately selected concrete provider.


### P2: source-grounded workload and reported planning preferences

Three flat tools read workload context and set availability/protected windows or
optional project hour budgets. Settings use existing versioned canonical Preferences,
request attribution and exact ChangeSets. They create no calendar/provider effects.
Missing schedules stay unknown; no standard workday is invented. Capacity uses UTC
interval unions, explicit travel buffers and local clock policies, preserving DST
and date-line gaps. Past work totals remain separate from future capacity.

The packet combines canonical commitments, covered cached provider observations,
due tasks, current reported settings, configured reporting limits, full-set actual
work totals and outcomes, shared-source outstanding money and exact project-week
budgets. Recurring occurrences retain their identities. Linked work can resolve
one candidate occurrence without hiding other occurrences. Calendar candidates
never create attendance, work quantities or compensation. Overlapping meetings
consume capacity once; unplaced duration and all-day constraints remain explicit.
Read capture is repeatable-read on PostgreSQL and normal packets fit 8 KiB.

The new definitions measure 556–2,113 input bytes and 1,151–2,727 complete-definition
bytes; discovery is 105 tools / 177,885 compact UTF-8 bytes. Metadata v65 and the
generated reference agree. Human-readable hours accompany exact microseconds.
Native journeys check sparse/stale settings, provider deduplication, interval unions,
unknown capacity, project totals/outcomes, candidate occurrence review and absence
of unintended work/provider writes. Disposable PostgreSQL checks singleton create
and version races, exact native settings and retained project reports. The storage
and release groups pass. These are source checks, not deployment acceptance.

Typed operational reminders/cases, bounded exception publication, optional actual
Calendar visibility, full release validation and hosted refresh remain required.


### P2: selective operational follow-up

Two flat commands schedule an exact work/engagement/document/expense/payment
follow-up or reconcile a bounded period into nonbinding attention cases. Reminder
creation reuses ReminderPlan and its existing queue/inbox; its explicit retained
trigger needs no synthetic Calendar event. Retraction cancels the plan. Due reminders
remain deliverable after downtime and emit once. Labels and subject ownership are
validated, with no payroll, Calendar write or financial transfer.

Reconciliation discovers source refs internally and retains its as-of range,
versions, coverage and exact exception set under a genuine request/ChangeSet.
It checks actual interval overlaps, optional classification gaps, overdue tracked
obligations and posted money, changed document bases, approaching explicit follow-ups
and overlapping future commitments. The open-timer diagnostic is explicitly a
12-hour heuristic, never an inferred stop. Scans and exception counts are bounded;
partial coverage is visible. No work, compensation, attendance or payment is inferred.

Existing supporting decision CaseItems represent these reviews without new blocking
canonical consequences. Corrections append case revisions and new item payloads;
earlier source snapshots remain intact. Stable situation identities, retained inbox
notices and a default weekly cooldown suppress repeated alerts. Current notification
preferences can further restrict publication at apply time. Suppression never blocks
ordinary work. The client explicitly schedules reconciliation under its approved
write delegation; no inference daemon or extra background authority was added.

`docket.work.attention` and `docket.finance.attention` use the existing event protocol
and generic ref/summary payload. Read the referenced case for facts; event delivery
neither grants write authority nor proves the operator read it. No new OAuth scope.
Migration `20261105f2a3` adds immutable operational trigger/review records and explicit
ReminderPlan subjects. PostgreSQL checks owner matching, exact queue delivery and
retained trigger identity; populated downgrade refuses before dropping history.
Use prior image/backup and forward repair, never edit production rows to bypass it.

Discovery is 107 tools / 69 canonical variants and 180,811 compact UTF-8 bytes.
Follow-up schemas are 841/851 bytes, complete definitions 1,415/1,504 bytes; metadata
v66, generated reference and packaged skill are aligned. New native journeys cover
late delivery/retries/retraction, deduplication, retained case items, current quiet
preferences, stale exact previews and read-only rejection. PostgreSQL additionally
checks competing reviews, cross-owner rejection and direct-SQL immutability.
Native local gate: 262 passed in 54.89 seconds; Ruff and strict mypy passed for
246 source files. The storage group passed 122 checks before its explicit migration
inventory assertion was updated; all three migration checks then passed. Disposable
PostgreSQL completed fresh/model-parity/empty-round-trip checks, reminder owner and
immutability constraints, concurrent review/deduplication, all earlier work/finance
races and populated downgrade refusal. No real provider/work/financial records were
created. Optional actual-time Calendar visibility and full release/host activation remain.


### Optional actual-time visibility and compact entry guidance

`docket_show_work_session` takes only the selected work and Calendar destination
plus the standard command fields. Docket derives the Event, scoped routing and
atomic provider intent. Stable wcal_ identity links the retained work revision to
its display; immutable wcrv_ revisions retain subsequent refresh/hide/invalidation.
Snapshot titles identify the source revision and net hours. UTC rendering preserves
exact instants across DST/travel, while notes retain source timezone and explain
breaks. Unbounded, incomplete or composite work never invents a display interval.

Ordinary logging has no Calendar effect. Source corrections commit immediately and
invalidate enabled display metadata in the same transaction. A stale snapshot is
refreshable through the show command when standard provider evidence is ready;
provider readiness gates only that display operation. Retraction or split/merge can
leave a historical snapshot marked for hiding, never a duplicated accounting row.
Shared `docket_retract_record` dispatches wcal_ hiding to exact Event cancellation.
Managed Events cannot be independently edited, and canonical/cached actual displays
are excluded from workload commitments. Work totals still count only work lineage.
SPEC ADR 009, the adopted delta and forward index record this boundary together.

Migration `20261105a3b4` adds typed projection heads and immutable revisions.
PostgreSQL enforces source ownership, exact retained head versions, immutable
identity/history and source invalidation at commit. A populated downgrade refuses;
retain prior image/backup and use forward repair. No production reset/row editing.

Native journeys cover unknown create/cancel outcomes, original-effect recovery,
source correction while Google is unresolved, exact preview rejection, refresh of
the same Event, readonly managed displays, split lineage, hiding and unchanged work
accounting. Disposable PostgreSQL verifies one winning concurrent display with one
Event/provider intent, rollback of the competing proposal, nonblocking source
invalidation, immutable history and complete version/owner guards. All earlier
work/finance/follow-up rehearsals and the retained downgrade refusal pass.

Evidence: native 265 passed in 57.59 seconds; storage 123 in 22.31 seconds; provider
77 in 4.68 seconds. Ruff and strict mypy passed for 249 checked source files.
Discovery is 108 tools / 72 canonical variants, 182,165 compact UTF-8 bytes.
The show input is 643 bytes and complete definition 1,350 bytes. Shared retraction
adds only the closed wcal_ prefix: 617-byte input with the same five fields.
Metadata v67 and generated contract/disposition align. Global MCP instructions
remain 1,643 bytes. The entry skill shrinks from 17,195 to 4,441 bytes, routing
work, finance, operations, triage and advanced commands to on-demand reference files.

C0–P2 and optional visibility are now implemented and checked in source. Complete
clean-source tested-image validation, publication/merge/deployment and the same
hosted-plugin refresh remain required. No real Calendar/work/financial records were
created for verification. P3 still requires its separate concrete provider choice.


## November 6 activation

[PR 64](https://github.com/nkuhanas/Docket/pull/64) is merged by linear fast-forward;
all 17 implementation commits and their hashes remain. Clean synchronized main at
`3f8c23b412c307ec3172a97c00ae11000d900748` was deployed using immutable image
`sha256:6c3bc754c7c12ea1cfcdc00710371d746a4bebd18929f0b92071ba25a7284b4f`.
The tracked source tree is `4729a22ebf9bb2aa5f9080e9b3c8fcce4ad7dc6b`.
Source-bound validation took 203.41 seconds: 425 selected native/release/storage
checks, static checks, exact-image native startup and disposable PostgreSQL proof.
The affected provider group separately passed 77 checks.

The supported deploy passed predeploy, drain, backup, schema upgrade, health and
native-boundary checks. Database head is `20261105a3b4`; the prior image and backup
remain. Public OAuth discovery returns 200 with all four existing scope names;
anonymous MCP returns 401. Runtime introspection confirms v67 and 108 definitions.
The existing ChatGPT connection reads the new overview with read/write/triage.
Its current token does not include events; inbox reads/reconciliation remain useful,
while event subscriptions need that separate existing grant.

The same private USER-scoped plugin is now
[2.1.0](https://chatgpt.com/plugins/plugin_asdk_app_6aebd82ba1108191a77747be377965f8),
release `pluginrel_6aed8a3a0cc881918fff5ef23158ae83`. App identity, connection,
audience, presentation and prompts are unchanged. Both manifests preserve everything
except version. Read-back matches all seven hosted skill/reference/contract files
to source and retains the App binding. No replacement plugin or new OAuth scope.

Initial activation left this development conversation on the earlier 84-tool
declarations. Fresh ChatGPT work context is now accepted as recorded below.
The [machine-readable activation receipt](../evidence/work-attribution-release/activation.json)
distinguishes host evidence from deployed backend/package acceptance.
The [final source measurement](../evidence/work-attribution-release/contracts.json)
confirms 37 additions after the 13-tool consolidation. The full tools/list result
is 182,175 bytes; its array alone is the earlier reported 182,165 bytes. Largest
new input/complete definition is 3,494/4,062 bytes. These are byte measurements,
not token counts or evidence that a 108-tool catalogue is small.

No real work, Calendar or financial fixture was created in production. P3 remains
a separate concrete provider choice. Earlier pending markers in this report are
historical source checkpoints; this activation and the host acceptance below are
the current release state.

## November 6 host acceptance

The Operator reports “Work context loads” in a fresh ChatGPT chat. Retained server
history independently contains four completed, successful `docket_get_work_context`
calls under `docket-tools-2026-11-05-v67`, from 17:45:02 through 17:47:18 UTC.
The [host receipt](../evidence/work-attribution-release/host-acceptance.json) retains
only call references, contract identity, timestamps and success states. It excludes
arguments, result contents, source bodies and transcripts. No synthetic business
record or provider effect was created for this acceptance check.

The earlier fresh-chat-only instruction omitted the App's discovery scan step.
[Official OpenAI guidance](https://developers.openai.com/plugins/deploy/submission#scan-your-latest-changes)
requires rescanning after MCP changes: existing plugin → MCPs → connected server
→ Issues → Rescan, then check tool availability and invoke the relevant read.
Publishing the skill does not establish that its App-owned discovery has refreshed.
The package/App identity and OAuth grants stay intact; no replacement plugin or
new permission is needed for work and finance.

C0–P2 and optional revision-pinned Calendar visibility are implemented, locally
validated, merged, deployed and host-accepted. P3 remains the separately scoped
provider selection/integration described in the adopted delta. This release has
no remaining acceptance item requiring Operator action.
