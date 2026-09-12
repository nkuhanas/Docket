# Operations runbook

This runbook describes the clean tracked-context runtime. Historical rollout
evidence is retained separately in
[Ontology rollout verification](ontology-rollout-verification.md); retired
Record, Approval, Action, QueueItem, and pre-cutover MCP flows are not runtime
recovery paths.

Never print or paste service tokens, OAuth files, attachment keys, raw Gmail
bodies, authorization headers, or unredacted retained utterances. Prefer typed
public references and bounded history views over internal UUIDs or table dumps.

## Runtime invariants

PostgreSQL is the only durable authority. The live flow is:

```text
authenticated Operator input
  -> immutable utt_ before interpretation
  -> IntentSession / statements / conflict handling
  -> one chg_ canonical transaction
  -> canonical objects + required op_ provider intents
  -> asynchronous provider execution and reconciliation
```

The interactive MCP profile exposes exactly 23 tools. Only
`docket_commit_changeset` and `docket_resolve_conflict` can mutate canonical
state. The isolated triage profile exposes four non-authoritative tools;
`docket_get_attention_case` is the sole shared read.

The principal public vocabulary is:

```text
ent_    persistent Entity       item_   bounded tracked Item
task_   operator work           time_   temporal meaning
evt_    actual occurrence       rem_    notification behavior
case_   AttentionCase           citem_  case component
utt_    Operator evidence       stm_    interpretation
chg_    atomic mutation         op_     provider effect
call_   tool invocation         aud_    semantic audit
```

Provider writes are compiler-owned. An authorized Event or Time projection
ChangeSet must create any required `op_` in the same transaction. Hermes does
not formulate provider intents and must never ask for a second “push to Google”
authorization after an already-authorized canonical mutation.

## First checks

From the repository root:

```bash
scripts/docket status
sudo docker compose ps
sudo docker compose logs --since 15m docket hermes discord-ingress
```

Keep log output bounded. For a reported tool problem, begin with its `call_`,
`trace_`, `ses_`, `chg_`, `op_`, `case_`, or `proj_` reference through the
bounded history tools or trusted internal history API. Do not start by dumping
all utterances or provider payloads.

The Docket tool-activity projection reports exact whole-trace attempt and
authenticated-invocation counts. Its byte-bounded preview shows recent attempts;
the omitted count and `docket_get_history_entry(ref="trace_...", view="calls")`
provide the rest. Stage/review/commit totals always describe the entire trace.
When a wrapper callback is missing, signed invocations still supply the call
row and workflow count. `transport_layer=docket` identifies server processing,
not proof that Hermes received its response; wrapper latency and argument
preview remain unrecorded. Retransmissions share one upstream attempt row while
every authenticated invocation is counted separately. Finalization queues its
own trace refresh instead of depending on a later callback.
Local wrapper rejections are not authenticated Docket invocations. Unlinked
attempts stay unreconciled rather than becoming evidence of domain success.

One `trace_` now spans all admitted executions of its original Discord message.
Each execution retains its own immutable lease/gateway/contract binding and
local call ordinals. `E2.1` means the first call in the second execution, not a
replay of execution one's first call. Inspect
`docket_get_history_entry(ref="trace_...", view="executions")` for bounded
per-execution status, timing and call counts, including zero-call executions.
Use `view="calls"` for the combined call history. Both collections reject stale
revision cursors explicitly. Recovery downtime is not relabeled as model time;
each execution's measured closed intervals remain separate from whole-trace
elapsed time. A late prior-gateway callback cannot reopen the current execution.

Migration `20260912b8f7` moves each previously captured trace's execution fields
into an internal `TraceExecutionSegment`, retaining original references,
timestamps, calls and durable outcomes. These rows are marked `retained_trace`;
no historical completion token/lease is guessed. Exact existing trace links
are preserved, while historical missing links remain unknown. This is a
lossless evidence move, not an old callback decoder or a domain backfill. New
executions require the authenticated bind callback and format-2 signed MCP
envelope. Drain and deploy Docket plus the pinned plugin together; old callback
formats are rejected. Queued projections are rebuilt from durable trace state.
Downgrade is allowed only before an admitted execution exists. Once new work
has run, use forward repair or a verified backup; neither deleting evidence nor
an image-only rollback is a valid recovery procedure.

