# Google Tasks and NocoDB retirement implementation

Requirements: [SPEC 1.2](../SPEC.md), adopted from the
[November 6 delta](../deltas/2026-11-06T17-59-26Z-google-tasks-and-nocodb-retirement.md).
Implementation starts at `d0bb86ce4330bb9d1febbc2686e4e43791deffca` (runtime
source baseline `8488f165efc100436813f1bebda7d014dcd5846c`).
Status: adopted; implementation in progress. Deployed behavior remains SPEC 1.1
until source-bound release validation and activation. No real provider/device
acceptance is claimed by a fixture or disabled capability.

## Scope and acceptance tracking

| Requirement | Evidence required | State |
| --- | --- | --- |
| Adoption | One current SPEC, decisions/history and index reconciled | SPEC 1.2 adopted in working source |
| R0 inventory | Exact ownership/status of NocoDB containers, networks, volumes, startup/access and unique data | Six dedicated evaluation containers, four exclusive volumes (including Redis), and two exclusive networks inventoried and removed under explicit Operator instruction |
| R0 protected data | Consistent encrypted archive, checksums/recovery, schema/count/identity comparison and unique-data disposition | Three authenticated encrypted archives verified; isolated restores inventoried. Operator classified the legacy data as unnecessary and authorized disposal without import; protected archives retained |
| G0 OAuth/readiness | Tasks scope profiles preserve granted scopes; atomic verified replacement and isolated feature readiness | Tasks consent completed, Calendar grants preserved and Tasks API enabled. Local context readiness and safe SERVICE_DISABLED diagnosis implemented |
| G0 adapter | Real/fake/disabled Tasks read/write protocols, exact field/marker handling and guarded method dispatch | Transport/pagination, exact-version writes, unknown outcomes and scoped notes implemented with synthetic tests; shared-runner dispatch implemented; live validation pending |
| G0 live capabilities | Method-specific controlled insert/patch/move/delete/date/time/uncertainty evidence | Real insert/patch/move/delete, date set/preserve/null, full limits, completion/reopening and response-discarded correlation checks pass on authorized targets. Native UI clock and hierarchy cases remain; runtime admission/activation pending |
| G1 title path | Full 1,024-character Task and required Item schemas/columns/mutations/context/export, including creation without Item | Full canonical/import/native paths and isolated PostgreSQL proof pass; live Google roundtrip pending |
| G1 title ownership | Typed derived-pair ownership, atomic renames, independent/shared/relinked relinquishment and concurrency | Typed ownership, native rename/relinquishment and concurrent PostgreSQL Task/Item rename pass; further independent-Item race rehearsal remains |
| G1 durable domain | Scheduling/hierarchy, lists/policies, immutable observations, bindings/generations, baselines/conflicts and sync state | Typed storage, retained baselines/provider snapshots, per-field decisions and bounded policy-effect roots implemented; no live activation |
| G1 reads/import | Context/query/export and selected-list import through admitted canonical services with policy/observation roots | Flat native context/query and lossless notes detail, policy imports and explicit linking implemented; synthetic native/PostgreSQL proofs pass |
| G1 polling | Full hidden/completed/deleted pagination, overlap watermarks, resumability/full inventories and coverage | Worker wired; bounded discovery/list paging, leases, overlap/full inventories and revocation tests pass. Later-page parents remain observed for the next scan; live polling pending |
| G2 task writes | Flat create/update/complete/reopen/retract and exact retry/preview, with atomic Tasks intents | Shared Operation/Target/Attempt queue wired; native-to-fake create/edit/complete/reopen/delete, sparse conditional retries and explicit uncertain-creation repair pass. Live G0 remains |
| G2 conflict/lifecycle | Three-way field merge, delete/edit conflict, detachment/disable/list loss without global blocking | Per-field merge, native resolution/detachment, supersession and scoped outbound recovery pass; live cross-interface validation remains |
| G2 markers/limits | Transport-only marker baseline separation, no blind insert retry or silent field truncation | Unknown inserts recover only after complete scoped correlation coverage; missing markers stay uncertain, explicit link parks original uncertainty honestly, and polling defers plausible pending creations. More live limits/hierarchy cases remain |
| G2 reminders | Flat remind_at derives exact owned primary Calendar companion, routing, reschedule/null/completion cancellation and uncertainty | Atomic generations and both provider queues implemented; rescheduling and native/Google completion retire helpers. Pending/claimed/unknown-create windows and PostgreSQL replacement race pass. Inbound Calendar helper edits/deletion remain |
| G2 time display | Visible AM/PM time without relying on Google's opaque native clock | Derived Google title headers implemented; scoped normalization preserves clean Task/Item titles and Google-side renames. Independent reminder dates are labelled; 1,024-character titles remain lossless with an explicit display limitation |
| G2 privacy | Private/free actual request and read-back/reconciliation, existing zero-minute popup reused | Task generation flows serialize private/free popup-zero markers; real adapter read-back/privacy-change checks pass. Inbound helper reconciliation remains |
| G2 workload origin | Validated typed origin survives cache refresh/restart and excludes canonical/cache commitment/capacity accounting | Scoped generation/retained-operation origin survives full cache rebuild and retired/unknown-create generations; workload excludes it before timing/accounting. Copied labels remain ordinary observations |
| G2 DST | UTC start plus 60 elapsed seconds with independent offsets, actual rendering and reconciliation | Task marker compilation and rendering/reconciliation pass autumn and spring transitions, including real UTC transport acceptance |
| G3 hierarchy/planning | Native cross-list/parent/sibling moves, exact identity and constraints; workload/reminder/inbox integration | Live moves carry children/grandchildren with stable identities and no source tombstones. Deletion emits tombstones. Parent completion/deletion cascades without a parent-ETag change on child creation; Operator adopted Google-style subtree semantics (ADR 014). Canonical integration pending |
| Authority/privacy | OAuth/local grant, owner isolation/revocation/triage rejection, genuine service roots, no sensitive invocation payloads | Shared-engine policy import creates no per-poll request/utterance; list/account/local/OAuth revocation, triage rejection and exact-effect admission covered. Native workflows, polling revocation and shared-write runner evidence pass; real provider acceptance remains |
| Contracts | Six compact tools, every modified schema, 4/6/24 KiB and context/result budgets, generated docs/skill | Local v68 source advertises 114 tools; six new plus two changed definitions total 12,245 bytes within 4/6/24 KiB budgets. Bundle skill/host refresh remains |
| Storage | Fresh/populated isolated PostgreSQL migrations, constraints/history/races, restart/drain and recovery | Fresh/empty roundtrip, retained revisions, shared-runner delivery and atomic reminder/competing replacement proofs pass at 20261106f6a7; populated narrowing refuses without data loss. Release-artifact upgrade/restart/drain remains |
| R1 retirement | Unique data migrated/disposed; all dedicated NocoDB runtime/access/startup removed; no fallback/dual write | Exact owned containers, volumes and networks removed and verified absent. Startup/ingress/secret-name/environment checks found only the retained archive key. Production remains healthy; release cold-start verification pending |
| R1 aliases | All fifteen hidden native aliases unregistered without effects; current routes/history still work | Removed from registry/dispatch; surviving native completion/retraction journeys pass; historical records retained |
| Activation | Local gates, tested image, reviewed merge, synchronized main, backup/migration/health and same App/plugin refresh | Pending |
| Intended device | One actual notification observed on the intended device/client, independently recorded from API status | Pending |

