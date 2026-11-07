# Docket: Google Tasks synchronization and NocoDB retirement

Draft timestamp: 2026-11-06 17:59:26 UTC
Status: adopted by Operator implementation goal; implementation in progress
SPEC baseline: [1.1](../SPEC.md)
Inspected source: `8488f165efc100436813f1bebda7d014dcd5846c`
Adopted SPEC target: [1.2](../SPEC.md); [implementation evidence](../docs/google-tasks-implementation.md)

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
capability from its date-only public API. For an operator-requested timed alert,
automatically create a linked Google Calendar reminder through the existing
Calendar integration. The Task remains the canonical work/checkbox record; the
Calendar entry is its explicit reminder projection. Existing Docket deadlines
remain valid, but neither creating a task nor requesting a reminder requires one.
This is the Operator-accepted alternative to manually editing each Google task.

The architectural review is incorporated below: the complete Task/Item title
path, ownership of automatically derived Item titles, explicit deletion-policy
consequences, transport-only creation markers and per-operation G0 activation
evidence. The subsequent Operator implementation goal adopts the reviewed scope
into SPEC 1.2; implementation and provider acceptance are tracked separately.
The revised review adds three concrete G2 integration requirements: private
visibility through rendering/reconciliation, persistent reminder-origin exclusion
from cached workload observations, and instant-based one-minute DST arithmetic.
Device notification acceptance remains separate from successful provider creation.

The Operator additionally requests a clean break without legacy compatibility.
Remove the fifteen hidden native MCP compatibility aliases retained by the prior
catalogue consolidation and visibility extension as part of this release. Keep their underlying domain
capabilities through current commands. Preserve genuine historical receipts and
readable evidence, without callable aliases, payload translators or old-tool replay.
This deliberately supersedes SPEC 1.1's standing cached-client alias requirement
on adoption. It does not retire legitimate canonical Tasks or their public refs.

This forward delta is not a second master SPEC. Its original drafting performed
no provider/runtime changes. The Operator implementation goal adopts its scoped
requirements; SPEC, decisions/history and the index are reconciled together. Existing development
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
| Calendar reminder rendering | `_standalone_event_body` emits transparency but not visibility; normalized snapshots/reconciliation also omit visibility; `_google_reminders` already supports a zero-minute popup | Carry and verify explicit private visibility through the entire write/read path; reuse popup encoding |
| Calendar workload/cache | Actual-work displays have exclusions; reminder companions have no typed origin persisted across refresh in `CalendarEventCache` | Retain validated reminder classification through synchronization and filter both canonical and cached workload paths |
| Timed marker endpoints | `_temporal_timing` adds duration to local wall time; the renderer can apply one fold to both endpoints | Build reminder start/end from unambiguous instants and preserve independent endpoint offsets |
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
| “Remind me about the report Friday at 5 PM.” | Google Tasks receives the task/day; Docket automatically creates one linked Calendar reminder at 5 PM, without a deadline or manual per-task time editing |
| “Move that reminder to Monday at 4 PM.” | Update the logical reminder and its Calendar projection, retaining the Task and prior history; do not create a second task |
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

