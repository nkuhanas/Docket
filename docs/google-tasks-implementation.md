# Google Tasks and NocoDB retirement implementation

Requirements: [SPEC 1.2](../SPEC.md), adopted from the
[November 6 delta](../deltas/2026-11-06T17-59-26Z-google-tasks-and-nocodb-retirement.md).
Implementation starts at `d0bb86ce4330bb9d1febbc2686e4e43791deffca` (runtime
source baseline `8488f165efc100436813f1bebda7d014dcd5846c`).
Status: deployed from `c7d25789ecfb` with contract v68, 114 tools and plugin 2.2.0.
All source-bound release groups and actual runtime Google write-method admission
pass. Fresh ChatGPT Tasks reads and write journeys pass. Intended-device popup acceptance remains pending;
complete delta acceptance is not claimed. See the [activation receipt](../evidence/google-tasks-retirement/activation.json)
and [same-App read-back](../evidence/google-tasks-retirement/hosted-plugin.json).
Implementation notes below retain earlier checkpoint evidence; this status and the
acceptance matrix describe current readiness.

## Scope and acceptance tracking

| Requirement | Evidence required | State |
| --- | --- | --- |
| Adoption | One current SPEC, decisions/history and index reconciled | SPEC 1.2 adopted and tracked |
| R0 inventory | Exact ownership/status of NocoDB containers, networks, volumes, startup/access and unique data | Six dedicated evaluation containers, four exclusive volumes (including Redis), and two exclusive networks inventoried and removed under explicit Operator instruction |
| R0 protected data | Consistent encrypted archive, checksums/recovery, schema/count/identity comparison and unique-data disposition | Three authenticated encrypted archives verified; isolated restores inventoried. Operator classified the legacy data as unnecessary and authorized disposal without import; protected archives retained |
| G0 OAuth/readiness | Tasks scope profiles preserve granted scopes; atomic verified replacement and isolated feature readiness | Tasks consent completed, Calendar grants preserved and Tasks API enabled. Local context readiness and safe SERVICE_DISABLED diagnosis implemented |
| G0 adapter | Real/fake/disabled Tasks read/write protocols, exact field/marker handling and guarded method dispatch | Deployed shared-runner adapter; real current-grant insert/patch/move/delete method admission passes. Runtime status verifies all four methods; no gate bypass |
| G0 live capabilities | Method-specific controlled insert/patch/move/delete/date/time/uncertainty evidence | Actual runtime probe passes full limits, date set/preserve/clear, completion/reopen, stale writes, stable cross-list identity and discarded-response recovery. Fixtures cleaned. Native clock loss led to accepted AM/PM headers |
| G1 title path | Full 1,024-character Task and required Item schemas/columns/mutations/context/export, including creation without Item | Full canonical/import/native paths and isolated PostgreSQL proof pass; actual Google 1,024-character roundtrip and runtime field-limit admission pass. Actual Google import/rename/retraction and native context preserve the full title; full-title-live.json records scoped service reconciliation |
| G1 title ownership | Typed derived-pair ownership, atomic renames, independent/shared/relinked relinquishment and concurrency | Typed ownership and rename/relinquishment pass native tests and competing PostgreSQL Task/Item edits in the tested release artifact |
| G1 durable domain | Scheduling/hierarchy, lists/policies, immutable observations, bindings/generations, baselines/conflicts and sync state | Typed storage and immutable roots/baselines/conflicts deployed at 20261106a7b8; runtime healthy. Selected-list host acceptance remains |
| G1 reads/import | Context/query/export and selected-list import through admitted canonical services with policy/observation roots | Flat native context/query and lossless notes detail, policy imports and explicit linking implemented; synthetic native/PostgreSQL proofs pass |
| G1 polling | Full hidden/completed/deleted pagination, overlap watermarks, resumability/full inventories and coverage | Deployed bounded discovery/polling with leases, overlap/full inventories, exception reporting and revocation proofs. Lists start disabled until configured through native authority |
| G2 task writes | Flat create/update/complete/reopen/retract and exact retry/preview, with atomic Tasks intents | Tested native/shared-runner create/edit/complete/reopen/delete deployed; all actual method gates pass. Real native create/edit/complete/reopen, subtree moves and retraction pass with confirmed Google delivery |
| G2 conflict/lifecycle | Three-way field merge, delete/edit conflict, detachment/disable/list loss without global blocking | Per-field merge, native resolution/detachment, supersession and scoped outbound recovery pass; live cross-interface validation remains |
| G2 markers/limits | Transport-only marker baseline separation, no blind insert retry or silent field truncation | Actual runtime probe verifies full limits and unique discarded-response correlation recovery with cleanup; ambiguous/lost identities remain uncertain without blind retries |
| G2 reminders | Flat remind_at derives exact owned primary Calendar companion, routing, reschedule/null/completion cancellation and uncertainty | Atomic generations and both provider queues implemented; rescheduling and native/Google completion retire helpers. Pending/claimed/unknown-create windows and PostgreSQL replacement race pass. Owned Calendar clock edits/deletion use exact immutable authority and genuine Calendar observations through the shared ChangeSet engine; revoked grants, copied identities and unsupported all-day edits cannot mutate Tasks. API and device acceptance remain |
| G2 time display | Visible AM/PM time without relying on Google's opaque native clock | Derived Google title headers implemented; scoped normalization preserves clean Task/Item titles and Google-side renames. Independent reminder dates are labelled; 1,024-character titles remain lossless with an explicit display limitation |
| G2 privacy | Private/free actual request and read-back/reconciliation, existing zero-minute popup reused | Task generation flows serialize private/free popup-zero markers; real adapter read-back/privacy-change checks pass. Owned-helper changes resnapshot privacy/free popup-zero projections through scoped canonical effects; live-host/device acceptance remains |
| G2 workload origin | Validated typed origin survives cache refresh/restart and excludes canonical/cache commitment/capacity accounting | Scoped generation/retained-operation origin survives full cache rebuild and retired/unknown-create generations; workload excludes it before timing/accounting. Copied labels remain ordinary observations |
| G2 DST | UTC start plus 60 elapsed seconds with independent offsets, actual rendering and reconciliation | Task marker compilation and rendering/reconciliation pass autumn and spring transitions, including real UTC transport acceptance |
| G3 hierarchy/planning | Native cross-list/parent/sibling moves, exact identity and constraints; workload/reminder/inbox integration | Live moves carry children/grandchildren with stable identities and no source tombstones. Deletion emits tombstones. Parent completion/deletion cascades without a parent-ETag change on child creation; Operator adopted Google-style subtree semantics (ADR 014). Parent-only lifecycle writes, independent child read-back, native/Google stable-ID moves and immediate stale-ETag reconciliation pass native and isolated PostgreSQL checks. Concurrent ancestry moves retain actual mismatch evidence; conflicts, persistent polling/list loss and unsupported reminders use coalesced inbox notices. Bounded sibling read-back can confirm lost move acknowledgements without resending. Runtime method admission passes; same-host hierarchy journeys pass |
| Authority/privacy | OAuth/local grant, owner isolation/revocation/triage rejection, genuine service roots, no sensitive invocation payloads | Shared-engine policy import creates no per-poll request/utterance; list/account/local/OAuth revocation, triage rejection and exact-effect admission covered. Native workflows, polling revocation and shared-write runner evidence pass; real provider acceptance remains |
| Contracts | Six compact tools, every modified schema, 4/6/24 KiB and context/result budgets, generated docs/skill | Deployed catalogue has 114 tools, all six Tasks workflows and no retired alias registration. Eight new/modified definitions total 12,245 bytes. Plugin 2.2.0 read-back and fresh ChatGPT Tasks context pass |
| Storage | Fresh/populated isolated PostgreSQL migrations, constraints/history/races, restart/drain and recovery | Tested immutable artifact passes fresh/empty migrations, retained populated downgrade refusal, atomic reminder/Task races and existing work/finance concurrency. Production drain/backup/upgrade/restart/health pass at 20261106a7b8 |
| R1 retirement | Unique data migrated/disposed; all dedicated NocoDB runtime/access/startup removed; no fallback/dual write | Dedicated NocoDB containers, exclusive volumes/networks and images removed under disposition; encrypted recovery retained. Tested artifact cold startup and healthy production prove no runtime dependency |
| R1 aliases | All fifteen hidden native aliases unregistered without effects; current routes/history still work | Removed from registry/dispatch; surviving native completion/retraction journeys pass; historical records retained |
| Activation | Local gates, tested image, reviewed merge, synchronized main, backup/migration/health and same App/plugin refresh | PR65 merged; clean synchronized main deployed using tested image, durable drain, backup, migration and health checks. Runtime method admission and same-App plugin 2.2.0 read-back pass. Fresh ChatGPT Tasks reads pass; native write journeys pass; device popup pending |
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

