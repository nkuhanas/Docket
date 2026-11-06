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
| R0 inventory | Exact ownership/status of NocoDB containers, networks, volumes, startup/access and unique data | Six evaluation containers stopped; three dedicated volumes identified by current read-only Docker metadata; data disposition pending |
| R0 protected data | Consistent encrypted archive, checksums/recovery, schema/count/identity comparison and unique-data disposition | Three authenticated encrypted archives verified; both PostgreSQL archives restored into networkless temporary clones and inventoried. 9 Tasks/11 Items lack current canonical identities; disposition pending |
| G0 OAuth/readiness | Tasks scope profiles preserve granted scopes; atomic verified replacement and isolated feature readiness | Scope upgrade implemented and tested; runtime capability readiness pending |
| G0 adapter | Real/fake/disabled Tasks read/write protocols, exact field/marker handling and guarded method dispatch | Transport/pagination, exact-version writes, unknown outcomes and scoped notes implemented with synthetic tests; runtime dispatch and live validation pending |
| G0 live capabilities | Method-specific controlled insert/patch/move/delete/date/time/uncertainty evidence | Pending; unverified methods remain disabled |
| G1 title path | Full 1,024-character Task and required Item schemas/columns/mutations/context/export, including creation without Item | Implemented with ordered widening migration and real native MCP roundtrip; PostgreSQL rehearsal/import delivery pending |
| G1 title ownership | Typed derived-pair ownership, atomic renames, independent/shared/relinked relinquishment and concurrency | Shared services and explicit retained ownership implemented; native rename/same-title relinquishment verified; PostgreSQL race evidence pending |
| G1 durable domain | Scheduling/hierarchy, lists/policies, immutable observations, bindings/generations, baselines/conflicts and sync state | Typed storage and migration implemented, including immutable bounded policy-effect authority roots; live reconciliation/polling pending |
| G1 reads/import | Context/query/export and selected-list import through admitted canonical services with policy/observation roots | Pending |
| G1 polling | Full hidden/completed/deleted pagination, overlap watermarks, resumability/full inventories and coverage | Pending |
| G2 task writes | Flat create/update/complete/reopen/retract and exact retry/preview, with atomic Tasks intents | Pending |
| G2 conflict/lifecycle | Three-way field merge, delete/edit conflict, detachment/disable/list loss without global blocking | Pending |
| G2 markers/limits | Transport-only marker baseline separation, no blind insert retry or silent field truncation | Pending |
| G2 reminders | Flat remind_at derives exact owned primary Calendar companion, routing, reschedule/null/completion cancellation and uncertainty | Pending |
| G2 privacy | Private/free actual request and read-back/reconciliation, existing zero-minute popup reused | Calendar renderer/read masks/reconciliation implemented and covered through HTTP adapter; Task intent derivation pending |
| G2 workload origin | Validated typed origin survives cache refresh/restart and excludes canonical/cache commitment/capacity accounting | Pending |
| G2 DST | UTC start plus 60 elapsed seconds with independent offsets, actual rendering and reconciliation | Timed-marker compilation and actual rendering/reconciliation pass autumn and spring transitions; Task reminder flow pending |
| G3 hierarchy/planning | Native cross-list/parent/sibling moves, exact identity and constraints; workload/reminder/inbox integration | Pending |
| Authority/privacy | OAuth/local grant, owner isolation/revocation/triage rejection, genuine service roots, no sensitive invocation payloads | Shared-engine policy import creates no per-poll request/utterance; list/account/local/OAuth revocation, triage rejection and exact-effect admission covered. Full native/new-worker evidence pending |
| Contracts | Six compact tools, every modified schema, 4/6/24 KiB and context/result budgets, generated docs/skill | Pending; future tools are not yet advertised |
| Storage | Fresh/populated isolated PostgreSQL migrations, constraints/history/races, restart/drain and recovery | Pending |
| R1 retirement | Unique data migrated/disposed; all dedicated NocoDB runtime/access/startup removed; no fallback/dual write | Pending |
| R1 aliases | All fifteen hidden native aliases unregistered without effects; current routes/history still work | Pending |
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

The native Task input schemas currently measure 2,016 bytes (create) and 2,622
bytes (update), using compact JSON serialization. These remain flat contracts;
runtime tool-definition and combined discovery budgets are checked again when the
six task tools are registered. The new reminder field is not a deployed alert
capability until the G2 projection/delivery path is complete.

The [sanitized R0 inventory](../evidence/google-tasks-retirement/inventory.json)
records volume ownership, ciphertext checksums, restoration checks and aggregate
identity comparisons. Private archives and schema/count inventories are retained
under `.runtime/nocodb-retirement/`, with the protected key under `secrets/local/`.
No private rows, transcripts or credentials are in that evidence. Original
volumes remain unchanged; no NocoDB runtime deletion is claimed yet.