Local rejection and turn completion now checkpoint the gateway's observations
through the authenticated internal trace endpoint, in pages of at most 25 calls
and 16 KiB. This recovers a dropped callback prefix without inventing missing
calls or waiting for the global telemetry queue. A checkpoint page commits all
of its observations or none, and exact replay is safe after a lost response.
It binds the original captured utterance, trace, gateway and contract. Delayed
start callbacks cannot regress a completed observation. A wrapper-reported
disposition is retained separately from the authoritative Docket disposition;
neither a checkpoint nor a local rejection can manufacture a successful `call_`.

Successful tool calls retain asynchronous telemetry on their normal critical
path. If local-rejection capture is unavailable, the next Docket dispatch waits
for durable checkpoint recovery and reports that specific blocker; it does not
request renewed authorization. Final response persistence remains independent:
a trace-capture failure must not suppress a committed domain result. PostgreSQL
is still the only durable store—there is no local payload spool, historical
backfill, or claim that an unacknowledged observation survived a process crash.

`docket_execution_ms` is the union of closed durable invocation intervals within
the trace window; overlapping calls are not double-counted. Running calls do not
prove uninterrupted execution. `wrapper_elapsed_sum_ms` is a separate aggregate
over all attempts, not a wall-clock partition. `unattributed_ms` is the remaining
trace time, not model time. Closed gateway observations now measure model-request
intervals, context/schema preparation, and local argument validation. These
payload-free observations are immutable and recover through the same bounded
checkpoint path, including turns with no Docket tool calls. Nested/parallel
intervals are attributed once, with Docket execution, local validation, model
requests, then context/schema taking precedence. A phase without observations
is null, not zero. Model-request time includes network/provider response and
the pinned hook-boundary overhead; it is not a claim of pure model compute.
`queue_ms` covers only the initial durable receipt-to-first-interactive-claim
interval when the original source/actor, first claim's gateway and timestamp
order agree. That evidence expands the elapsed window to ledger receipt time.
The current DeferredIngress claim is not used: retries may replace it. Missing
or contradictory evidence leaves queue time null and retains the trace window;
later recovery downtime and Hermes-internal scheduling after the claim remain
unattributed unless separately measured. Provider-wait time remains null without
an explicit waiting observation; provider delivery status does not prove Hermes
was waiting. A large
`before_first_docket_call_ms` identifies a delay, not its cause. Statuses are live
observations; call cursors bind the trace revision and projected invocation
snapshot and explicitly require a restart if either advances. Historical callback
formats are not decoded into the
new contract; coordinated deployment drains active execution first.

Migration `20260911a6d5` adds `trace_timing_observations` with no historical
timing reconstruction. PostgreSQL rejects updates/deletes of observations. A
nonempty table blocks downgrade; preserve it through forward repair or the
verified backup procedure. Do not delete evidence to permit an image rollback.

The gateway now supplies an infrastructure-only signed `invocation_binding` on
MCP dispatch. It binds trace/call/ordinal, captured utterance, gateway lifetime,
tool, argument digest and contract for at most 15 minutes. Docket verifies it
before execution and stores the exact correlation on `call_`, including schema
rejections. It grants no semantic authority and is not persisted or exposed to
the model. Argument-hash/time-window matching has been removed. Missing bindings
on non-gateway service calls leave them uncorrelated; they cannot be adopted by
matching arguments later. Transport retries are distinct authenticated calls
under the original signed trace ordinal; the durable assembly/ChangeSet service
still owns operation replay and canonical idempotency. A running retry cannot
erase a known committed outcome. Invalid bindings require runtime recovery,
not renewed Operator authorization.

Health endpoints distinguish liveness from readiness:

```bash
curl -fsS http://127.0.0.1:${DOCKET_PORT:-8080}/health/live
curl -fsS http://127.0.0.1:${DOCKET_PORT:-8080}/health/ready
```

A ready API does not prove that a provider operation succeeded or that a
Discord projection was delivered. Inspect those durable lifecycles separately.

## Local verification

The required deterministic gate is:

```bash
scripts/docket check
```

Changes to migrations, Compose, authentication, startup, MCP, tool contracts,
providers, or dependencies additionally require:

```bash
scripts/docket compose-smoke
```

The smoke stack uses `.env.example` and `secrets/smoke/`. It must never inherit
the production `.env` or production credentials.

## Tool and contract diagnosis

New staged revisions retain an immutable request interpretation under the exact
`sreq_`/version pair in `semantic_request_specifications`. Its integrity digest
and source hashes do not prove that the proposed interpretation is authorized:
`pending_evidence_validation` remains explicit, separate from draft compilation
readiness. Do not use a mutable or later proposal to justify changed semantics.
New source imports also retain their first, fallible interpretations in the
append-only `request_entry_interpretations` table. The initial assembly scope
contains the COMPLETE `selected_entry_ids` inventory, including later batches
and no-occurrence entries. Each entry's first values are bound to original
utterance/source digests separately from compiled actions. The receipt labels
this `recorded_interpretation`, not independently verified source truth or a new
authority grant. Native vision supplies that interpretation; no second OCR model
is required. The original authenticated instruction remains the authority.