### Subtree integration

The standing Google-style subtree choice is now reflected in context, native
previews and bounded commit receipts. Native parent completion/cancellation,
retraction and list moves derive scoped descendant changes under the graph lock,
preserving each Task/Item identity, audit trail and unrelated title/notes. Already
completed children keep their recorded completion time. No recursive input is
added. New subtasks added on Google follow the parent provider action.

One native parent move carries the provider subtree; descendant queue entries
read actual destination resources rather than issuing duplicate moves. A unique
Google-side move between two admitted lists updates the existing canonical Task
and binding generation. Detached, ambiguous or suspended identities do not
become duplicate imports. Rejected stale writes retain an actual fresh read and
re-admit the original immutable intent through the normal policy/conflict checks.
A source GET 404 remains uncertain for a lost delete, since a move produces the
same absence; an actual deletion acknowledgement/tombstone is required.

[evidence/google-tasks-retirement/subtrees.json](../evidence/google-tasks-retirement/subtrees.json)
records isolated PostgreSQL fresh/empty-roundtrip migrations, competing parent
completion (exactly one commit), per-child versions, dependent provider delivery
and retraction. The disposable database was removed. The provider gate passed
122 cases and the native gate 268; this is implementation evidence, not deployment
or activation. Inbound Calendar reminder reconciliation, remaining G3 exceptions,
release validation and same-App activation still remain.

