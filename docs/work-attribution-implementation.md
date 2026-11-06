# Work attribution and personal operations implementation

Requirements: [SPEC 1.1](../SPEC.md), adopted from the
[November 5 delta](../deltas/2026-11-05T21-18-26Z-work-attribution-and-personal-operations.md).
Starting source: `3a578859265431ac0270ab1acb62b48bfb8d6717`.
Status: implementation in progress; the work-attribution release is not deployed.
Design probes and catalogue projections are not implementation acceptance evidence.

## Scope and completion tracking

The full objective includes C0, P0a/P0b, P1a/P1b/P1c and P2. A completed foundation
does not complete the finance or operational-intelligence requirements. Conditional
wrapper retirements require equivalent behavior, not a target catalogue count.
P3 explicitly requires a separately chosen concrete provider delta; this release
does not invent a payroll/banking integration or require its credentials.

| Requirement | Required evidence | Current state |
| --- | --- | --- |
| Adoption | Single current SPEC, ADR/history and delta index reconciled | Adopted in working source; not activation |
| C0 catalogue | Shared retraction through existing typed handlers; completion/context aliases; all baseline effects; no larger surviving inputs; cached-client permission/retry preservation | Implemented in source; native/storage checks pass; PostgreSQL/release pending |
| Conditional consolidation | Rich person/org/Item facets and bounded detail remain reachable; exact recurrence/provider cancellation parity | Retain these four wrappers: generic context lacks the rich facets, and update/cancel use distinct canonical effects; no mandatory count target |
| P0a domain | Typed owner/versioned Engagement and WorkSession, immutable revisions and normalized segments; additive migration; no forced Item/Event/payer/rate | Six native recording/context/query tools implemented and checked; tested-image and release acceptance pending |
| P0a reporting | Flat interval, duration-only and incomplete recording, sparse audit-preserving correction/retraction; classification/event/mode/location; field-level validation | Recording, correction and informal reporting paths checked; full release acceptance pending |
| P0a queries/export | Full-set daily/weekly/engagement/project totals, unique/overlap/unplaced/unknown coverage, stable bounded JSON/CSV exports independent of page size | Implemented: sealed revision membership, server aggregation, bounded lossless continuation; MCP and PostgreSQL checks pass |
| Time math | UTC elapsed/break union, midnight/travel/DST folds, no fabricated bounds or period allocation | Pure math, command and aggregate/query integration checked |
| P0b timers | Retry-stable instant, exact preview, start/pause/resume/stop/switch, restart survival; PostgreSQL single-active and races | Five named commands implemented; native/PostgreSQL races and fresh-process recovery checked; release acceptance pending |
| P0b lineage | Atomic split/merge preserves known quantities/gaps/attribution; superseded parents excluded; stale races reject affected operation | Interval/duration and mixed/incomplete/differently dated composition implemented; MCP/PostgreSQL checks pass. Full release acceptance pending |
| P1a policies | Effective dates, exact rates/currencies, hourly/nonhourly/unpaid distinction, explicit rounding/period/limits; unknown eligibility/estimates remain partial | Typed pay/reporting timeline configuration and bounded history implemented and checked; evaluation/rounding/limit calculations remain pending |
| P1a formal timesheets | Exact contributions, reporting lifecycle, immutable submitted snapshots, same-purpose duplicate-quantity protection, manual submission observations | Pending |
| P1b claims/invoices | Stable lineage across corrections/splits, partial reservations/concurrent drafts, cancellation release, exact numbered issued snapshots, adjustments/reconciliation flags | Pending |
| P1b expenses | Independent exact money/currency/date/attribution/eligibility/ownership, optional protected receipt, submission/approval and distinct reimbursement claim | Pending |
| P1c settlement | Partial/unmatched/overpaid payments, allocations/credits/reversals, external-ID dedup, gross/net differences and outstanding/due balances without false receipt | Pending |
| Formal export | Deterministic CSV/JSON and usable invoice/timesheet rendition with references, policy/calculation/revision basis; no implicit sending | Pending |
| P2 operations | Source-grounded workload/capacity/interval unions, outcomes/professional history, explicit assumptions/coverage and suggested omissions without invented performed work | Pending |
| P2 follow-up | Typed work/finance reminder/case/event subjects, bounded exception reconciliation, retained inbox, suppression/preferences and client scheduling | Pending |
| Optional visibility | Explicit correlated actual-time Calendar projection, no duplicate editable time/accounting or default provider effect | Pending |
| Authority/provenance | OAuth/current local grant, req_/call_, owner isolation/revocation, full approved automation, restricted triage, optional honest capture and sensitive-log protection | New commands enforce write admission and retain req_/call_ roots; read-only rejection checked; complete new-domain isolation/revocation/privacy acceptance pending |
| Contracts/host | Actual schemas and budgets, first-valid-attempt MCP journeys, generated reference/skill, same registered plugin identity and actual advertised capabilities | Thirteen P0 definitions measured within budgets; generated reference/skill aligned. Hosted refresh/release pending |
| Storage/operations | Fresh/populated PostgreSQL migrations, constraints/locking/races, protected history and migration-specific recovery | Initial work schema round-trip/immutability/uniqueness verified on PostgreSQL; full release pending |
| Release | Local gate/affected groups, clean-source tested image, reviewed PR/merge, synchronized source, drain/idle/backup/migration/postdeploy and host evidence | Pending |

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
