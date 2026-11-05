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
| Conditional consolidation | Rich person/org/Item facets and bounded detail remain reachable; exact recurrence/provider cancellation parity | Pending equivalence review |
| P0a domain | Typed owner/versioned Engagement and WorkSession, immutable revisions and normalized segments; additive migration; no forced Item/Event/payer/rate | Storage/time foundation checked; canonical handler and MCP integration pending |
| P0a reporting | Flat interval, duration-only and incomplete recording, sparse audit-preserving correction/retraction; classification/event/mode/location; field-level validation | Pending |
| P0a queries/export | Full-set daily/weekly/engagement/project totals, unique/overlap/unplaced/unknown coverage, stable bounded JSON/CSV exports independent of page size | Pending |
| Time math | UTC elapsed/break union, midnight/travel/DST folds, no fabricated bounds or period allocation | Pure normalization verified; command/query integration pending |
| P0b timers | Retry-stable instant, exact preview, start/pause/resume/stop/switch, restart survival; PostgreSQL single-active and races | Pending |
| P0b lineage | Atomic split/merge preserves known quantities/gaps/attribution; superseded parents excluded; stale races reject affected operation | Pending |
| P1a policies | Effective dates, exact rates/currencies, hourly/nonhourly/unpaid distinction, explicit rounding/period/limits; unknown eligibility/estimates remain partial | Pending |
| P1a formal timesheets | Exact contributions, reporting lifecycle, immutable submitted snapshots, same-purpose duplicate-quantity protection, manual submission observations | Pending |
| P1b claims/invoices | Stable lineage across corrections/splits, partial reservations/concurrent drafts, cancellation release, exact numbered issued snapshots, adjustments/reconciliation flags | Pending |
| P1b expenses | Independent exact money/currency/date/attribution/eligibility/ownership, optional protected receipt, submission/approval and distinct reimbursement claim | Pending |
| P1c settlement | Partial/unmatched/overpaid payments, allocations/credits/reversals, external-ID dedup, gross/net differences and outstanding/due balances without false receipt | Pending |
| Formal export | Deterministic CSV/JSON and usable invoice/timesheet rendition with references, policy/calculation/revision basis; no implicit sending | Pending |
| P2 operations | Source-grounded workload/capacity/interval unions, outcomes/professional history, explicit assumptions/coverage and suggested omissions without invented performed work | Pending |
| P2 follow-up | Typed work/finance reminder/case/event subjects, bounded exception reconciliation, retained inbox, suppression/preferences and client scheduling | Pending |
| Optional visibility | Explicit correlated actual-time Calendar projection, no duplicate editable time/accounting or default provider effect | Pending |
| Authority/provenance | OAuth/current local grant, req_/call_, owner isolation/revocation, full approved automation, restricted triage, optional honest capture and sensitive-log protection | Pending new-domain evidence |
| Contracts/host | Actual schemas and budgets, first-valid-attempt MCP journeys, generated reference/skill, same registered plugin identity and actual advertised capabilities | Pending |
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
refresh are claimed by this foundation checkpoint. The next slice must connect
typed canonical effects, provenance/history/context and flat logging/correction/
query/export tools through the existing atomic engine.