Google Calendar separately supports timestamped events and
[popup reminder offsets, including zero minutes](https://developers.google.com/workspace/calendar/api/v3/reference/events).
Use that supported path for automatic timed alerts. A Calendar companion is a
separate visible reminder entry, not a Tasks-native scheduled clock or an invented
Google Task feature. Its lifecycle is specified in section 12.

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

Configure a reminder Calendar destination once, using the owner's existing
Calendar routing/default or a flat `reminder_calendar_ref` on task-list policy.
The policy/context states that requested timed reminders create entries there.
Use an existing authorized calendar; provisioning a dedicated calendar is an
explicit configuration action, not a side effect of every task. Missing Tasks
permission does not invalidate existing Calendar permission, or vice versa.
The admitted policy also scopes clock/deletion reconciliation to exact owned
reminder companions. It grants no authority over unrelated Calendar events or
other lists; explicit native reminder edits retain their own current write grant.

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
| Timed reminder | Typed Task-owned reminder plan with an exact instant/zone, exposed as `remind_at` | Derive one linked Calendar projection and its popup; no Tasks-native time/Deadline write is claimed |
| Open status | not_started / in_progress / blocked | All project to `needsAction`; an unchanged Google open status does not reset Docket's richer state |
| Completion | completed plus exact completion basis | Project/import completion; compare at Google's observed millisecond precision while preserving full canonical reported evidence. Repeated observations do not rewrite completion time |
| Reopening | Nonterminal canonical state | A genuine completed → open transition reopens; default to not_started, while retaining history |
| Priority, project, Item, work links | Existing canonical relationships | Stay local; Google absence is not a request to clear them |
| Parent/list/order | Typed parent plus provider binding and observed order | Use supported move semantics; validate same-list parent/sibling, descendants and cycles |
| Deleted/hidden | Distinct lifecycle observations | Hiding preserves completion/history; confirmed deletion is handled separately below |

Scheduling a task's day alone does not allocate a duration, reserve capacity,
create a Calendar entry or set a financial reporting deadline. An explicitly
requested timed reminder creates the section 12 Calendar companion. The normal
Google task path uses scheduled/display dates and leaves Google's Deadline feature unused. Do not
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
The Operator selected Google's normal subtree semantics on November 6 after live
validation showed that completing/deleting a parent cascades and adding a child
does not change the parent's ETag. A parent completion/deletion request therefore
authorizes that provider task's current subtree, including children added since
the last Docket read. Reflect known canonical descendants through admitted,
versioned services and retain actual provider observations for later-discovered
children. Native parent moves likewise carry their subtree without copy/delete.
Describe this standing behavior in task context, previews and receipts; add no
recursive input flag or repeated approval prompt. It grants no authority over
unrelated tasks, other lists, WorkSessions or financial records. Preserve stable
canonical references/history and honest outcome reconciliation when concurrent
external moves change subtree membership.

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
| Linked Calendar reminder | Reuse verified Calendar primitives and prove the new Task/plan/projection flow: exact-time popup, correlation/retry, stale-source rejection, reschedule and cancellation after uncertain delivery |

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

Add `task_list_ref`, `scheduled_date`, `parent_task_ref`, `after_task_ref` and `remind_at` as
optional flat fields to existing task create/update. A configured default supplies
the list for new tasks; an omitted update list preserves its binding. Explicit
null detaches without deleting either record. A different non-null list moves a
bound task or publishes an unbound local task, derived internally. An omitted
ordering field preserves order; explicit parent/sibling changes have documented
null behavior and field-specific errors. The agent never supplies positions,
ETags, cursor timestamps, Google IDs, Operation parameters or baseline graphs.

`remind_at` is one optional flat ISO timestamp, using the existing timezone
normalization rules. On update, omission preserves the reminder, a value schedules
or reschedules the primary timed reminder, and null cancels it. Docket derives
the plan, source-time record and Calendar projection internally. Task-list
configuration adds only the optional flat `reminder_calendar_ref` destination;
normal task calls use that/default routing rather than asking for it every time.
No public reminder-operation union, second task-create tool or companion-event
CRUD sequence is required. Include these modified definitions in the same budgets.

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

Keep existing independent deadline/work/finance reminder behavior. The ordinary
task path uses `remind_at` to automate a Google Calendar companion; it does not
need a new public tool or the previously proposed Task-subject expansion of
`docket_schedule_followup`. Preserve that command's existing domain behavior.
Google Deadline creation/sync and Tasks-native reminder/recurrence editing remain
excluded. No fake deadline or fabricated attendance is needed to express an alert.

### Linked Calendar reminders

For a request such as “remind me about the report Friday at 5 PM,” the connected
agent supplies the task/day and `remind_at` through the normal command. Once the
existing Calendar target is configured, no per-task manual Google UI editing is
required. Creation commits the canonical Task, necessary Item, typed primary
reminder plan, linked projection and required Tasks/Calendar provider intents in
one transaction. Each provider delivers afterward and has its own receipt status.
Task identity never depends on the companion Event or successful Google delivery.

Reuse ReminderPlan, typed time/projection services and Calendar Operations where
their semantics fit; add only the typed Task/primary-reminder/projection ownership
needed. Keep one authoritative reminder instant, not independently editable copies
in Task, ReminderPlan and Event. Preserve immutable plan revisions when replacing
or rescheduling. Retain stable logical companion identity and source versions;
at most one active primary projection may exist for a Task's primary reminder.

The Calendar entry is explicitly labelled as a task reminder, uses no attendees,
and requests a popup at zero minutes before the requested instant. Use private
visibility and `transparency="transparent"` through supported provider fields.
If the Calendar API needs a span, use a documented one-minute display span; it
does not record work duration or reserve time. Exclude these entries, including
cached provider observations, from commitments, free-capacity subtraction,
attendance inference and actual-work/financial totals. A date-only task creates
no companion. The helper is a real requested reminder projection, not a new task,
meeting or Tasks-native scheduled-time claim.

The following are mandatory G2 implementation corrections, not features already
provided by the current Calendar infrastructure:

- Carry `visibility="private"` from the owned reminder specification through
  provider-intent parameters and `_standalone_event_body` into the actual Google
  create/patch body. Include visibility and transparency in applicable read-field
  selections and normalized snapshots; carry visibility through
  `normalize_event_body`, `event_matches_request` and uncertain-outcome reconciliation. Private
  visibility and free transparency are independent properties. An intermediate
  specification value cannot prove the serialized request or observed event is
  private; missing/different observed visibility must not pass a required privacy
  check. Reuse `_google_reminders` for `useDefault=false` and the zero-minute popup.
- Give each companion a stable typed Docket origin, for example
  `origin_kind="task_reminder"`, plus its logical projection/owner binding. This
  is application origin metadata, not a new Google `eventType`. Propagate it through
  provider snapshots, synchronization read models and validated cache storage.
  `CalendarEventCache` needs the small typed classification/link fields required
  to preserve it across refresh, new snapshot generations and process restart.
  Exclude owned reminders in both canonical and provider-observed paths of
  `WorkloadCalendarService`, before commitment/count/interval-union/capacity work.
  Do not depend solely on an in-memory join, title prefix, transparency or an
  unverified copied private property. Validate against the scoped owned binding
  or retained operation identity; ordinary events retain their existing policy.
- Resolve the reminder start to an unambiguous UTC instant, add exactly 60 elapsed
  seconds, then serialize each endpoint with its independently resolved offset
  (or UTC). Retain the IANA zone for display/context. Do not add a minute to naive
  wall time or apply one shared fold to both endpoints. In `America/Los_Angeles`,
  `2026-11-01T01:59:00-07:00` must end at `2026-11-01T01:00:00-08:00`:
  08:59Z → 09:00Z, exactly 60 seconds, despite the local clock moving backward.
  The reminder path must accept those ordered instants without requiring the
  naive local endpoint to be later. Cover actual rendered and reconciled endpoints,
  not only an isolated duration helper. Keep this fix scoped to the reminder/timed
  marker path rather than redesigning general Calendar recurrence or timing.

  November 6 live validation found that Google returns `timeRangeEmpty` for
  the exact local-offset fall-back pair above, including UTC `dateTime` values
  accompanied by the Los Angeles `timeZone`. UTC endpoints with `timeZone=UTC`
  succeed and read back correctly. Use that transport when local wall time moves
  backwards; retain the original IANA zone in canonical context. This applies
  the permitted UTC representation rather than changing the reminder instant.
  [Provider evidence](../evidence/google-tasks-retirement/live-provider.json)
  records the rejected/accepted encodings and cleaned-up private/free popup tests.

Preserve full 1,024-character Task/Item titles. A Calendar helper label can be a
bounded, clearly labelled display excerpt with a stable task reference; it must
not force truncation of the canonical/imported title to fit a narrower Event/cache
title field. Do not automatically copy all task notes or linked private records
into a possibly shared calendar. The task context remains the full detail source.

The Operator's November 6 amendment uses an AM/PM header in the Google Task title
for visible clock time, alongside the API scheduled day. Render a known `remind_at`
as `5:00 PM — Task title`; include its local date if it differs from scheduled day.
This is derived transport display, with a scoped retained prefix/binding basis.
Keep canonical Task/derived Item titles clean, preserve Google-side body renames,
and never parse arbitrary clock text into performed work, paid work or a reminder.
Changing/clearing the reminder must update/clear the generated header. Full titles
remain lossless; if the prefix cannot fit Google's 1,024-character limit, omit it
and expose the display limitation while keeping the Task and Calendar alert valid.

A controlled native-clock sample lost its UI time after sparse title/notes and
day edits. Do not require more opaque-clock setup or promise its preservation.
Exact alerts continue through the automatic Calendar companion; the title label
is visible context, not notification configuration. Native clock support remains
outside the public API contract. No new input field or operation grammar is needed.

Changing `remind_at` updates the owned companion or uses an ordered, versioned
replacement when necessary; never clone the Task or leave two active alerts.
Null cancels the reminder and queues companion cancellation. Task completion,
cancellation or retraction, including accepted Google-side changes, retires its
pending primary reminder and atomically records required Calendar cancellation.
Reopening does not resurrect an expired/already delivered alert; a new requested
time is explicit. Changing the scheduled day alone does not silently reinterpret
an independently specified reminder instant; a requested reminder move supplies
the new instant through `remind_at` and preserves its zone/precision.

Canonical task changes remain saved while Calendar cancellation/delivery is
pending, failed or uncertain. Retry/reconciliation follows the original operation,
and queued stale creates cannot resurrect a retired reminder: dispatch checks its
current reminder/projection generation and prerequisites. Completion learned from
Google is subject to polling latency; do not promise cancellation of a notification
that already fired before that observation or before Calendar accepted cancellation.

Only exact linked Calendar entries participate in reminder reconciliation. Apply
the same baseline/version rules to their observed clock changes; they affect the
reminder, not the Task deadline, scheduled day, title or work history. Divergence
is a reminder-only conflict. A confirmed companion deletion cancels that reminder,
preserves the Task and does not trigger automatic recreation. Arbitrary Calendar
events acquire no task authority. Guard direct Docket Event editing of managed
companions so it cannot bypass their typed reminder lifecycle.

Disabling task-list sync preserves already created Calendar entries; it cannot
silently mute a notification that lives in Google. Context/configuration explains
this, and explicit reminder cancellation remains a separate authorized action.
Calendar scope/target failure leaves the valid task and reminder intent retained
with an actionable delivery state. Never ask the operator to enter each time
manually as the normal fallback, or silently switch notification channels after
an uncertain Calendar outcome.

Use one default push route for a requested timed task alert. Retained Docket inbox
status/catch-up remains available, but do not also publish a duplicate Docket push
unless requested. Distinguish committed reminder intent, Calendar acceptance and
actual notification/read acknowledgement. No mandatory `docket.events` grant is
needed for the Calendar popup route; normal Google Calendar permissions apply.

Before declaring timed reminders accepted on the operator's intended device,
perform one separately recorded end-to-end notification check using the designated
test target. Record the client/device, test zone, requested instant, API acceptance
and whether/when the notification was actually observed. Google's
[notification settings and client permissions](https://support.google.com/calendar/answer/37242)
affect display. A 2xx create, event read-back or popup override is API evidence,
not proof of notification display; report device acceptance as pending if it has
not been observed. Do not promise guaranteed display or treat settings changes as
a reason to redesign task synchronization. This is a focused client check, not a
new browser-automation dependency or recurring per-task manual configuration.

Google Tasks may separately issue its own first-party date-only notifications.
Expose that behavior once in setup; do not claim that creating a Calendar popup
suppresses Tasks-app alerts or lets the adapter configure an opaque native clock.
Any app-level notification preference is distinct from manual per-task time entry.

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
| G2 — useful two-way tasks | Flat create/edit/complete/reopen, scheduled dates and automatic `remind_at` Calendar companions; require serialized/reconciled private visibility, cached typed-origin workload exclusion and 60-elapsed-second DST-safe markers. Explicit policy/marker/conflict semantics remain; each write passes G0 and device notification evidence is separate from API acceptance |
| G3 — hierarchy and integrated planning | Parent/sibling/cross-list moves with provider constraints, cancellation/retraction boundaries, workload/reminder integration and meaningful selective inbox exceptions |
| R1 — clean-break cutover | Migrate verified unique NocoDB data, remove runtime/access residue and fifteen MCP aliases, verify cold start with none present; current refs/history remain |
| A — activation | Local tested-image validation, supported deploy, same private plugin update/rescan and real host acceptance with expressly designated Tasks and reminder-Calendar test targets |

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
| First valid call | Context then flat date-only task creation produces one Task, necessary Item and one Tasks intent; unchanged retry returns the same receipt; no reminder Event is created without `remind_at` |
| Scheduling and reminders | Date-only values survive zone/DST changes; Google Deadline is unused and no Docket deadline is fabricated. A flat 5 PM `remind_at` request atomically records the Task, owned reminder/projection and separate Tasks/Calendar intents; one private/free Calendar companion has a zero-minute popup and truthful delivery state, without manual per-task setup or a Tasks-native clock claim |
| Reminder lifecycle/races | Retrying and competing schedules cannot duplicate the primary companion. Rescheduling uses current source versions; completion/null/retraction cancels it, including unknown-after-create recovery. Old queued writes cannot resurrect it; Task/work/finance records survive, reopening never replays a past alert and Calendar/Tasks failures remain separate |
| Reminder planning/privacy | Canonical/cache workload and capacity exclude reminder display spans. Calendar labels can be bounded while Task/Item titles stay lossless, including a 1,024-character title with `remind_at`. No attendee invite, copied private note or duplicate default Docket push is created; owned-helper edits/deletion affect only its reminder |
| G2 private renderer | Follow a companion through intent compilation and real request serialization: explicit `visibility=private`, `transparency=transparent`, `useDefault=false` and a popup override at zero minutes. Read-back/snapshot/reconciliation must retain and compare required visibility; missing/public visibility cannot falsely match a private request |
| G2 cached origin/workload | Create the owned companion, perform Calendar refresh/new cache generation and restart/read from persisted observations. Its validated typed origin/link survives and both canonical/cache paths exclude it from commitment counts and capacity. A normal transparent event or copied reminder-looking title/property is not blanket-excluded; existing actual-work exclusions remain |
| G2 DST marker | At the specified Los Angeles fall-back boundary, start/end are 08:59Z/09:00Z, representing local -07:00/-08:00 offsets and exactly 60 elapsed seconds. Google transport uses UTC when the local wall-clock range is rejected; canonical IANA zone remains. Shared-fold/wall-time arithmetic must fail the regression; include a spring-forward boundary and real provider read-back |
| Completion | Google UI completion/hidden completion imports once; Docket completion reaches Google; reopening works; blocked/in-progress and existing work links survive ordinary open observations |
| Coverage | Multi-page scans include hidden/completed/deleted records; failed/truncated scans cannot imply deletion; watermark overlap and restart recover without skipping changes |
| Reconciliation | External one-sided/disjoint edits converge; divergent same-field edits and delete-versus-edit retain both bases and block only affected work; echo loops do not occur |
| Provider activation/races | Missing/failed G0 evidence prevents the corresponding HTTP write; real method guards reject stale patch/delete/move or a valid constrained strategy is proven. Concurrent local edits/imports cannot overwrite the pinned basis; unverified move/delete and scheduled-time preservation cannot inherit a patch checkbox |
| Uncertain creation | Crash/timeout after insert never causes a blind second insert; exact/ambiguous/no-match cases preserve the original intent and honest unresolved status |
| Correlation versus notes | Recognized transport markers never enter canonical user notes/exports/baselines; marker-only changes do not conflict or echo. Edits preserve one intended marker, lookalike user text survives, absent-marker outcomes remain uncertain where identity cannot be recovered, and combined provider notes respect the size limit |
| Identity and hierarchy | Cross-list moves preserve canonical refs and WorkSession links; verified provider identity changes update binding generations. Prevent duplicates/cycles and effects outside the authorized subtree; parent completion/deletion follows the Operator-selected current-subtree semantics, including newly observed children |
| G1 title path | Import a 1,024-character Google title without an existing Item; the required Item and Task persist the full text, and context/export plus subsequent sync preserve it. Cover create/patch inputs and canonical mutation validation, not only Task storage |
| Derived Item title ownership | A Google Task rename updates its exclusively owned derived Item atomically; an independently titled/shared/relinked Item stays untouched. Concurrent Item-title edits cannot be overwritten, and unmarked historical pairs are not auto-adopted from matching names |
| Other field limits | Oversized notes and unsupported assignments remain useful and visibly limited rather than truncated or falsely delivered |
| Lifecycle policy | Configuration text/context/receipt disclose Google-delete → canonical-retraction. Completion, confirmed deletion, detachment and disabling have the four distinct consequences specified above; in-flight outcomes remain honest and history/work links survive |
| Local usefulness | Google outage, missing optional data, one conflict and a disabled list cannot block unrelated tasks, work recording, finance or Calendar/Gmail |
| NocoDB retirement | Protected export/disposition covers unique data; owned runtime, startup paths, access and dependencies are gone; no broad prune, production reset or compatibility service; cold start passes |
| Alias retirement | All fifteen old names are unregistered/rejected without side effects; current replacements work and old receipt/history reads preserve original identity |
| Contracts | Measure all six new definitions, every modified task/list definition and aggregate discovery; `remind_at`/routing remain optional flat fields and no companion-event grammar is exposed. First-attempt MCP journeys and packet/export continuation stay within budgets |
| PostgreSQL | Fresh/additive populated migration rehearsal, uniqueness/ownership/history guards, concurrent import/link/write/version conflicts and restart/drain behavior pass |
| Release/host | Source-bound local gate, affected provider/storage/release checks, tested image, drain/backup/migration/health; same App/plugin rescan and successful real task-context/query call |
| Intended-device notification | One designated end-to-end timed reminder is actually observed on the intended client/device and recorded separately from API acceptance. Record pending/failed display honestly; successful event creation alone cannot satisfy this check or guarantee future notification display |

Do not create disposable tasks in the operator's real lists under routine release
authority. Live mutation checks need expressly designated disposable list/task
and Calendar reminder targets with narrowly scoped cleanup. State exactly which real-provider behaviors are
verified and which remain fixture-only. A deployment receipt is not proof of
Google UI round-trip, and a skill publication is not proof of App tool rescan.

## 15. Storage, recovery and SPEC reconciliation

Use ordered additive migrations for the full Task/Item 1,024-character title path,
audited derived-title ownership, typed Task-owned reminder/projection links, scheduling/hierarchy, policies,
observations, bindings/baselines/conflicts, persisted validated Calendar origin
classification and explicit Tasks operation constraints. Extend synchronization
read models and cache migration deliberately; do not infer historical origin from
titles or reinterpret Google's ordinary `eventType` as Docket authority.
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
Tasks/linked-Calendar delivery and recovery and explicit alias retirement. Add decisions for one
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
- [Existing reminder and temporal projection inputs](../src/docket/schemas/tracked_context.py)
  and [Calendar provider rendering](../src/docket/providers/google/calendar.py)
  for the derived Task reminder path; keep existing independent follow-up behavior.
- [Calendar synchronization](../src/docket/services/calendar_sync.py) and
  [canonical/provider workload capture](../src/docket/services/workload_calendar.py)
  for persisted origin classification and exclusions after cache refresh.
- [Provider intents](../src/docket/services/provider_intents.py),
  [operation runner](../src/docket/services/operations.py),
  [Operation/sync storage](../src/docket/models/calendar.py),
  [Google provider factory](../src/docket/providers/google/factory.py) and
  [OAuth profiles](../src/docket/providers/google/oauth.py).
- [Native contexts](../src/docket/services/native_context.py),
  [native workflow contracts](../src/docket/schemas/native_workflows.py),
  [typed retraction dispatch](../src/docket/schemas/business.py),
  [native registration](../src/docket/plugin/server.py),
  [workload context](../src/docket/services/workload_context.py) and
  [supported Compose](../docker-compose.yml).
- [Current release procedure](../CONTRIBUTING.md),
  [plugin setup/rescan](../docs/plugin-setup.md) and
  [preceding accepted release evidence](../evidence/work-attribution-release/activation.json).

Drafting used current repository files, read-only container metadata and linked
official Google API/UI documentation. No personal task rows, OAuth secrets or NocoDB
database contents were read; no provider effects, migration or retirement ran.

### Live acceptance clarification — Calendar point observations

The iPhone-created notification control returned a confirmed timed Calendar event
with equal start and end instants. Provider observations must retain that valid
point without inventing a duration or making the entire Calendar generation stale.
This is a reader/cache rule: canonical Event creation still requires a positive
interval, and backwards or incomplete provider ranges remain invalid. Typed task
reminder origin and workload exclusion survive the promoted mixed snapshot.

### Implementation clarification — Task exports

The required JSON/CSV export uses one optional flat `format` on docket_query_tasks.
Follow its existing cursor while present and concatenate content in order. Full
canonical titles and notes survive output budgets, including one oversized row.
Each page's chunks use existing connection-owned bounded read snapshots; later
pages are read when requested. This does not promise an immutable whole-query
financial report or add another tool, public payload grammar or storage table.