### Owned Calendar reminder imports

An additive migration (`20261106a7b8`) retains immutable per-generation request,
principal and grant-epoch authority. Calendar observations use a closed variant
of the existing `teff_` service root, with their actual identity/ETag/clock proof;
they do not masquerade as Google Task observations, user requests or utterances.
The shared ChangeSet engine admits only remind_at/timezone for the current exact
owned helper and current grant. Disabling Task-list synchronization does not
revoke this independent Calendar relationship. Revoked grants, copied labels,
retired generations, all-day edits and unsupported recurring conversions leave
canonical work unchanged and retain a useful deferred reason.

Clock changes replace/cancel the old helper and refresh the transport-only AM/PM
header. Calendar deletion clears the reminder alone. Privacy/free/zero-popup
changes resnapshot the owned helper under the same bounded authority. Canonical
Task titles, scheduled days, deadlines, work and financial records stay scoped
to their ordinary commands. Task views include compact generation, destination,
operation/delivery and binding state; receipt success does not assert device
notification display. Reminder source observations have a bounded history view.

[reminder-inbound.json](../evidence/google-tasks-retirement/reminder-inbound.json)
records fresh/empty-roundtrip PostgreSQL migration, genuine clock import while
Tasks sync is disabled, immutable authority, parent/timer/race regressions and
cleanup of the temporary database. Provider/storage gates passed 127/152 cases.
The same-App source was inspected (USER/private, unchanged App and release);
its prepared skill update adds only a conditional Tasks reference. Nothing from
this slice has been deployed or published yet.

### Current-subtree provider boundary

