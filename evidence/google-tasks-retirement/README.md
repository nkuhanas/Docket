# Google Tasks and NocoDB retirement

Implementation of the adopted [SPEC 1.2 delta](../../deltas/2026-11-06T17-59-26Z-google-tasks-and-nocodb-retirement.md)
is in progress. This directory records local evidence, not production activation.
The existing deployment and hosted plugin have not been replaced by these slices.

## Current source

- The Task and required Item preserve full 1,024-character titles. Explicit derived
  title ownership survives imports and renames and can be relinquished independently.
- Google Tasks transport and OAuth scope upgrades are implemented. Production
  mutations remain subject to real, account-scoped G0 method validation.
- Selected-list policies retain their genuine authenticated configuration request.
  Deterministic imports use a typed policy/observation root through the shared
  ChangeSet engine; they do not manufacture fresh operator utterances or requests.
- Immutable observations, binding revisions, per-field conflicts and resolutions
  support independent edits, explicit linking and detachment. Disabled or revoked
  policies cannot admit new canonical imports. Missing/inaccessible lists do not
  imply task deletion.
- The worker has resumable discovery and list polling with leases, pagination,
  overlapping incremental windows, periodic full inventories and bounded backoff.
  Full inventory coverage is required before advancing a watermark. A child whose
  parent appears on a later page remains observed until a subsequent scan can link it.
- The six flat native workflows are registered. Task creation and completion use
  the existing create/update tools. The fifteen former hidden aliases are removed
  from registration and dispatch. Historical receipts and tool-call names remain.
- Calendar rendering emits and reconciles explicit private visibility. Timed
  marker endpoints use elapsed UTC duration and independent offsets across DST.
  Live Google rejects backwards local wall-clock ranges despite exact offsets;
  those markers use UTC transport with their canonical timezone retained. Real
  private/free popup-zero creation, read-back, privacy-change detection and
  cleanup pass at both DST boundaries; device display remains unobserved.
- Reminder origins derive from scoped retained generation/operation identity,
  survive full cache rebuilds and exclude markers from workload accounting.
  Copied labels or private properties alone confer no origin. Automatic outbound
  generation, rescheduling and completion cancellation use that same identity.
- Requested times appear as AM/PM headers in Google Tasks. Canonical Task/Item
  titles remain clean; scoped transport normalization preserves Google-side
  renames. Full titles are not truncated to make a header fit.

The [contract measurements](contracts.json) record 114 advertised tools. All six
new definitions plus both modified task definitions total 12,245 bytes; each input
is below 4 KiB and each definition below 6 KiB. These are local-source measurements.


The shared runner now executes Tasks operations using its existing claims, attempts,
leases and recovery. Task state and provider intents commit together; newer unsent
intents can supersede old work, and independent fields remain separate. A stale
conditional write retries its original named field only after fresh reconciliation.
Unknown insert recovery pages the full selected list before accepting a unique
corroborated match. Removed markers remain uncertain; an explicit native counterpart
selection preserves that original uncertainty while allowing current work to continue.
Read-reconciliation interruption never returns an uncertain insert to execution.
A task GET 404 requires accessible-collection confirmation before deletion is confirmed.

## Verification

Focused native MCP journeys cover flat list configuration, task creation/query/
completion, explicit linking, separate field resolutions and detachment. The
storage group covers 151 checks and the provider group covers 119 checks, including
polling lease recovery, observation-only policies and revoked standing authority.
Ruff and strict mypy passed. Release validation and activation remain outstanding.

The [isolated PostgreSQL proof](postgres.json) verifies storage shape, genuine
policy imports, full Task/Item titles, deferred retained-baseline constraints and
competing native edits with exactly one commit, and shared-runner delivery with
retained binding revisions. The disposable container was removed.

The [reminder rehearsal](reminders.json) adds real PostgreSQL atomic reminder
delivery, competing replacement with one commit, time-header normalization and
completion cancellation. It also verifies fresh/empty migration roundtrips at
`20261106f6a7`, correct projection discriminator width and safe populated narrowing
refusal. This is isolated-provider evidence, not deployed Google/device acceptance.

## Retirement inventory

[The sanitized inventory](inventory.json) records the stopped evaluation containers
and their dedicated volumes. Three protected encrypted volume archives were
authenticated, decrypted and inspected using isolated disposable copies before
retirement. The production database was not modified.

Nine historical Tasks and eleven Items have identities absent from the current
canonical database. Their private content remains in protected recovery artifacts;
it has not been printed, committed or automatically imported. The Operator
subsequently classified that data as unnecessary and authorized disposal without
import. [The retirement receipt](retirement.json) records removal and verified
absence of all six owned containers, four exclusive volumes and two networks.
Production Docket/PostgreSQL remained healthy. Protected encrypted archives stay
as recovery evidence, with no callable legacy runtime.

## Remaining milestones

- Shared-operation task writes and scoped recovery now pass native/fake and PostgreSQL
  checks. Complete hierarchy/moves, live acceptance and operational readiness reporting.
  A canonical commit remains distinct from provider delivery.
- Complete inbound Calendar helper clock/deletion reconciliation. Automatic outbound
  generations, cancellation and pending/claimed/unknown-create windows are implemented.
- Consent and Tasks API enablement are complete. [Real provider checks](live-provider.json)
  pass on the Operator-authorized dedicated validation targets, including stale-write
  rejection and native cross-list identity. The native-clock experiment lost its
  UI time; the Operator selected AM/PM title headers plus Calendar alerts. Complete
  hierarchy evidence and production method admission; device display is independent.
- Complete release cold-start verification of legacy retirement, release validation, reviewed merge/deployment,
  existing-app action rescan and real task verification. Device popup observation is
  separate from API creation success and is not guaranteed by Docket.
