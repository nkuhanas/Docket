# Docket: Google Tasks synchronization and NocoDB retirement

Draft timestamp: 2026-11-06 17:59:26 UTC
Status: proposed; no implementation, adoption or provider changes
SPEC baseline: [1.1](../SPEC.md)
Inspected source: `8488f165efc100436813f1bebda7d014dcd5846c`
Proposed SPEC target: 1.2 on an Operator instruction to implement

## 1. Decision

Make Google Tasks a supported everyday interface to Docket's existing canonical
tasks. Support capture, edits, scheduling, completion and reopening from either
ChatGPT/MCP or Google's task interface, with explicit synchronization coverage and
recoverable provider delivery. PostgreSQL remains the canonical system of record.

Retire NocoDB completely. Preserve and migrate useful records before removing its
remaining evaluation runtime, access and deployment residue. Do not replace it
with another self-hosted table UI. Rich organizations, projects, work, compensation
and financial records stay in Docket and remain accessible through MCP and exports;
Google Tasks is a task interface, not a replacement for every relational table.

Use scheduled dates for the Google task interface and leave Google's separate
Deadline feature unused. Exact-time notifications in Google's UI are a distinct
capability from its date-only public API. Existing Docket deadlines remain valid,
but neither creating a Google task nor requesting a reminder requires one.

The architectural review is incorporated below: the complete Task/Item title
path, ownership of automatically derived Item titles, explicit deletion-policy
consequences, transport-only creation markers and per-operation G0 activation
evidence. These amendments refine this proposal without adopting or implementing it.

The Operator additionally requests a clean break without legacy compatibility.
Remove the fifteen hidden native MCP compatibility aliases retained by the prior
catalogue consolidation and visibility extension as part of this release. Keep their underlying domain
capabilities through current commands. Preserve genuine historical receipts and
readable evidence, without callable aliases, payload translators or old-tool replay.
This deliberately supersedes SPEC 1.1's standing cached-client alias requirement
on adoption. It does not retire legitimate canonical Tasks or their public refs.

This is a forward proposal, not a second master SPEC. Drafting it authorizes no
Google writes, data deletion, credential changes or runtime shutdown. Adoption
updates SPEC, decisions/history and the delta index together. Existing development
and release authority applies; add no Discord, ledger or repeated approval gate.

## 2. Inspected baseline and actual gaps

| Surface | Present | Required extension |
| --- | --- | --- |
| Canonical `Task` | Stable `task_`, required Item link, title/notes, five task states, priority, completion time, provenance and version | Reuse it; add typed scheduled date and task hierarchy rather than another task registry |
| Required Item/title path | Both Task and Item columns and their create/patch inputs currently limit titles to 512; auto-derived Item creation copies the Task title | Widen the entire path to 1,024 before G1; record explicit title ownership for newly auto-derived Items |
| Native task commands | Flat create/update; completion through update; shared logical retraction; due time derived internally | Add small scheduling/list/hierarchy fields and derive Google effects internally |
| Due time | `TemporalBinding(role="due_by")` supports Docket deadlines | Keep distinct from Google's scheduled day and from time spent working |
| Provider account | Existing Google account, capabilities and protected credential reference | Add Tasks capabilities and scope readiness without duplicating account identity |
| Provider execution | Durable Operations, targets, attempts, leases and uncertain-outcome recovery | Current intent types, account checks and runner are Calendar-specific; add typed Tasks dispatch and recovery |
| Google OAuth | Workspace/Calendar/Gmail/Docs/Sheets scope profiles | No Tasks scope/profile currently exists; consent extension must preserve existing scopes |
| Context and history | Owner-bound packets, exact versions, prepared changes, audit and delivery reads | Add task-list choices, task observations, synchronization baselines and task delivery facets |
| NocoDB | No dependency found in the inspected tracked runtime/source; current Compose is Docket plus PostgreSQL | Inventory remaining stopped evaluation resources and any unique persisted data, then remove them |
| Compatibility | Thirteen individual retraction aliases, including work Calendar visibility, plus `docket_complete_task` and `docket_get_conflict` remain callable outside discovery | Remove registration, dispatch fallbacks and compatibility-only tests; require current commands after rescan |

Read-only Docker metadata identifies `docket-workspace-eval-nocodb-1` and its worker
using `nocodb/nocodb:2026.09.0`, both stopped. Associated evaluation Docket,
PostgreSQL, metadata PostgreSQL and Redis containers are also stopped. The recorded
Compose path is `compose.workspace-eval.yml`, which no longer exists in this checkout.
Named volumes include `docket-workspace-eval_nocodb-data`,
`docket-workspace-eval_metadata-data` and `docket-workspace-eval_canonical-data`.
These observations do not establish that their data is empty or disposable.
No NocoDB rows, credentials or private source contents were inspected for drafting.

## 3. User-visible behavior

The operator uses natural language. The connected agent resolves names and dates
from compact context; Docket validates, persists and synchronizes deterministic
changes. Ordinary work does not require learning import graphs or provider IDs.