## Initial inventory

Current metadata confirms the stopped `docket-workspace-eval` project owns the
evaluation Docket, NocoDB app/worker, metadata PostgreSQL, Redis and canonical
PostgreSQL containers. Its volumes are `canonical-data`, `metadata-data` and
`nocodb-data` under that project prefix. The missing old Compose file is not a
reason to restart the experiment or treat its data as disposable. No row contents
or secrets are printed in this report. Production Docket/PostgreSQL are separate.

## Operational boundaries

Preserve current Google grants and the working token while preparing consent
extension. No Tasks grant is inferred from Calendar access. Provider acceptance
uses expressly designated disposable targets, not real personal tasks as fixtures.
Keep per-method G0 disabled until actual evidence passes; do not call G2/G3 done
with a required advertised method disabled. Preserve genuine history and restore
material without callable legacy aliases. No NocoDB fallback is a recovery path.

## Implementation evidence

The first transport slice adds separate `tasks`/`tasks-read` OAuth profiles without
expanding the default Workspace profile. Reauthorization requests the union with
existing grants and, for Tasks, requires Google's actual granted scopes before
atomically replacing the refresh credential. A partial/missing grant preserves
the old file. Real transport writes require an admitted per-method gate; the
default gate admits no writes. Synthetic tests exercise hidden/completed/deleted
pagination, native cross-list movement, exact ETags, uncertain inserts without
retries, and correlation suffix separation. These are local adapter tests, not
G0 live-provider acceptance.

The canonical storage slice adds Task scheduling/hierarchy/reminder fields,
retained list policies/observations/binding revisions/conflicts/sync cursors and
reminder generations. Deterministic imports use a typed `teff_` admission tied to
one policy and provider observation. The authority engine checks its exact frozen
content and current owner grant, rather than inventing another `req_`/`utt_` per
poll. The ordered migration retains the new evidence and refuses destructive
downgrades once populated. Local schema roundtrips and canonical import tests are
not substitutes for PostgreSQL concurrency or live provider acceptance.