An incomplete inventory remains `saved_with_errors`; stage the remaining selected
entries. Removing an entry clears its owned draft actions but cannot silently
remove it from the request. A date, destination, title, or selection change is
`request_interpretation_conflict`, not mechanical repair. The old draft and
authority survive; surface the exact conflicting reading rather than making a
new request to bypass it. A corrected fragment checksum may be submitted with
otherwise unchanged evidence coordinates/values; the extractor must still verify
it. Compiler-only repair can restage the same entries in a NEW operation and
commit without mandatory review. Replaying an old failed operation returns its
old outcome. Ready revisions still require explicit migration for compiler drift.
No old request is backfilled or executed by migration `20260911e4b3`. A nonempty
specification table blocks downgrade; use a verified backup or forward repair,
never delete evidence to make an image rollback work.

When Hermes appears to use the wrong schema:

1. Confirm the running Docket and Hermes image revisions.
2. Confirm the interactive profile reports 23 tools and triage reports four.
3. Compare the injected contract version, hash, and profile with the generated
   repository artifacts.
   Confirm a namespaced `mcp__docket__docket_stage_changes` description with
   exact `mutation_types` returns the scoped Task/Time field definitions rather
   than Hermes's generic truncated description.
   Commit has no model arguments or direct-content mode; clarification has its
   own noncanonical tool. The gateway binds stage/review/commit/clarification to
   the captured message. A resumed old recipe must be rejected, not translated.
4. Inspect the `call_` lifecycle:
   `transport_state`, `domain_state`, and `result_disposition` are distinct.
5. Reload MCP only after the server and generated contract agree.

Do not probe hidden HTTP endpoints or use terminal access to discover a mutation
shape. The Pydantic/FastMCP schema is the structural contract; the generated
Markdown contract supplies authority, selection, side-effect, and result rules.

Staging returns `ready_to_commit` or `saved_with_errors`. The latter preserves
all entries and action inputs but blocks the entire canonical commit. Repair
the identified entry through a new stage operation in the same request; a replay
of the old operation deliberately returns its old receipt. The bounded preview
shows exact entry totals and omitted counts; optional revision-bound review
provides details. A compiler failure must not lead to a new authorization request
or manual edits of production rows. Pre-cutover drafts without an independent
input snapshot require explicit adoption, not an automatic backfill.

Update payloads distinguish omission from an explicit `null`: omission leaves a
field unchanged; an allowed explicit null clears it. Staged inputs, request
proposals, executable revisions, optional diffs and commit must preserve that
distinction. For example, reopening a completed Task requires the requested
nonterminal state and `completed_at: null` together. Do not use output-oriented
null stripping or materialize omitted patch fields as nulls during compilation.
This correction cannot recover intent already stripped from an old stored patch;
old evidence is not rewritten or executed automatically. Such a correction still
needs the normal evidence-grounded repair/adoption path.

Diagnostic samples are byte-bounded independently of draft validity. A nonzero
`diagnostic_count` with an empty sample requires the receipt's `diagnostic_review`
read, not another commit or a new request. That read's cursor is pinned to the
failed immutable revision, even after later repair. `omitted_diagnostic_count`
counts complete diagnostics absent from the sample; detail pages may contain
lossless fragments for a single oversized diagnostic. Calendar import errors
name the entry, action-relative field path and violated constraint. A title
comparison explicitly names its linked staged Item; it is not independent
source verification and does not authorize guessing a different title.

