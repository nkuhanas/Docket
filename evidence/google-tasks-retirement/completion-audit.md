# SPEC 1.2 completion audit

Scope: every requirement in the adopted [Google Tasks/NocoDB delta](../../deltas/2026-11-06T17-59-26Z-google-tasks-and-nocodb-retirement.md),
including the title/ownership, timed-reminder, AM/PM-display and current-subtree
amendments. Earlier checkpoint receipts remain frozen; later receipts supersede
their pending acceptance fields. Device success does not establish a notification SLA.

| Requirement | Inspected implementation and authoritative evidence |
| --- | --- |
| One canonical Task, Item and stable relationships; no second registry | `tracked_context.py`, `google_tasks.py`, shared task command/import services; [native journey](native-journey.json), [full-title live proof](full-title-live.json), isolated PostgreSQL checks |
| Full 1,024-character Task/Item path and lossless reads/exports | Ordered title migration, typed inputs/mutations and derived Item compilation; actual import/rename/context proof above; native JSON/CSV export roundtrip covers full titles and 16,000-character Unicode notes |
| Derived-title ownership, independent/shared/relinked relinquishment and competing edits | Typed ownership model and canonical mutation service; task-sync tests and PostgreSQL Task/Item race checks; no historical ownership inferred from matching text |
| Scheduled day, deadline and alert instant remain distinct; richer task states preserved | Flat task schemas, three-way reconciliation, unchanged-open and completion-precision tests; actual native scheduled-date edits, completion/reopening and reminder reschedules |
| OAuth union, atomic verified replacement and feature isolation | `google/oauth.py`, setup CLI and OAuth tests; completed consent/API enablement and grant-bound runtime method admission in [activation](activation.json) |
| Selected-list policy, deletion disclosure, genuine authority roots and revocation | Task-list/context/command services, typed policy effects and shared engine; native policy receipts; policy-authority tests and PostgreSQL retained-policy/import proofs |
| Privacy and untrusted provider notes | Invocation attribution retains refs/digests rather than text; provider bodies/errors remain bounded and sanitized; triage rejection, payload substitution, current grant and owner checks exercised by native/storage groups |
| Discovery, selected import, explicit linking and duplicate prevention | Native configure/link journeys and sync tests; provider identity uniqueness in PostgreSQL; full-title actual Google import retains policy roots without a fabricated request |
| Bounded polling, full/hidden/completed/deleted pagination, overlap, leases and restart | `tasks_polling.py`, provider paging and polling tests; runtime selected-list coverage and empty validation-list readbacks in [device acceptance](device-acceptance.json) |
| Three-way field merge, delete/edit conflicts, detach/disable and list loss | Native link/field-resolution/detachment journeys; task-sync/polling tests; one conflict does not block independent fields or local work |
| Completion, cancellation, retraction and reopening | Actual Google/native completion and tombstones; [Google completion proof](google-completion-live.json), native journey and final cleanup. Item/history survive; reopening does not replay an old alert |
| Parent/subtree and sibling movement with stable identity | Actual source/destination moves and subtree probe; native operation tests cover new children, concurrently moved descendants and lost sibling-move acknowledgements; no recursive input or copy/delete strategy |
| Assigned/opaque first-party features remain limited | Adapter decodes assignment/limitations, uses sparse known-field patches and refuses assigned writes; native clock loss is recorded, with Operator-adopted title headers rather than a false clock-preservation claim |
| Shared Operations queue, atomic intents, pinned bases and method-specific activation | Operation runner, task adapter and immutable binding revisions; real insert/patch/move/delete admission; source-bound PostgreSQL atomicity/race and drain checks |
| Correlation outside canonical notes; uncertain insert never blindly repeats | Scoped marker encoder/decoder and retained creation identity; actual discarded-response recovery, plus ambiguous/absent/removed-marker and restart tests; canonical notes and exports stay clean |
| Exact automatic Task reminder, routing and separate provider outcomes | Task generation/plan/time/projection and queue transaction; native 7:15 request and two confirmed operations in [trial](reminder-synced-device-trial.json); no companion CRUD grammar or invented deadline |
| Required private/free serialization and reconciliation | Actual rendered/GET-verified private visibility, transparency and popup-zero settings; privacy-change detection in real provider proof; current format also passed the intended-device test |
| Stable typed cache origin, refresh/restart and workload exclusion | Origin/cache schema and binding/operation validation; [point observation activation](point-observation-activation.json), active-marker full-refresh/workload proof in the trial; copied metadata gains no ownership |
| Sixty elapsed seconds through DST | Instant-based compiler, independent endpoint rendering and reconciliation; actual spring/fall Google tests and documented UTC fallback in [live provider proof](live-provider.json) |
| Reminder replacement, null/completion/deletion retirement and uncertainty | Native reminder and PostgreSQL replacement tests; owned Calendar observation/authority tests; actual cancellation recovery retains its original sent pin in [recovery activation](cancellation-recovery-activation.json) |
| Workload, exception reporting and independent local usefulness | Task-aware workload/inbox paths and all native work/finance tests; disabled/unavailable/conflicted projections stay scoped, ordinary scheduled days add no busy duration |
| Six compact workflows, existing command reuse and lossless continuation | Runtime 114 tools, six Tasks workflows, instruction and 4/6/24 KiB measurements; JSON/CSV uses one optional `format` field and existing bounded, connection-owned read snapshots. No new table or public export tool |
| NocoDB inventory, protected recovery and explicit unique-data disposition | [Inventory](inventory.json), isolated restores and Operator-authorized discard in [retirement](retirement.json); all three retained encrypted archives checksum-verified during final acceptance |
| Complete NocoDB runtime/access/startup/storage/image retirement | Exact six containers, four volumes, two networks and dedicated images absent; no active source/Compose/environment dependency; tested image cold starts and production is healthy after retirement |
| Fifteen aliases removed, current routes and historical receipts retained | Native registry/transport test and actual deployed registration introspection; no compatibility registrations, translators or fallback service |
| Additive PostgreSQL storage, concurrency and recovery | Ordered migrations through `20261106a7b8`; full release isolated fresh/additive migration, retained downgrade refusal, constraints/history and work/Task races. No production-row repair/reset |
| Tested artifact, drain/idle/backup/deployment and same App | Source-bound release receipts and deployed image, normal deployment logs/backup and health; [same-App package](hosted-plugin.json), actual v68 host reads/writes. Export follow-up release and package are tracked separately |
| Intended-device alert | Operator confirmed both the native Task companion and API control G at 7:15 PM PST on the iPhone; [device acceptance](device-acceptance.json) records pre-alarm visibility and cleanup independently from API acceptance |
| Final fixture cleanup and preservation | Native cleanup has two confirmed operations and actual Task tombstone/Calendar cancellation; all nine plain/manual controls removed, both selected validation lists have no open fixtures; four older local Tasks remain version 1 and unlinked, My Tasks stays disabled |
| SPEC, index, generated contracts, capability disposition, skill and runbook | SPEC 1.2 and forward index; generated native contract/capability report, task skill reference and operational procedure. Current release status is reported separately from frozen historical checkpoints |

Export follow-up: implementation and native validation are in progress. Final
artifact/package evidence must pass before complete acceptance is claimed.

The 7:15 test used an already visible event on the phone. Late device propagation
or alert registration is the leading explanation for earlier misses, not a proven
sole cause or a measured minimum lead time. No automatic delay, widened marker,
privacy downgrade or extra notification channel was introduced.