The first subtree slice used individual child lifecycle writes. A concurrent
ancestor move can leave a grandchild's ETag unchanged, so those writes could
reach outside Google's current parent subtree. The executor now sends the one
parent lifecycle write and independently reads known descendants. Each child's
confirmation depends on the parent effect, rather than on another child's
confirmation. It never sends individual descendant completion/deletion writes.
Mismatch responses retain actual observations and one coalesced parent notice;
a child outside the subtree does not block other confirmations or receive a write.
Actual Google child completion timestamps can converge under the existing exact
standing import policy while independent later canonical edits remain protected.

Explicit cancellation removes the mirror and preserves canonical records. Notes
on a cancelled task do not create another mirror. Explicit reopening authorizes
a fresh representation, retaining the previous identity/exclusion and Item.
Positive tombstones are retained as real provider resources; binding changes and
immutable revisions flush atomically on PostgreSQL.

[subtree-boundaries.json](../evidence/google-tasks-retirement/subtree-boundaries.json)
records the concurrent-ancestor test, cancellation/reopen journey and final
PostgreSQL proof. The provider suite passed 129 cases; 268 native cases passed,
and final Ruff/strict mypy passed separately. Production has not been changed.
Remaining work includes runtime method admission, release/host acceptance and the
intended-device notification check.

### Runtime method validation and sibling recovery

The local `docket-tasks` administration command probes only supplied dedicated
validation lists and records immutable account/adapter/current-grant evidence
through application services, with a safe audit event. It does not accept a
hand-written success report. Scoped target checks precede fixture creation;
cleanup touches only identified fixtures. A grant rotating during the probe
records no admission, and normal access-token refresh preserves existing evidence.
`status` exposes readiness without credentials. The source procedure is
[Google Tasks operations](google-tasks-operations.md). Actual runtime execution and current-grant admission now pass for all four methods;
see the activation receipt. No credential fingerprints are published.

A sibling hint is now explicitly a provider projection in canonical previews,
preventing the former missing-Task-column failure. Lost move/insert responses with
an after_task_ref can use bounded paged sibling coverage, nearest-predecessor
comparison and a final target read. Changed, ambiguous or incomplete ordering
stays uncertain. A satisfied move acknowledgement recovers without a second move.
The synthetic lost-ack journey passes; no canonical sibling-rank column or new
MCP argument is introduced. Native instructions remain 1,889 bytes and the prepared
plugin source is version 2.2.0. Hosted identity/configuration remain unchanged.

## Validated activation — November 6, 2026

The final source snapshot passed 590 tests, Ruff, strict mypy and all selected
native/provider/storage/release groups. Its immutable image passed native startup
with 114 tools, migration roundtrips, retained-history refusal, exact preview and
atomic batch checks, and PostgreSQL work/finance/task/reminder/subtree races.
Rehearsal assertions now select only their owned Calendar records, refresh their
exact account/calendar and verify the new populated reminder-authority downgrade
guard without discarding retained history.

PR65 merged by a history-preserving fast-forward. Clean synchronized main deployed
through `scripts/docket deploy`; the tested image, prior rollback image, pre-migration
backup, drain, schema head and health are recorded in the activation receipt.
Actual runtime method admission exercised only the two authorized disposable lists,
cleaned its fixtures and verified all four methods against the current Google grant.
Discovery is enabled; personal lists have not been silently enrolled in two-way sync.

The same private App/plugin is updated to 2.2.0. Read-back confirms the original
App binding, presentation, connection metadata and audience, plus the compact Tasks
reference. Native OAuth overview/account/context calls work. The authorized
validation Calendar was adopted through an ordinary native command, with genuine
request/ChangeSet attribution and no provider effect. A manual server rescan,
new Tasks-tool journeys and intended-device popup observation remain outstanding.

### Fresh ChatGPT context acceptance

The Operator completed the same-App rescan and confirmed task context loads in a
fresh chat. [host-context.json](../evidence/google-tasks-retirement/host-context.json)
corroborates that observation with two successful docket_get_tasks_context calls
and one docket_query_tasks call on contract v68. Access and method admission are
ready; lists remain disabled until their explicit native policy is configured.
The four older local tasks remain local. The current long-running Codex connection
still exposes its cached older tool bindings, so the fresh ChatGPT chat is the
available native host for the validation-list write and reminder journey. No
legacy alias, fabricated request or direct production row edit is used to bypass it.