Optional `docket_review_changeset(view="diff")` reports actual staged field
changes against the preceding immutable draft revision. It distinguishes added,
removed and modified entries/actions, including occurrence/series scope, and
names both revisions. This is a draft-to-draft comparison, not live canonical
or Google state. Separate `canonical_event_effect` rows compare Calendar
presentation fields captured at staging with the planned result: title, time,
location, lane, status, recurrence and scope. Their expected/observed versions
remain fixed even if a concurrent request changes canonical state. A moved
occurrence retains its original identity while showing the moved timeslot.
Uncompiled occurrences are explicitly unavailable, never shown as master
cancellations. Additional non-presentation inputs stay in the ordinary input
diff. Old revisions without a captured preview report it unavailable; reads do
not reconstruct one from live state. Compiler-owned new records appear once
through their source entry.
`canonical_target_effect` rows extend the same staging-time comparison to
Item/Task/Time, Reminder, temporal projection, policy, lane and registry patches.
They contain only affected fields, not whole object profiles or provenance
chains. An explicit null is a clear, not an omitted value. Retraction shows the
type's actual lifecycle effect (for example, disabling a lane is not deleting
it); supersession retires the old assertion while its proposed replacement stays
in the input diff. Same-draft dependencies retain their change ID rather than an
invented future public ref. Case-resolution rows distinguish Operator dispositions
from system-derived `not_pursued`; they do not expose CaseItem payloads or claim
a future Decision timestamp. Unavailable targets and stale case revisions are
explicitly unavailable, not predicted success. Preview availability never
overrides validation or the commit's expected-version checks.
Continue with the same cursor even if another attempt edits the draft. Reading
old pages does not observe that newer revision or permit committing it.

If staging an event change reports `provider_event_binding_required`, inspect its
bounded diagnostic before treating the connection as missing. It identifies an
exact original create operation when one exists and provides `status_read` for
that committed request. Pending/executing creation, uncertain creation, failed
creation, and a confirmed operation missing its binding are distinct recovery
states. Preserve the new draft; never duplicate the initial event or ask for
equivalent authorization. Once the original binding is restored, a new staging
operation with unchanged inputs revalidates the same request. No historical
provider operation is automatically replayed by this diagnostic. Receipt fields
`semantic_request_ref` and `authority_availability_at_operation` describe the
recorded operation, not a current-state guarantee when an old receipt is replayed.

Oversized review details are losslessly split into `json_utf8` fragments with
UTF-8 byte offsets, total bytes and a SHA-256 digest. `logical_detail_count`
counts fields/details; `total_if_known` counts transport rows after splitting.
No first-row exception bypasses the page budget. Cursor format mismatches require
restarting review, not a compatibility decoder. Review is still optional.

Draft revisions also retain an executable pin in their compiler manifest: the
compiler and input-schema versions, input/ownership digest, compiled-effect
digest, execution preconditions and authority binding. Commit checks the
immutable revision under the same transaction lock and executes its stored
effects without rerunning a newly deployed compiler. Current preconditions
still validate. Missing/incompatible pins require an explicit audited draft
migration; a pin mismatch requires revision reconciliation. Neither permits
editing production rows, reauthorizing the same request, or silently changing
the draft. A committed receipt remains replayable without recompilation.

For a draft whose pinned inputs and effects still parse exactly under the current
schemas, a sole stage patch `{"operations":[{"operation":"draft_recompile"}]}`
requests audited recompilation. It cannot carry edits, new authority or expected
versions. Docket requires equality of the typed canonical and provider semantic
projections, except for the exact source-proved title coalescence below. It retains
the old revision and writes a new executable pin and audit.
The receipt sets `observation_required=true`: a fresh bounded summary or the
receipt's first diff page observes its exact still-current revision before
commit. All older attempts remain stale. Migration diff pages include actual
compiler products and executable pins; ordinary stage/commit needs no review.

A semantic difference, missing pin or unsupported input schema leaves the draft
and authority intact. This equality path does not establish whether an earlier
source interpretation was correct and cannot repair a wrong source-derived date
or title. General source-semantic validation and one-time pre-cutover request
adoption remain separate deployment gates. Never use a new request to bypass them.

For a compiler-owned Event/Item title mismatch, the same `draft_recompile` patch
can coalesce both Event title fields to the unchanged recorded entry/Item title.
This rule requires the exact immutable request revision, its original statement
and attachment digest, and literal title text in the verified retained PDF
fragment. Every other canonical/provider effect must remain identical. It cannot
add an event, change dates, location or destination, or expand recurrence.
For native images the equivalent rule compares against the separately recorded
initial interpretation and verifies the retained original bytes. Its proof is
labeled `recorded_image_interpretation` with independent semantic verification
false; it does not claim OCR-certified truth. Unproved evidence leaves the old
draft intact with an exact constraint.
The receipt carries a repair count and proof hash, not copied source text. Full
proof bindings stay in the immutable revision; replay does not reapply the repair.
Repeated same-title entries compare using exact pinned dependency IDs, not an
assumed equivalence of distinct created objects. This comparison does not redefine
semantic authority hashes. No historical request is automatically repaired.