| Interaction | Expected result |
| --- | --- |
| “Use my Personal Google list for new tasks.” | Bind the selected existing list and set its explicit default; show import scope and last successful scan |
| “Add renew passport to Personal for Friday.” | One canonical Task and derived Item if needed; Friday is its scheduled date; one queued Google create |
| “Remind me about the report Friday at 5 PM.” | Google receives Friday's scheduled day; a requested Docket follow-up retains 5 PM and uses its configured inbox/delivery route, without creating a deadline or claiming a Google-native timed alert |
| “Bring in my Work list.” | Import eligible tasks under that list's admitted sync policy; retain original source identity and flag possible matches to existing records |
| “I checked that off on my phone.” | The next successful poll imports completion once; an explicit refresh can request a sooner check |
| “Reopen it; move it to next Monday.” | Reopen the same canonical task and update its scheduled day without replacing its identity |
| “Move this under travel planning, after book flights.” | Resolve parent/sibling refs; derive supported Google move operations and ordering |
| “What needs doing this week?” | Combine scheduled tasks, true deadlines and existing workload context, with coverage and unresolved sync issues visible |
| “These are the same task.” | Explicitly link the selected existing records; titles alone never merge identities |
| “Keep Google's version of that change.” | Resolve the selected current field conflict through a small named command; unrelated tasks continue |

Creating or completing a task records neither attendance nor actual working time.
Existing WorkSession/Item/project relationships remain useful without a Google list.
Local-only tasks continue to work when Google is unavailable or unconfigured.

## 4. Google API boundaries checked for this draft