### Real native write and reminder acceptance

[native-journey.json](../evidence/google-tasks-retirement/native-journey.json) records
14 actual v68 native write invocations and fifteen confirmed subtree operations:
creation, notes/day edits, completion/reopening, native cross-list moves in both
directions and retraction. Provider identities remain stable across both lists;
reopening the parent leaves its child completed. Two context/version rejections
were recovered with fresh context. The four older local tasks remain unchanged,
and My Tasks remains disabled under the validation-only instruction.

[full-title-live.json](../evidence/google-tasks-retirement/full-title-live.json)
adds a real 1,024-character Google title import through the admitted standing-policy
service, an atomic rename of the owned Item, native context reads at full length,
and tombstone-confirmed cleanup. It is an explicit scoped read/reconciliation
exercise, not a claim that an operator performed work or that a timed poll ran.

[reminder-native-live.json](../evidence/google-tasks-retirement/reminder-native-live.json)
records a real native-created Task and separate timed Calendar companion. Actual
Google read-back confirms private/free, sixty elapsed seconds and popup zero. A
forced refresh of only the designated Calendar produces a new persisted cache
generation with the same typed reminder origin. The Operator sees the timed event
in the browser and iPhone Google Calendar app but did not receive the first popup.
The all-day Task is the date-only representation; its header supplies the visible
clock. It is separate from the timed Calendar event supplying the exact alert.

The Operator requested a 6 PM retry. The unchanged fixture was rescheduled through
the genuine native update command. [reminder-six-pm.json](../evidence/google-tasks-retirement/reminder-six-pm.json)
records confirmed old-marker cancellation, replacement at 18:00–18:01 Los Angeles,
zero-minute popup and updated 6:00 PM Task header. Device observation is pending.

The delivery summary exposed a display defect: it validated committed offset-aware
reminder snapshots against the offset-free public event-input model. The correction
accepts bounded, ordered offset endpoints for retained delivery display, preserving
both DST instants and the stored transport timezone. Public input schemas and
provider effects are unchanged. Existing ordinary and spring/autumn reminder
journeys now assert truthful timing summaries; affected provider checks pass.

### Delivery correction activation and actual-device investigation

The delivery-summary correction deployed from `3a6cd55521d7` through the validated
image and ordinary drain/backup path. [delivery-timing-activation.json](../evidence/google-tasks-retirement/delivery-timing-activation.json)
records 458 release cases plus 132 affected provider cases and actual corrected
native history output. Native input contracts, plugin identity and method gates
remain unchanged.

The at-start alarm still did not appear at 6 PM. The Operator confirmed the exact
timed event, its bell set to at-start, and iPhone Google Calendar notifications and
banners enabled. An app-created control on the same Calendar alerted at 6:10 while
the Docket event did not. Calendar transport failures delayed that Docket marker
until forty seconds before the test. Exact original-operation read/reconciliation
proved absence before the same reserved identity was executed; no blind creation
or manual production row repair was used.

[notification-comparison.json](../evidence/google-tasks-retirement/notification-comparison.json)
records a 6:25 comparison prepared at 6:14: both API-created events share account,
private visibility, one-minute duration and popup zero. The independent control
is busy; the canonical Task companion is free. Actual device observation remains
pending. API acceptance is not treated as a successful phone alarm.

The cancellation recovery correction is implemented in source: a genuinely fresh
read matching the original non-wildcard ETag can return the same retired-marker
cancellation to execution. Its original transmission pin is retained, including
if a Calendar refresh changes the binding between read and retry. Changed markers
stay unresolved and are not deleted. Retired creation remains blocked. Recovery
polling defaults to fifteen seconds with existing per-operation backoff, replacing
the five-minute cadence that delayed the live test. Three focused interleavings
prove unchanged cleanup and edits before/after the proof read.