New persisted option/freeform scope hashes use typed semantic format 2. They
retain policy JSON literally and normalize only declared mechanical fields;
renaming a same-draft dependency is not a new semantic effect. Source identities,
cardinality, dates, destinations and occurrence scope remain bound. Symbolic
selection substitution applies only to declared provenance slots, never arbitrary
policy text/data. A comparison digest does not independently establish authority.

`semantic_request_migration_required` means a preserved pre-staging or unversioned
binding needs explicit adoption. For a current typed, hash-matching direct request,
review its bounded current summary, then use the sole `draft_adopt` staging
operation. It takes no new payload, scope or versions. It records a new assembly
revision and immutable proof while preserving the original request, utterances,
sources, hashes and failed attempts. Observe the new revision before committing;
a bounded summary suffices. Subsequent edits/recompilation and the shared commit
service still enforce the original exact effects and provider identity.

`request_adoption_unproven` identifies the exact missing proof or incompatible
representation; it is not a request for renewed authorization. Unversioned scopes,
unverifiable compiler ownership or changed evidence remain preserved, not decoded
or automatically rebound. Do not create a second request, edit its hash in
production or retry adoption indefinitely. Source-grounded adoption outside this
current-typed equality path remains a deployment prerequisite.

Migration `20260911f5c4` adds internal `request_assembly_adoptions`, with one
immutable proof per request and references to its original/adopted revisions.
It does not adopt or execute historical work at startup. PostgreSQL rejects proof
updates/deletes. Downgrade refuses when any proof exists; use the verified
pre-migration backup or a reviewed forward repair, never delete evidence to
make downgrade pass. Empty-database downgrade/re-upgrade is an isolated rehearsal.

Assembly admission and execution resolve the exact originating utterance's
existing request, including when that request was created after tool admission.
They do not pick the latest request or drop cancelled/superseded requests from
the lookup. A committed request recovers its stored receipt without compiling or
executing again; an unfinished direct request remains bound while adoption is
required. Multiple request bindings return `assembly_resume_ambiguous` and must
be reconciled. Retrying a recorded failed operation keeps its original binding
and result even when a later attempt has progressed. PostgreSQL locks serialize
initial binding and attempt-number allocation across concurrent executions.

Every authenticated tool invocation receives one `call_`, including validation
and authority rejection. Tool logs retain hashes and bounded references, not raw
arguments or results. A conversational trace marked interrupted has
`domain_state=unknown` unless a durable terminal Docket outcome proves otherwise.
Local schema rejection is a completed transport with
`result_disposition=rejected_validation`; a trace card left at `running` for
such a rejection indicates a trace-callback contract failure, not live Docket
work.

Early instruction, binding, schema and admission rejections in a live request
are recorded and terminalized by the gateway before returning to Hermes; they
do not need a post-tool hook. A call with no live captured request is blocked,
not executed or attached to another turn. Local signing precedes admission.
When an admission response is lost, a subsequent new operation can reconcile
its predecessor only from exact, terminal, pre-dispatch trace evidence matching
the original source, tool and arguments. A started or committed operation is
never reclassified from local telemetry.

Trace retention no longer stops at 100 calls. The card still shows a bounded
recent sample, and history pages still cap at 100 rows/16 KiB. Exact totals and
omitted counts describe the complete retained trace, including late staging and
commit calls. Migration `20260911d3a2` removes the database's 100-call constraint;
it changes no existing trace or governance evidence. Downgrade refuses to run
if any longer trace exists rather than truncating it. Use a reviewed forward
repair or the verified pre-migration backup for production recovery.

## Operator input and response failures

An import error such as `source_fragment_hash_mismatch` or
`source_fragment_extractor_mismatch` preserves its staged entries. Read the
retained PDF with `docket_read_attachment_text` and submit a new staging operation
with the exact returned extractor version and fragment coordinates/digest.
Do not replay the rejected operation key with changed content, invent a digest,
or ask the Operator to authorize the same request again. This check establishes
the cited text's integrity; it is not independent proof of its interpretation.

If an Operator message receives only a reaction or no final response:

1. Verify that one `utt_` exists for the exact Discord message/interaction.
2. Inspect its IntentSession and ToolInvocations.
3. Determine whether a `chg_` committed before diagnosing delivery.
4. Inspect `rsp_`, `proj_`, projection delivery, and outbox state separately.
5. Reconcile a dead or cleanly replaced gateway lifetime against durable
   outcomes; never relabel a committed ChangeSet as interrupted merely because
   response delivery failed.

An input that cannot be durably captured fails closed. A generated response may
exist even when delivery failed; retry the same projection identity rather than
creating another semantic response.