The November 6 [Task resource](https://developers.google.com/workspace/tasks/reference/rest/v1/tasks)
exposes a scheduled day through `due`, two completion states, notes, hierarchy and
links. Its scheduled value cannot carry a clock time or a deadline. It has no
documented writable recurrence, reminder or priority field. Treat unsupported
first-party features as unobserved; never clear them through full-resource writes.
Assigned tasks have additional restrictions and remain observation-only initially.

Google's [UI documentation](https://support.google.com/tasks/answer/7675838)
supports a scheduled date/time with a notification at that time, separately from
its Deadline feature. The public Tasks API still cannot read or write that
scheduled clock time. Avoiding Deadline is the chosen product behavior; it does
not make a 5 PM Google-native alert programmable through this API. A time set in
Google's UI stays unobserved by the adapter. Sparse updates and G0 checks must
prevent unsupported assumptions about preserving or changing it.

The [list method](https://developers.google.com/workspace/tasks/reference/rest/v1/tasks/list)
is paginated, with at most 100 tasks per page and an `updatedMin` filter.
Read completion, hidden and deletion signals explicitly: first-party completed
tasks can require both `showCompleted=true` and `showHidden=true`; request
`showDeleted=true` as well. Assigned tasks require a separate explicit flag.
Task-list discovery [also paginates](https://developers.google.com/workspace/tasks/reference/rest/v1/tasklists/list).

The current [move method](https://developers.google.com/workspace/tasks/reference/rest/v1/tasks/move)
supports `destinationTasklist`. Use native cross-list moves where supported,
instead of manufacturing a copy/delete pair. Repeating and assigned tasks have
documented move/parent restrictions; completed hidden tasks also restrict ordering.
Do not infer arbitrary hierarchy support or that a cross-list move preserves every
provider identifier without a contract check.

[Clearing completed tasks](https://developers.google.com/workspace/tasks/reference/rest/v1/tasks/clear)
hides them; it is not deletion. [Deleting an assigned task](https://developers.google.com/workspace/tasks/reference/rest/v1/tasks/delete)
can delete its original in Docs or Chat Spaces. Neither bulk clear, list deletion
nor assigned-task mutation belongs in the initial write surface.

[Patch is supported](https://developers.google.com/workspace/tasks/reference/rest/v1/tasks/patch).
Google's [performance guide](https://developers.google.com/workspace/tasks/performance)
describes sparse updates and ETag preconditions, partly through generic examples.
Prove the actual Tasks behavior for patch/delete/move and null clearing during
adapter acceptance; a generic example alone is not proof for every method.

The reviewed [v1 method catalogue](https://developers.google.com/workspace/tasks/reference/rest)
does not supply a Tasks watch method or a Calendar-style sync-token workflow.
Use bounded polling as the initial design; do not claim real-time webhooks or
borrow Calendar's cursor semantics. Revisit only against a concrete supported API.

## 5. Authority, account setup and privacy

Keep the existing MCP `docket.read` and approved `docket.write` permissions.
No `docket.tasks` Auth0 scope, new OAuth application, background-agent authority
or human-presence check is needed. Restricted triage still cannot mutate tasks.

Google's separate grant must include
`https://www.googleapis.com/auth/tasks` for two-way operation, or
`https://www.googleapis.com/auth/tasks.readonly` for observation-only use.
These are the [documented Google scopes](https://developers.google.com/workspace/tasks/auth).
They are account-wide grants: list selection is enforced by Docket, not by Google.

Implementation adds explicit `tasks` and `tasks-read` scope profiles. Extend the
existing credential grant with the union of already needed scopes; never replace
Calendar/Gmail access with a Tasks-only token or assume a successful refresh adds
new scopes. Validate the scopes actually granted and retain the working token
until a replacement is safely written. Missing Tasks permission affects Tasks
readiness, not Calendar, Gmail or local task editing. Do not widen the default
workspace profile silently for other deployments.

Enable the Tasks API in the existing Google Cloud project. A consent step may be
needed for the additional Google scope; current owner login/publication and
verification requirements are determined from that project's actual configuration.
Do not ask for secrets or callback URLs in chat. On adoption, document an exact
credential-upgrade/recovery command using the current setup CLI and selected scope
union; do not advertise an unimplemented Tasks flag as working today.

List configuration admits a bounded deterministic synchronization policy:
account, selected list, observe/two-way/disabled mode and optional default for
new tasks. In two-way mode it explicitly permits importing supported task fields
and confirmed per-task lifecycle changes. Polling performs no inference and gains
no authority to edit other lists, organizations, work sessions or money.

The `two_way` mode description, task-list context and configuration receipt must
plainly state that Google deletion can retract the linked canonical Task. Use the
following fixed lifecycle semantics, rather than adding another policy grammar:

| Operator action | Canonical consequence |
| --- | --- |
| Complete in Google | Complete the linked Task once, retaining valid completion evidence |
| Delete in Google under two-way policy | Retract the unchanged linked Task and retain history; pending local edits create a conflict |
| Detach the Google representation | Preserve both records and suppress automatic relinking |
| Disable list synchronization | Preserve canonical tasks and suspend further sync under that policy |

These consequences are visible when configuring the list; they introduce no
per-deletion approval prompt. Disabling/detaching stops new affected dispatches
and imports at the recorded policy boundary. Requests already transmitted may
finish and require honest outcome reconciliation; do not claim they were unsent.

Workers retain the policy revision, provider observation and authenticated policy
root behind imported ChangeSets. They do not fabricate a live-user request or
utterance for every poll. Extend the authority engine's typed service admission
if necessary; never borrow a stale chat credential or write canonical rows directly.
Revoked local grants, disabled accounts/policies and scope loss stop future affected
mutations and remain auditable. In-flight effects recover under the existing rules.

Task notes and provider observations are untrusted data, never instructions to an
agent. Do not add employer terms, invoices, receipts, transcript content or other
linked records to Google notes automatically. Invocation logs retain bounded refs
and reason codes, not task text, OAuth material or raw provider payloads.

## 6. Canonical model and field mapping

Extend the existing Task and typed task inputs; reuse current mutation/history
services. Add `scheduled_date: date | null` and `parent_task_ref: TaskRef | null`
with owner, cycle and active-parent validation. Preserve true deadlines in the
existing `due_by` bindings. Provider sibling order is a projection concern, not
an inferred priority or dependency graph. Widen canonical Task titles to 1,024
characters so valid imported titles are not truncated. The required Item path is
part of the same G1 prerequisite: widen `Item.title` as well as `Task.title`,
their database columns and applicable constraints, `ItemInput`/`ItemPatchInput`,
`TaskInput`/`TaskPatchInput`, native and generated Item/Task contracts, and canonical
create/modify validation. Check derived Item compilation, import validation,
context/query/history/export and subsequent synchronization against the full
title. Widening only Task is insufficient: an imported 900-character title must
not fail while constructing its required Item. Do not truncate, hash or substitute
a placeholder title to bypass the model mismatch.

Automatically derived Items have a typed, service-owned title-ownership marker
tied to their Task/Item pair and recorded creation provenance. While that Item is
exclusively title-owned by the Task, a task rename, including a Google import,
updates both titles in the same canonical transaction with normal versions/audit.
The agent supplies no companion Item mutation or ownership selector.

An explicit independent Item-title edit relinquishes title ownership, even if the
new text happens to be equal. Sharing that Item with another Task or relinking the
owning Task also ends the old title ownership. Existing independently selected or
shared Items are never task-owned by default. Unrelated Item-field edits do not
overwrite those fields or require the agent to repair a title graph.

Lock/version the Task, ownership state and Item in a consistent order. A concurrent
independent Item-title edit cannot be overwritten by a delayed Google task rename.
Retain ownership transitions in canonical history and expose the relationship in
bounded Task/Item reads once titles are independent. Do not infer historical title
ownership from matching titles, `kind="work.task"` or a sole current relationship;
old unmarked pairs remain independent unless provenance or explicit new intent
establishes ownership. This is a domain distinction, not a legacy dispatch path.

| Meaning | Docket | Google projection/import rule |
| --- | --- | --- |
| Title | Canonical Task title | Preserve the full supported title |
| Notes | Existing description, up to 16,000 characters | Preserve full canonical text; Google has a smaller field limit, so report a blocked notes projection rather than silently truncate |
| Scheduled day | New date-only field | Map Google's `due` calendar-date component directly, without UTC-to-local day shifting |
| Deadline | Existing date/datetime `due_by` | Never overwrite it with a Google scheduled day; no invented midnight or lost deadline clock |
| Timed reminder | Existing explicit-instant operational reminder, extended to Task subjects | Deliver through the configured Docket inbox/notification route; no Google-native time/Deadline write is claimed |
| Open status | not_started / in_progress / blocked | All project to `needsAction`; an unchanged Google open status does not reset Docket's richer state |
| Completion | completed plus exact completion basis | Project/import completion; repeated observations do not rewrite completion time |
| Reopening | Nonterminal canonical state | A genuine completed → open transition reopens; default to not_started, while retaining history |
| Priority, project, Item, work links | Existing canonical relationships | Stay local; Google absence is not a request to clear them |
| Parent/list/order | Typed parent plus provider binding and observed order | Use supported move semantics; validate same-list parent/sibling, descendants and cycles |
| Deleted/hidden | Distinct lifecycle observations | Hiding preserves completion/history; confirmed deletion is handled separately below |

Scheduling a task does not allocate a duration, reserve capacity, create a Calendar
Event or set a financial reporting deadline. The normal Google task path uses
scheduled/display dates and leaves Google's Deadline feature unused. Do not
automatically create a Docket `due_by` binding as a workaround for a scheduled
task or a timed reminder. Existing or explicitly requested Docket obligations
retain their independent deadlines and precision. A later Google date edit changes
only the scheduled/display day. Omission is useful and does not create a date-type
clarification ceremony for a routine task request.

Represent list policy, provider task binding, retained observations, sync state and
field baselines as typed storage. Reuse the Google ProviderAccount. Bind one active
Google representation per canonical Task initially, with unique provider identity
per account/list/task and retained location generations for moves. Public list and
observation refs hide raw Google IDs; finalize/register their prefixes on adoption.
Each binding pins the last agreed field values and canonical/provider versions.
Retain resolved conflict observations and binding history; do not turn a mutable
cache into the only evidence of an external edit.

Import may derive one minimal Item when no explicit suitable Item exists, as native
task creation already does. Do not create fake Events or duplicate project entities.
Existing Docket tasks are not all exported merely because a default list is chosen;
publish selected existing tasks explicitly through their ordinary update command.

Oversized or unsupported fields remain local with an explicit per-field projection
limitation. Valid local edits and independent projectable fields still commit;
receipts must distinguish partial delivery from fully synchronized state. Never
claim that Google's copy contains notes that were withheld. Imports that lack a
required valid completion timestamp retain their observation and flag that task;
do not invent an actual completion time to satisfy the current Task constraint.

## 7. Bootstrap and inbound synchronization

1. Discover task lists and collect complete bounded observation pages. Return
   freshness, coverage, partial/error state and continuation; reads do not imply
   a refresh or import. No selected lists means no automatic canonical import.
2. Before enabling two-way import, compare provider identities and any explicit
   migration mappings to existing tasks. Similar title/date only suggests a match.
   Link exact selected equivalents; do not silently merge or delete them.
3. Under an admitted selected-list policy, create canonical tasks for new eligible
   observations, with one unique binding and durable source/policy provenance.
   Unsupported assignments remain visible observations with their Google link.
4. Apply external changes through the same typed canonical engine and expected
   versions. Update the field baseline transactionally and suppress echo effects;
   importing Google's completion must not enqueue another identical completion.
5. Use per-list leases and generations. Poll with an overlapping `updatedMin`
   watermark and page through every result; deduplicate by identity/version.
   Advance a committed watermark only after the corresponding work is durably
   represented, including retained conflicts/errors. Do not use a future client
   clock or a partially read page as a complete observation boundary.
6. Run periodic full inventories, including task-list rediscovery, to recover from
   moves, deleted lists, lost cursors, clock skew and changes outside filtered views.
   A page failure, quota abort, access loss or missing task in a filtered result
   is not deletion. Confirm tombstones or reconcile exact identity/location first.

Initial engineering targets are a configurable roughly five-minute poll for
enabled lists and a daily full inventory, with bounded requests per run, resumable
continuation, jitter and exponential backoff for rate limits. Measure and tune
against actual quota/list sizes; never impose a one-page inventory cap. An explicit
refresh queues/coalesces the same durable job rather than spawning an inference
agent or an unbounded request. Queued work survives restart and respects release
drain, account revocation and selected-list policy versions.

## 8. Conflicts, deletion and lifecycle

Use a three-way comparison: last agreed value, current Docket value and latest
Google observation. A one-sided edit converges; disjoint fields merge; identical
edits converge. Divergent edits to the same field retain both versions and create
a bounded conflict. Do not choose the largest timestamp as automatic authority.
An open Google status is compared at Google's coarse granularity, so it cannot
erase a local blocked/in-progress distinction on every poll.

Only affected fields/bindings pause. Canonical local work, other tasks and other
providers continue. Freshly received provider changes cannot be overwritten by a
queued stale operation; compare its pinned basis immediately before sending and
use verified ETag conditions. A precondition failure refreshes/retains the conflict,
rather than force-writing. Check local versions again before committing imports.

Ordinary task cancellation removes the Google representation where supported
without pretending it was completed; canonical cancellation/history remain.
Canonical retraction similarly plans deletion of its linked Google task. A
confirmed ordinary-task deletion in Google can retract the unchanged canonical
task under the admitted two-way policy. Pending local edits instead create a
delete-versus-edit conflict. Preserve work-session links and prior revisions.

List disappearance/deletion never cascades into mass canonical task deletion or
automatic list recreation. Mark bindings unavailable and offer relinking or
local-only continuation. Explicit detachment preserves both records and retains
an identity exclusion so the next automatic import does not recreate the link.
Restoring a deleted mirror requires a new explicit publish intent, not replaying
the old create. Source identities and canonical references remain traceable.

Assigned tasks are observation-only initially because removal can affect other
Google surfaces. Do not send assigned deletes through a generic retraction.
For parents, preserve discovered children and verify Google's actual completion,
move and deletion effects before enabling them. Initially permit leaf deletion;
return an actionable subtree constraint for a parent with remaining children.
An explicitly authorized subtree removal can use current named child retractions
and exact batching with provider prerequisites, without a generic cascade option
on every retraction tool. Incomplete hierarchy coverage never authorizes deletion.

Retain first-party recurring/scheduled task information that the API cannot expose
without inferring a recurrence rule, materializing future instances or claiming
that completing one task completes an entire series. Unsupported provider actions
fail only that projection with a useful next step; use sparse patches for known
supported fields. No undocumented browser automation fills API gaps.

## 9. Outbound delivery and uncertainty

Add Tasks read/write protocols, real/fake/disabled adapters and per-feature readiness.
Extend the existing Operation/target/attempt system and its storage constraints;
dispatch by typed provider capability rather than treating every Google account
as Calendar. Do not copy the whole runner into a parallel Tasks queue or invent
a provider-plugin framework for one adapter. Update auth recovery, receipts,
history, drain/idle accounting and health checks for both domains.

A normal task command commits its canonical change and all required task provider
intents in one transaction. HTTP runs afterward. Same-operation retries recover
the original receipt and intent. Dependent parent/create/move/delete effects have
explicit predecessors; unready parents/lists cannot result in misrouted children.
Queued updates retain their intended field patch and basis, rather than silently
sending whatever the current task contains at execution time.

Google's [insert contract](https://developers.google.com/workspace/tasks/reference/rest/v1/tasks/insert)
does not document a request-id idempotency key. Do not assume Calendar-style
client-assigned IDs or private extended properties. Prove any stronger behavior
before relying on it. A durable Docket operation alone does not guarantee that
repeating an HTTP insert is safe.

Before create dispatch, persist a creation correlation and bounded recovery basis.
If no verified provider idempotency facility exists, a compact visible Docket
reference in notes may assist reconciliation; reserve its space and document it.
The marker is transport correlation data, separate from canonical user-authored
notes and the user-note three-way baseline. Store its known version/correlation
in binding/operation metadata. An outbound encoder combines user text with at most
one recognized marker, accounting for both within Google's notes limit; canonical
notes, exports and ordinary Task views retain only the user text.

Inbound decoding separates only a recognized marker matching the scoped known
binding/correlation before comparing notes. Preserve unrecognized or lookalike
user text. A marker-only difference must not create a user-note conflict, canonical
note revision or endless echo patch. Outbound note changes preserve the marker or
deliberately replace it under the documented binding/correlation policy, without
copying it into canonical notes or accumulating duplicates. Observing user removal
alone does not trigger a notes rewrite to put it back.

It is a correlation aid, not authority or permission to bind an arbitrary matching
title. A fully scoped, uniquely corroborated match can recover the original result;
zero/multiple matches, an unavailable marker or incomplete coverage remain uncertain
when exact outcome identity cannot otherwise be established. An already known task
ID still permits normal user-note comparison when its marker is absent. Never
blindly reinsert after an unknown-after-send result or repeated empty scans. Show
unresolved delivery and permit an explicit new repair intent when needed.

For patch/move/delete, retain exact identity, affected fields and before/after
basis. Reconcile unknown delivery against those facts before retrying; a later
user edit is not proof of the original outcome. Verify method-specific ETag and
retry behavior against controlled Google targets before write activation. If a
method cannot meet the promised guard, keep that operation disabled or constrain
its supported behavior with a valid evidenced strategy. Report the limit instead
of claiming race safety from a preflight GET alone.

G0 is a hard activation prerequisite for each corresponding provider write, not
one general checkbox for the adapter. Retain a capability/evidence matrix:

| Write capability | Evidence required before activation |
| --- | --- |
| Insert | Actual identifier/idempotency behavior; timeout-after-send and restart recovery; unique/ambiguous/no-match correlation and marker separation |
| Patch, including completion/reopening | Actual sparse/null semantics and stale-version precondition behavior; unmodified fields, including opaque first-party time settings, remain protected |
| Within-list and cross-list move | Actual conditional-write behavior or a valid constrained reconciliation strategy; identity, hierarchy and ordering effects under a concurrent Google edit |
| Delete | Actual stale-delete protection or a valid constrained strategy; verified ordinary/assigned/parent effects and no confusion with moves or inaccessible lists |

Test scheduled-day writes against a task with a time set in Google's UI. An
API-returned day cannot establish that the clock survived a write. If preservation
or the intended effect cannot be established, constrain the affected date write
and state that Google-native timed scheduling is unsupported; never silently
erase it or promise an unchanged 5 PM Google alert. Unchanged date fields are not
resent during unrelated title/notes updates.

Fixtures and generic API examples do not prove real method preconditions. In
particular, `move`/`delete` do not inherit patch guarantees, and `If-Match: *` is
not a stale-write guard. A method with missing, failed or invalidated G0 evidence
cannot execute merely because list policy permits two-way sync or to finish G2
sooner. Local task work, observations and separately verified methods remain useful.

## 10. Small native MCP surface

Reuse task create/update, shared retraction, selected-object context, prepared
changes, history, workload context, notifications and account reads. No Google
completion wrapper or generic `manage_google_tasks(action, payload)` tool.

| Proposed new tool | Purpose and bounded input |
| --- | --- |
| `docket_get_tasks_context` | List choices, selected task/observation/conflict refs, versions, coverage and short allowed actions |
| `docket_query_tasks` | Task-first filtering, paging, full-set counts and JSON/CSV export; Item queries do not already provide equivalent task-row pagination |
| `docket_configure_task_list` | Select an existing list's disabled/observe/two-way policy and optional default; its mode description/context/receipt explicitly state completion, deletion, detachment and disabling consequences |
| `docket_refresh_tasks` | Queue/coalesce a bounded refresh for a selected list or enabled lists; return job/coverage status |
| `docket_link_google_task` | Link one existing canonical Task to one selected provider observation without a text-based identity guess |
| `docket_resolve_task_sync` | Resolve one current field conflict with keep-Docket/accept-Google; context holds both versions |

Add `task_list_ref`, `scheduled_date`, `parent_task_ref` and `after_task_ref` as
optional flat fields to existing task create/update. A configured default supplies
the list for new tasks; an omitted update list preserves its binding. Explicit
null detaches without deleting either record. A different non-null list moves a
bound task or publishes an unbound local task, derived internally. An omitted
ordering field preserves order; explicit parent/sibling changes have documented
null behavior and field-specific errors. The agent never supplies positions,
ETags, cursor timestamps, Google IDs, Operation parameters or baseline graphs.

Selected-list import is the normal adoption path. Explicit linking covers existing
equivalents; one-off migration/adoption can use internal services through the
bounded operator migration procedure. Do not add a second family of public import,
export, complete, reopen, patch and CRUD aliases merely to expose adapter methods.

The target is six new tools: 108 → 114 advertised, before any separately proven
consolidation. Removing fifteen hidden aliases does not save advertised slots;
report that separately. Require at most 4 KiB per input including definitions,
6 KiB per complete new definition, and 24 KiB gross additional discovery including
changes to existing tools. Keep global instructions within 2 KiB, routine context
within 8 KiB and normal results within 16 KiB, with lossless continuation. These
are proposal budgets, not measurements of unimplemented schemas. No selectors,
conditional requiredness or operation unions may be added merely to hit a count.

## 11. NocoDB data migration and complete retirement

Retirement can proceed independently of Google consent because NocoDB is already
stopped and not required by the supported stack. Do not restart it as a fallback.

1. Inventory exact evaluation containers, networks, volumes, image consumers,
   ingress/DNS, timers, secrets, database roles and any ignored/untracked files
   belonging to this Docket experiment. Attribute ownership before removing shared
   resources. Container labels identify the old Compose project, not current data
   authority or permission to remove a different application's database.
2. Snapshot retained evaluation storage offline or with a supported consistent
   export. Keep encrypted private archives and a manifest of schema, counts,
   checksums and migration disposition; no row bodies or credentials in Git.
3. Determine whether each table is an unchanged canonical projection, synthetic
   fixture, or unique operator-entered data. Compare to the live canonical records
   by trustworthy identity and recorded lineage. Do not overwrite current Docket
   with an old evaluation database or treat matching titles as identity.
4. Map useful task data into the existing Task/Item model through normal typed
   services with retained import provenance. Map richer data only to suitable
   current entities/work/finance structures; uncertain columns stay in the archive
   with an explicit unresolved disposition. Google Tasks is not a dumping ground
   for arbitrary tables. No direct production row edits, historical-root rewriting
   or unapproved backfill of claimed work/payments.
5. Review unresolved unique data before discarding its only copy. Prove imported
   counts and sampled field relationships; keep legitimate private attachments
   under existing evidence/retention controls. The archive is historical data,
   not a supported running NocoDB deployment or legacy database API.
6. Remove the Docket evaluation's stopped app/worker/support containers, dedicated
   networks, deploy/start recipes, packages, mounts, active ingress, service/timer
   definitions, credentials and database access. Retire old files if they still
   exist outside tracked source. Keep no dormant NocoDB service flag, SQL view,
   compatibility ID resolver, write-through adapter or dual-write path.
7. Remove dedicated persistent storage/images only after the archive/disposition
   check and consumer inventory prove it safe. Never use broad Docker prune or
   remove `docket-postgres-1`/production volumes, unrelated workspaces or unrelated
   browser/evaluation resources. A retained private backup is not a compatibility
   layer; deleting historical recovery copies is not necessary to sunset software.
8. Re-run inventory and cold-start checks. No NocoDB runtime, listener, scheduled
   restart, integration token, database access or supported operational dependency
   remains. Preserve immutable historical documents/commits without treating their
   old instructions as current setup. Record disposition in the release evidence.

Remove the fifteen hidden native MCP aliases in the same clean-break release:
delete compatibility registration/dispatch and alias-specific docs/tests. Replace
their current instructions with shared retraction, task update and general context.
Old tool names return an unsupported-tool error before any canonical/provider
effect. Do not implement aliases under a deprecation flag or translate old payloads.
Historical call names, contract hashes, receipts and completed changes stay genuine
and readable; they do not authorize executing a retired tool or stale saved draft.

## 12. Workload, reminders and exception reporting

Include scheduled task days, true due times, canonical task state, list membership
and explicit synchronization coverage in existing workload context. Count each
canonical Task once, even if seen in Google Calendar, a Google Tasks observation
and Docket. Scheduling alone supplies neither effort nor a busy interval.

Keep existing task/deadline reminder behavior and extend typed subjects only where
needed; no fabricated Calendar Event or `due_by` deadline to make reminders work.
Extend the existing `docket_schedule_followup` command to accept Task subjects,
including typed reminder storage/validation, cancellation and current-state checks, instead
of adding another public reminder tool. A requested 5 PM follow-up preserves its
offset/zone and uses Docket's retained inbox and configured delivery route. An inbox
entry becoming due is not proof of push delivery or a Google-native alarm; native
Google time selection remains a UI action while the API cannot express it.

Google Deadline creation/sync is excluded from this integration. Preserve existing
independent Docket deadlines, without requiring one for ordinary tasks or reminders.
Do not promise Google-native reminder or recurring-rule editing absent API support.
Completion can stop task-specific pending reminders without altering genuine
work/finance obligations, and reopening uses versioned deterministic reconciliation.

Retain actionable sync exceptions in the existing inbox: authorization needed,
stale selected-list coverage, field conflict, uncertain create, unavailable list
or provider limitation. Deduplicate and respect preferences/cooldowns. Routine
successful polls should be quiet. Optional MCP events derive from durable accepted
changes and retain the separate `docket.events` permission; subscription delivery
is not authority and never justifies an inference loop inside Docket.

## 13. Delivery milestones

| Milestone | Deliverable and completion condition |
| --- | --- |
| R0 — retirement inventory | Identify NocoDB-owned resources and unique data; produce protected export/disposition and a precise removal list; no runtime fallback |
| G0 — adapter contract | Scope upgrade/readiness and real/fake/disabled Tasks protocols; retain real method-specific date/null/time-preservation, ETag, move/delete/completion and unknown-insert evidence. Passing evidence for each operation is required before activating that write |
| G1 — task read and import foundation | First widen the full Task/Item title path to 1,024 and establish derived-Item title ownership; then typed lists/observations/bindings/sync state, bounded queries and selected-list imports through admitted services; full pagination, owner isolation and no provider side effects from ordinary reads |
| G2 — useful two-way tasks | Flat create/edit/complete/reopen, scheduled dates, explicit deletion policy, marker-safe note reconciliation and deterministic conflicts; each activated write passes G0 and ordinary Google UI edits converge without per-edit approval |
| G3 — hierarchy and integrated planning | Parent/sibling/cross-list moves with provider constraints, cancellation/retraction boundaries, workload/reminder integration and meaningful selective inbox exceptions |
| R1 — clean-break cutover | Migrate verified unique NocoDB data, remove runtime/access residue and fifteen MCP aliases, verify cold start with none present; current refs/history remain |
| A — activation | Local tested-image validation, supported deploy, same private plugin update/rescan and real host acceptance with an expressly designated Tasks test target |

R0 inventory and G0 validation can proceed independently. G1 requires the complete
title-path fix before import activation; subsequent G2/G3 writes activate only as
their G0 evidence passes. R1 waits for the data-disposition checks, not another
broad architectural redesign.

G2 is the first everyday-use milestone. Do not label read-only list enumeration
as complete Google Tasks support. G3 and R1 are required for this delta's full
completion; unsupported first-party recurrence/assignment writes and bulk list
administration are explicit exclusions, not silently advertised future tools.
An advertised required G2/G3 operation that remains disabled is incomplete work;
do not weaken its concurrency requirement or redefine the milestone to claim done.

## 14. Acceptance evidence

Prefer focused end-to-end journeys and actual PostgreSQL races over duplicated
schema wording/count tests. Use fake providers and synthetic accounts by default.

| Area | Required proof |
| --- | --- |
| Scope isolation | Adding Tasks preserves existing Google scopes; Tasks revocation leaves local tasks and Calendar/Gmail usable; read/triage credentials cannot configure or mutate |
| First valid call | Context then flat task creation produces one Task, necessary Item and one Google intent; unchanged retry returns the same receipt; no Event is created |
| Scheduling and reminders | Date-only values survive zone/DST changes; Google Deadline is unused and no Docket deadline is fabricated for a task/reminder. Existing real deadlines remain unchanged; an explicit 5 PM Docket follow-up retains its instant and truthful delivery status without claiming a Google-native time write |
| Completion | Google UI completion/hidden completion imports once; Docket completion reaches Google; reopening works; blocked/in-progress and existing work links survive ordinary open observations |
| Coverage | Multi-page scans include hidden/completed/deleted records; failed/truncated scans cannot imply deletion; watermark overlap and restart recover without skipping changes |
| Reconciliation | External one-sided/disjoint edits converge; divergent same-field edits and delete-versus-edit retain both bases and block only affected work; echo loops do not occur |
| Provider activation/races | Missing/failed G0 evidence prevents the corresponding HTTP write; real method guards reject stale patch/delete/move or a valid constrained strategy is proven. Concurrent local edits/imports cannot overwrite the pinned basis; unverified move/delete and scheduled-time preservation cannot inherit a patch checkbox |
| Uncertain creation | Crash/timeout after insert never causes a blind second insert; exact/ambiguous/no-match cases preserve the original intent and honest unresolved status |
| Correlation versus notes | Recognized transport markers never enter canonical user notes/exports/baselines; marker-only changes do not conflict or echo. Edits preserve one intended marker, lookalike user text survives, absent-marker outcomes remain uncertain where identity cannot be recovered, and combined provider notes respect the size limit |
| Identity and hierarchy | Cross-list moves preserve canonical refs and WorkSession links; verified provider identity changes update binding generations; duplicates, cycles and unobserved child cascades are prevented |
| G1 title path | Import a 1,024-character Google title without an existing Item; the required Item and Task persist the full text, and context/export plus subsequent sync preserve it. Cover create/patch inputs and canonical mutation validation, not only Task storage |
| Derived Item title ownership | A Google Task rename updates its exclusively owned derived Item atomically; an independently titled/shared/relinked Item stays untouched. Concurrent Item-title edits cannot be overwritten, and unmarked historical pairs are not auto-adopted from matching names |
| Other field limits | Oversized notes and unsupported assignments remain useful and visibly limited rather than truncated or falsely delivered |
| Lifecycle policy | Configuration text/context/receipt disclose Google-delete → canonical-retraction. Completion, confirmed deletion, detachment and disabling have the four distinct consequences specified above; in-flight outcomes remain honest and history/work links survive |
| Local usefulness | Google outage, missing optional data, one conflict and a disabled list cannot block unrelated tasks, work recording, finance or Calendar/Gmail |
| NocoDB retirement | Protected export/disposition covers unique data; owned runtime, startup paths, access and dependencies are gone; no broad prune, production reset or compatibility service; cold start passes |
| Alias retirement | All fifteen old names are unregistered/rejected without side effects; current replacements work and old receipt/history reads preserve original identity |
| Contracts | Measure actual six new/modified definitions and aggregate discovery; first-attempt MCP journeys use visible flat fields; packet/export continuation stays within budgets |
| PostgreSQL | Fresh/additive populated migration rehearsal, uniqueness/ownership/history guards, concurrent import/link/write/version conflicts and restart/drain behavior pass |
| Release/host | Source-bound local gate, affected provider/storage/release checks, tested image, drain/backup/migration/health; same App/plugin rescan and successful real task-context/query call |

Do not create disposable tasks in the operator's real lists under routine release
authority. Live mutation checks need an expressly designated disposable list/task
and narrowly scoped cleanup. State exactly which real-provider behaviors are
verified and which remain fixture-only. A deployment receipt is not proof of
Google UI round-trip, and a skill publication is not proof of App tool rescan.

## 15. Storage, recovery and SPEC reconciliation

Use ordered additive migrations for the full Task/Item 1,024-character title path,
audited derived-title ownership, Task reminder subjects, typed scheduling/hierarchy, policies,
observations, bindings/baselines/conflicts and explicit Tasks operation constraints.
Reuse current versions, public refs, immutable revisions/evidence and atomic
canonical/provider transactions. PostgreSQL is authoritative for uniqueness,
locking and retained-source immutability; SQLite checks do not prove races.

Recovery disables affected Tasks sync without touching canonical data, retains
queued/uncertain operations, and uses the prior tested image/backup or forward
repair according to the migration. Do not reintroduce NocoDB, old MCP aliases or
dual writes as rollback mechanisms. Frozen history/migrations and private recovery
archives stay intact; no destructive database reset or speculative row repair.

On adoption, advance SPEC to 1.2 and reconcile sections 2–6 and 8, including the
bounded provider-import authority root, scheduled-day/deadline mapping, typed
Tasks delivery/recovery and explicit alias retirement. Add decisions for one
canonical task model with a Google interface, deterministic selected-list sync,
and complete NocoDB/compatibility retirement. Keep local CI/release requirements.
Update registries/history/context, native capability disposition, real schemas,
generated contracts, conditional plugin workflow guidance and operational runbooks.
Publish only implemented tools and rescan the existing registered App; create no
replacement plugin or parallel master specification.
Treat callable-alias removal as an intentional breaking change in contract/plugin
release notes and versioning. Do not replace the removed names with versioned
compatibility endpoints. This proposal itself changes no published wire version.

## 16. Implementation starting points

- [Canonical Task](../src/docket/models/tracked_context.py),
  [task inputs](../src/docket/schemas/tracked_context.py),
  [flat task commands](../src/docket/schemas/native_commands.py) and
  [derived Item/due-time compilation](../src/docket/services/routine_commands.py).
- [Existing follow-up inputs and subject validation](../src/docket/schemas/followup.py),
  extended for explicit Task reminders without creating another public tool.
- [Provider intents](../src/docket/services/provider_intents.py),
  [operation runner](../src/docket/services/operations.py),
  [Operation/sync storage](../src/docket/models/calendar.py),
  [Google provider factory](../src/docket/providers/google/factory.py) and
  [OAuth profiles](../src/docket/providers/google/oauth.py).
- [Native contexts](../src/docket/services/native_context.py),
  [native workflow contracts](../src/docket/schemas/native_workflows.py),
  [current alias definitions](../src/docket/schemas/business.py),
  [native registration](../src/docket/plugin/server.py),
  [workload context](../src/docket/services/workload_context.py) and
  [supported Compose](../docker-compose.yml).
- [Current release procedure](../CONTRIBUTING.md),
  [plugin setup/rescan](../docs/plugin-setup.md) and
  [preceding accepted release evidence](../evidence/work-attribution-release/activation.json).

Drafting used current repository files, read-only container metadata and linked
official Google API/UI documentation. No personal task rows, OAuth secrets or NocoDB
database contents were read; no provider effects, migration or retirement ran.