The native catalogue now registers six Tasks workflows and removes fifteen hidden
aliases. [Measured compact runtime schemas](../evidence/google-tasks-retirement/contracts.json)
are 1,665 bytes (create) and 2,180 bytes (update); all six new plus both changed
definitions total 12,245 bytes within the 24 KiB budget. Canonical create/query/
complete, explicit linking, independent field resolution and detachment pass
native MCP journeys. The reminder field now has a locally tested atomic
projection/delivery path; it remains undeployed until inbound reconciliation
and release/host acceptance.

Local tests cover 268 native, 151 storage and 119 provider checks, plus Ruff and
strict mypy. [Isolated PostgreSQL evidence](../evidence/google-tasks-retirement/postgres.json)
covers retained policy imports and baselines, title persistence, competing
native Task edits and shared-runner delivery with retained binding revisions. These checks do not establish live G0 capability or activation.

The [sanitized R0 inventory](../evidence/google-tasks-retirement/inventory.json)
records volume ownership, ciphertext checksums, restoration checks and aggregate
identity comparisons. Private archives and schema/count inventories are retained
under `.runtime/nocodb-retirement/`, with the protected key under `secrets/local/`.
No private rows, transcripts or credentials are in that evidence. The original
volume inventory remains historical evidence. The Operator subsequently
authorized disposal without import. [The retirement receipt](../evidence/google-tasks-retirement/retirement.json)
records removal of the exact six containers, four exclusive volumes and two
exclusive networks, with production Docket/PostgreSQL healthy afterward.
Protected encrypted archives remain; no legacy runtime or fallback was retained.

The Operator authorized disposable Google Tasks and Calendar targets named
`Docket validation` for live acceptance and cleanup. Consent completed after
the setup endpoint update and fresh-link private-browser retry. Tasks API was
then enabled after a sanitized SERVICE_DISABLED diagnosis. Tasks access and the
prior Calendar grants are present; credentials and URLs were not exposed.
The cause of the original browser freeze was not established.

[Real provider checks](../evidence/google-tasks-retirement/live-provider.json)
pass full title/notes limits, date set/unchanged/null, completion/reopening,
stale patch/move/delete rejection, native cross-list identity and deletion.
One actual insert response was deliberately discarded, then uniquely located
through complete scoped marker inspection without another insert. This is a
controlled recovery exercise, not a claim of an uncontrolled network failure.
The controlled native clock did not survive combined API edits. The Operator
selected explicit AM/PM Google title headers with Calendar alerts instead of
further opaque-clock setup. Additional hierarchy behavior, Calendar device
evidence and production method admission remain outstanding. Real Calendar
private/free popup-zero creation, privacy-change reconciliation and cleanup pass
at autumn and spring DST boundaries. Google rejects the backwards autumn local
range despite exact offsets; the timed-marker compiler now uses UTC transport
for that case while retaining the canonical IANA timezone (SPEC ADR 011).

Typed Calendar marker origins derive from the exact scoped reserved provider
identity, immutable reminder generation and retained sent operation/binding.
They survive cache rebuilds even without private Calendar properties and after
generation retirement. Copied labels/private properties cannot establish local
ownership. Four focused cases cover successful/unknown creation across both DST
transitions. Automatic outbound generations now reuse the same shared provider
queue, supersede unsent work, wait for unknown creation outcomes and cancel
retired helpers without resurrecting an old reminder. Google-side Task completion
uses its genuine standing-policy effect to retire the Calendar helper and clear
the managed time header; it does not manufacture a new user request.

[The reminder PostgreSQL rehearsal](../evidence/google-tasks-retirement/reminders.json)
verifies atomic delivery, clean canonical/transport titles, one winning concurrent
replacement and completion cancellation. It exposed an existing 16-character
`ProviderEventBinding.target_kind` column that could not hold the admitted
`temporal_projection` value. Ordered migration `20261106f6a7` widens it safely;
populated narrowing refuses to truncate history. The temporary instance was removed.
Inbound Calendar helper edits/deletion and full release activation remain pending.

Further live probes confirm nested children, native subtree movement and distinct
move/deletion observations. Parent completion/deletion affects children; adding a
child leaves its parent's ETag unchanged. The Operator chose standing Google-style
subtree semantics, including children added since the last read, with no recursive
field or recurring prompt. G3 implementation will scope canonical/provider effects
accordingly. The probe records were removed.

Google truncates explicit completion timestamps to milliseconds. Projection and
comparison now use that supported precision while canonical reported timestamps
remain unchanged. A derived reminder-header edit also leaves independent canonical/
Google title conflicts unresolved. Focused native/transport checks cover both fixes.