Native Discord receipt and deferred ingress may both observe the same message.
They share one durable utterance and a serialized execution claim. A completion
callback must belong to that exact claim, not merely the same gateway lifetime
or Discord message. A skipped duplicate cannot release the owner's lease. The
deferred path records its own response delivery/completion, and a persisted final
response prevents another model execution even if delivery remains pending.

## Attention and brief diagnosis

Triage may suppress under an existing Preference, create `bentry_` informational
output, or admit one `case_` for a concrete unresolved canonical consequence. An
unknown sender alone is not an attention reason.

During the active window, a new AttentionCase is queued for individual
projection. Overnight cases remain durable and appear in one morning brief. The
night brief covers daytime triage. A Discord reply binds to the exact visible
`proj_`, case or brief revision, and resumes an authenticated IntentSession.

For a noisy or stuck case, inspect:

```text
tri_ -> ctx_ -> source refs
case_ -> caserev_ -> required/supporting citem_
proj_ -> delivered Discord message
reply utt_ -> ses_ -> chg_
```

Required CaseItems need an Operator-backed terminal disposition. Supporting
items may become `not_pursued`; that is not the same as Operator rejection. A
case reply must apply the narrowest effects supported by the utterance.

Useful triage controls are:

```bash
scripts/docket gmail-status
scripts/docket gmail-triage-status
scripts/docket gmail-triage-pause
scripts/docket gmail-triage-run
scripts/docket gmail-triage-resume
```

The one-shot run requires the recurring job to be paused. Gmail evidence is
untrusted and triage cannot mutate canonical objects or providers.

## Item, Task, Time, Event, and Reminder diagnosis

Keep primitive boundaries explicit:

```text
Item     bounded thing being tracked
Task     work the Operator needs to do
Time     due/scheduled/open/expected/effective temporal meaning
Event    occurrence with scheduling/attendance semantics
Reminder notification behavior
```

A date does not create an Event. A Time calendar marker is a provider projection
of `time_`, remains distinguishable from `evt_`, and requires an explicit display
policy and CalendarLane. An Event linked to an Item must be temporally compatible
with the Time it claims to realize.

For a missing Calendar object:

1. Resolve whether the target is `evt_` or a Time marker (`time_` + `tproj_`).
2. Verify the lane and active route or projection.
3. Verify that the committing `chg_` contains a compiler-produced `op_` target.
4. Inspect Operation and ExecutionAttempt state.
5. If request transmission may have occurred, reconcile rather than retrying
   blindly.
6. Confirm the provider binding and fresh Calendar cache independently.

A Google popup ReminderPlan requires an Event provider binding or an active/
same-ChangeSet Time projection. Docket queue reminders can target Event or Time
without turning a deadline into an Event.

After commit, `docket_get_history_entry(ref="chg_...", view="delivery")` follows
the existing provider Operations. New receipts contain this read's exact
arguments. The bounded result reports whole-request Operation state counts and
each target's committed title, timing, lane, `op_`, error and next action. It
does not load unrelated source content or expose provider credentials/IDs.
Counts and page rows share one database statement snapshot; another page is a
new status observation and may reflect completed delivery. Cursor order is
stable across those status changes. A status read never retries provider work.

Report partial delivery precisely: identify the failed target and retain the
confirmed siblings. Recovery follows the existing `op_`, not a new stage/commit
of the user request. A queued receipt may be reported without repeatedly polling
Google. Canonical commitment, provider confirmation and Discord reply delivery
are different outcomes.

## Attachment evidence

An Operator attachment first creates bounded `src_` metadata and, according to
retention policy, an encrypted blob. Interpretation and mutation wait until
durable bytes are available. Attachment contents remain untrusted.

Imported Items must point to exact derived source-fragment statements. Exact
fragment correlation is idempotent, while identical bytes from distinct uploads
do not silently merge semantic Items. The safe default import scope permits
context only; Task, Event, Reminder, provider, Preference, and destructive
effects require explicit Operator scope.

Attachment download tries Discord's fresh URL before the cached proxy. A
matching replay cannot redefine terminal evidence, and a failed/rejected
capture terminates the ingress with a durable Operator response instead of
reaching interpretation or retrying indefinitely. If capture fails, verify the
terminal ingest and retention disposition without printing plaintext. The
encryption key must be retained with credential backups or retained blobs
cannot be restored.

Images use the image-capable main Hermes/Codex model's native input path, not a
separate OCR service or a lossy auxiliary-model caption. Docket checks the
original inline image bytes and their order against the captured attachment
digests before interpretation. Existing limits remain ten attachments, 8 MiB per
attachment and 16 MiB total by default; no GPU service or new OCR dependency is
required. Native vision remains fallible, particularly for small text and table
layout. Model-extracted entries are interpretations backed by a source, not
independent authority or a proof that their semantic values are correct.

For `docket_native_image_input_unavailable`, inspect the retained source's ingest
state, the deployed plugin pin and the native input path without printing image
payloads. Recover that path before resuming the same authorized request. Do not
substitute a text-only caption, ask for renewed authorization, or restage an
already committed ChangeSet. The failed execution stays failed; a fresh admitted
execution must verify the images again. A durable failure `rsp_` has its own
delivery recovery, so a generic gateway exception must not generate a second
reply. Merely mentioning an older retained image in a new message does not prove
that it was attached to the model input.

Local OCR is not installed or enabled by this path. If it is introduced later,
record the ROCm-compatible runtime/model pin, memory and concurrency budgets,
timeouts/cancellation, retention policy and failure behavior before enabling it.
Native image preparation and PDF text extraction are separate capabilities;
do not advertise scanned-PDF support without an independently verified path.

When Hermes cannot natively consume a retained PDF, it reads the exact `src_`
through `docket_read_attachment_text`. The tool returns bounded, paginated,
untrusted text with page/character locators, fragment hashes, and the extractor
identity/version required for derived statements. It never returns attachment
bytes. A PDF without a text layer fails explicitly; OCR is not currently
advertised or inferred.

## Provider operations and reconciliation

Canonical commit and provider execution are separate outcomes. A failed or
uncertain provider call never rolls back unrelated committed canonical state.

Inspect:

```text
chg_ -> op_ -> execution attempt -> provider binding
```

Only retry when the durable state machine proves no transmitted request can be
duplicated. Unknown-after-transmission uses reconciliation. Never mark an
operation succeeded to clear a queue or make a deployment pass.

New Calendar creation intents retain their provider event ID in the Operation
target before transmission. Recovery uses that same identity. Read failures
stay in reconciliation; a duplicate-ID response requires verification, not an
overwrite or another ID. A callback from an older execution lease cannot change
the current operation's outcome. Partial delivery recovery preserves successful
siblings and the original operation identities.

Pre-cutover creation Operations lacking a durable event ID cannot be blindly
executed after this change. A known matching correlation can still reconcile to
its existing provider event; a missing match remains unresolved. Inventory such
pending/uncertain operations and establish their exact disposition before
deployment. There is no automatic identity backfill or historical replay.

External write gates in production fail closed. Enabling a gate does not itself
authorize a new semantic effect.

If a committed ChangeSet's Calendar operations reached terminal
`google_auth_invalid`, replacing the refresh credential does not silently replay
them. Inspect and recover that exact scope through:

```bash
scripts/docket calendar-recover-auth status chg_...
scripts/docket calendar-recover-auth requeue-auth-failures chg_... --execute
```

The recovery command first validates the current Google refresh grant. It then
requeues only that committed ChangeSet's exact failed Calendar operations,
preserving every `op_`, provider correlation, idempotency key, basis reference,
and failed ExecutionAttempt. It refuses mixed provider scopes, unrelated
terminal failures, or concurrent pending/running/reconciliation work. The
original Operator authority remains the basis; credential recovery creates no
new semantic effect. Monitor the exact operations to terminal provider outcomes
and refresh Calendar sync before reporting the external repair complete.

## Deployment and drain

Deployment is distinct from push and requires explicit Operator direction.
Normal deployment is:

```bash
scripts/docket predeploy
scripts/docket deploy
```

`predeploy` requires a clean `main` exactly matching `origin/main`, both GitHub
CI jobs green for that SHA, production configuration, and safe durable state.
`deploy` establishes a drain barrier, lets pre-barrier execution leases finish,
captures later ingress durably for deferred processing, creates a backup,
upgrades the schema, replaces services, and verifies the result.

Queued durable operations and outbox rows survive restart; only claimed/in-flight
work blocks the drain. A drain timeout aborts without cancelling active work.

Deploying the stable Discord ingress itself uses the separately quiesced path:

```bash
scripts/docket deploy-ingress
```

Do not manually restart the gateway in the middle of an Operator turn. After an
unclean lifetime expires, reconciliation preserves any durable domain result and
marks only evidence-free conversational execution interrupted/unknown.
The same reconciliation runs when a drained deployment cleanly replaces a
gateway. If an `rsp_` or terminal `turn_` already proves execution finished, its
claimed ingress becomes completed rather than pending and is never re-executed;
only an ingress without durable terminal evidence is released for resumption.

Invocation recovery uses the exact authenticated upstream call and its durable
AssemblyOperation, not the SemanticRequest's latest commit state. An earlier
stage/review/rejection therefore keeps its own outcome even if a later commit
succeeded. Recovery includes invocations whose asynchronous trace callback never
arrived, and transport retries must match the original signed binding. It does
not replay mutations or provider operations.

A late authoritative Docket result can replace `unknown/gateway_interrupted`,
but cannot overwrite a known terminal outcome. The conversation remains
interrupted and its domain-status projection is refreshed. Late finalization
links only the exact assembly attempt, never the request's newest attempt.
An exception while assembling the response after commit returns the original
bounded receipt when that exact durable outcome can be established. Evidence-free
unknown calls remain unknown; periodic recovery does not repeatedly lock them
unless a matching terminal operation becomes available.

## Clean reset boundary

The signed tracked-context amendment authorizes implementation and rehearsal of
the clean reset. It does not authorize deleting production operator-domain data,
performing provider mutations, or deploying the reset.

Read-only rehearsal is available through:

```bash
scripts/docket readiness-rehearsal
```

It snapshots production, restores the matching pre-reset image, exports the
governance closure, inventories provider effects, creates isolated clean
databases, and verifies attachment backup/restore. Private evidence remains in
mode-`0600` ignored backup storage. A successful run also writes a sealed
`production-reset-manifest.json` and prints the one exact authorization message
bound to its backup hash and the full deployment revision. Any subsequent code
commit requires a new rehearsal and manifest because the revision binding no
longer matches.

Send that byte-exact authorization through the trusted Docket/Discord path.
Before cutover, verify that it produced an immutable `utt_`, a
`production_reset_authorization` `dec_`, and its `aud_`. A checkmark or ordinary
chat response is not sufficient evidence.

The destructive command is intentionally separate from rehearsal and from the
Discord authorization:

```bash
scripts/docket production-reset \
  backups/tracked-context-readiness-YYYYMMDDTHHMMSSZ \
  --execute
```

Run it only after a separate explicit Operator direction to perform the reset
and deployment. The command requires synchronized `main`, green GitHub CI, the
exact evidence directory, the exact ledger Decision, and the matching old image.
It verifies everything before drain and again after drain; stops all database
writers; recomputes the final governance closure with the reset-authorization
chain; materializes and verifies an empty `docket_cutover_*` database; then swaps
database names. The pre-reset database and image remain quarantined only until
the clean service, ingress, Hermes registry, schema head, and authority chain
pass. They are then removed from the live cluster while the sealed custom-format
backup remains offline.

Before the final database swap, any failure drops only the explicitly named
empty cutover database and restores the pre-reset image. After the swap but
before verification completes, failure renames the quarantined pre-reset
database back to `docket`, restores the matching image, and removes the failed
clean database. If that recovery itself fails, leave PostgreSQL and application
services stopped and restore the sealed backup with the matching image; never
start either image against the other schema.

Never execute a production reset without a later exact authenticated Operator
instruction bound to the reset manifest, backup, and deployment revision. The
governance ledger, specification-signoff Decisions, sign-off audits, artifact
identities, provider/account configuration, and required authority provenance
must survive. Disposable operator-domain state receives no compatibility alias,
decoder, or synthetic backfill.

## Backup and restore

Create or confirm the encrypted backup:

```bash
scripts/docket backup
```

Verify restore into disposable PostgreSQL:

```bash
scripts/docket verify-restore BACKUP_PATH
```

An image rollback does not reverse a database migration. Use the exact migration
recovery plan and verified backup; do not retag an old image against a newer
schema and hope for compatibility.

## Credentials and external dependencies

Production credentials belong in the configured secret directory, never the
repository or logs. Reauthorize Hermes only through:

```bash
scripts/docket setup-hermes-auth --main
scripts/docket setup-hermes-auth --triage
```

Calendar and Gmail provider identity must resolve through clean `acct_`
ProviderAccounts. SearXNG is a network-private search dependency for Hermes; it
is never canonical state or provenance authority.

## Handoff checklist

Before reporting an operational change complete, record:

- exact behavior changed and public refs used for verification;
- focused tests plus `scripts/docket check`;
- `scripts/docket compose-smoke` when required;
- commits created;
- whether anything was pushed;
- whether anything was deployed;
- whether any production data or provider state changed;
- remaining reconciliation, reset, or Operator step.
