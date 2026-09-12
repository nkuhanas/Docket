# Interaction correction verification

`ONT-DELTA-2026-09-11-INTERACTION-CORRECTION` is signed at SHA-256
`39ca596f2ff00e70cad28fe6e3750f284efde53d4db47d8490531d61ae9fd23a`.
Its private source remains under `deltas/`. The packaged artifact manifest
registers its exact command, implementation scope, and five prerequisite
specification Decisions. Registration does not establish sign-off.

September 11 read-only production verification established the exact chain:
`utt_01M293W0FNQ8NSMP6K71WQRNAQ` →
`dec_01M293W1JA9CZFRMNY3FR2A48D` →
`aud_01M293W1JBB5B6PZQXTKS7AXM0` →
`rsp_01M293W1JMYAYT29RBGDK1MK0A` (delivered).
Implementation is in progress; sign-off does not mean the amendment is deployed.

The enablement slice changes only manifest metadata and verification. It does
not activate occurrence actions, remove direct commit, change skills, migrate
drafts, or trigger calendar repair. There is no new provenance bootstrap.

## Sign-off gate

After deploying the enablement slice, send the manifest's exact `signoff_text`
through the authenticated Docket Discord surface. The existing deterministic
recognizer must capture `utt_`, record one `specification_signoff` `dec_` and
`aud_`, and deliver the durable `rsp_`. Inspect those references before marking
implementation authority available; a gateway reaction is insufficient.

The candidate requires all five exact prerequisite Decisions, including
`dec_01M1W7648P7YJZ22GRD114WSBV` for incremental assembly. A matching document
and hash under a different Decision ref cannot satisfy a prerequisite. No
production reset authority is granted. Deployment remains a separately directed
operational action.

Tests exercise real sign-off service transactions, replay, wrong hashes,
non-exact text, each substituted prerequisite, and zero canonical event,
ChangeSet, or provider Operation creation during sign-off. The tests read only
packaged metadata and synthetic test fixtures; private source files and
production state are not CI dependencies.

Local enablement verification on September 11 passed 369 tests, Ruff, and strict
mypy, including the same full gate from a clean Git archive without private
sources or runtime state. Isolated Compose smoke passed MCP checks, synthetic
governance export/restore, PostgreSQL assembly races, and migration round-trip.
Those results verify registration and the existing runtime only; they do not
constitute implementation evidence for the new UX behavior.

## Implementation gate

The private readiness design separates planned acceptance fixtures from passing
results. After ledger sign-off, deliver occurrence safeguards/instruction
isolation first, then staging/repair, deployment/provider recovery, and
diff/trace/performance work. Record passing deterministic and PostgreSQL evidence
as it exists; do not mark planned work implemented.

The central workflow is mandatory staging with optional review. Stage readiness
must distinguish saved errors from a committable revision. The final deployment
gate includes exact user outcomes, service-enforced occurrence scope, lost-response
and concurrency tests, compiler version continuity, provider duplicate prevention,
generated contracts, full checks, and isolated Compose smoke.

Live latency is measured from durable ingress to delivery of the canonical-commit
receipt; provider completion is separate. Deterministic fixtures are not evidence
of live latency. Historical calendar repair is not an automatic deployment step.

## Duplicate execution correction

An additional Operator report exposed two competing ingress paths: native
Discord delivery and Docket's deferred dispatcher. Read-only September 11
evidence found two recent utterances each with two leases. The first lease was
completed with `agent_turn_not_finalized` within seconds; a final response was
persisted minutes later, then a second lease was acquired. This establishes
erroneous re-admission, not a measured count of duplicate model generations.

The correction serializes claim admission on the immutable utterance, binds
processing callbacks to the actual dispatch event, and explicitly completes the
deferred dispatch's response lifecycle. Deterministic sign-offs have one delivery
owner. A terminal response fences re-execution even when its Discord delivery is
pending. An old completion token cannot release a newer claim or another message
under the same gateway lifetime. Genuine unfinished execution remains recoverable.

Regression coverage is in `test_deployment_continuity.py`,
`test_plugin_actor_gate.py`, and the native/deferred concurrency case in
`scripts/compose-assembly-postgres-smoke.py`. This does not replay or modify
historical requests and does not claim exactly-once delivery from Discord.

Local verification passed all 378 tests, Ruff, strict mypy, and isolated Compose
smoke, including a two-connection PostgreSQL race with exactly one execution
claim and one utterance audit. The first race run exposed an additional
deterministic-audit insertion race; serializing before audit creation fixed it,
and the complete smoke passed on rerun. No production deployment is implied.

## Reviewed instruction isolation

The Docket deployment mounts the entire Hermes discovery root read-only with
exactly `docket-manual-intent` and `docket-triage`. External discovery directories
are cleared by configuration synchronization. Runtime skill edits are rejected;
proposed improvements require repository review. Each gateway lifetime pins the
reviewed bundle's content hash, records it in trace context, and stops new model
dispatch/mutation calls if that bundle changes in place. This is instruction
isolation, not a substitute for the service-side semantic guards below.

After draining and backing up, deployment moves runtime-authored skills into
`.runtime/hermes/protocol-quarantine/skills-<random-id>` before starting the new
workers. Original files are retained, not deleted or translated. The quarantine
is outside skill retrieval. Repeating the operation on an empty root is a no-op;
redirected roots are rejected. This step has not been run against production.

Tests cover blocked skill tools, allowlist drift, bundle changes, config sync,
read-only mount configuration, and recoverable/idempotent quarantine ordering.
An isolated network-disabled container of the pinned Hermes image, without
credentials or production runtime volumes, discovered exactly the two reviewed
skills and rejected a write to its skill root with a read-only-filesystem error.

Local verification passed 386 tests, Ruff, strict mypy, and isolated Compose
smoke (MCP/authentication, governance preservation, PostgreSQL concurrency, and
migration downgrade/re-upgrade). The remaining occurrence, mandatory-staging,
semantic-repair, and delivery/latency acceptance gates are still open. This slice
does not claim the full amendment implemented or production changed.

## Occurrence identity and scope enforcement

The occurrence slice introduces internal `event_occurrences` rows, identified by
canonical series ref and the original zoned start/date, not by the replacement's
current start or a Google instance ID. Cancelling one occurrence compiles an
exception on the master. Moving it adds one routed replacement; subsequent edits
reuse that replacement. Explicit whole-series cancellation includes moved
children. Existing exclusions and adjacent occurrences remain intact. A repeated
occurrence cancellation produces a committed no-op without more provider work.

The guard runs in ChangeSet preflight and the canonical event mutation handler.
Unqualified recurring-event changes are rejected even from an older direct
client. Scope must match the persisted request, and occurrence effects must match
Docket's compiled actions. Calendar summary reads expose a compact
`mutation_target` with original identity and canonical series version, including
for moved occurrences. Unbound provider rows do not become canonical authority.

`calendar_date_bindings` pins the date and timezone resolved from the captured
utterance. The gateway supplies that message ref; a relative MCP lookup without
it fails closed. Recovery reuses the stored result despite timezone or clock
changes. Google all-day `originalStartTime.date` is retained in the provider
cache alongside timed original starts.

Migration `20260911b1e0` adds these internal tables and the all-day cache field.
PostgreSQL triggers protect original identities and date bindings. There is no
public alias, historical calendar rewrite, provider call, or domain backfill in
the migration. Its downgrade removes the new bookkeeping; it is a rehearsal
operation, not a safe production rollback after new occurrence edits. Production
recovery must use the pre-migration verified backup or a reviewed forward repair.

The staged read and recurrence tests verify original identity, DST ambiguity,
already-moved edits/cancellation, duplicate cancellation, existing exclusions,
explicit whole-series cancellation, relative-date stability, and no master
retraction through an unqualified service call. Local verification passed 408
tests, Ruff, and strict mypy. Isolated Compose smoke passed, including competing
occurrence commits (one succeeds; one receives a version conflict), concurrent
relative-date capture, database trigger enforcement, governance restore, and
migration downgrade/re-upgrade. Large scoped summaries paginate within the output
budget without losing the remaining rows. Mandatory
staging, exact repair, delivery recovery, and latency gates remain open.

## Staged-only MCP boundary

The Operator explicitly authorized the MCP API cutover. The interactive registry
now contains 23 tools: stage/review retain infrastructure-supplied utterance and
request bindings, commit exposes no model payload, and clarification persists
typed choices without canonical effects. All top-level MCP schemas reject unknown
fields. Removed direct/assembled payloads are rejected, never decoded or ignored.
Hermes scopes staging/choice schemas to requested variants and supplies execution
bindings from the captured message. Missing or stale execution context fails closed.

End-to-end MCP fixtures stage and commit without review, reject an old direct
recipe while preserving the draft, terminalize its admitted operation, and recover
the same receipt on commit retry. Clarification tests prove zero canonical changes.
The PostgreSQL smoke exposed child-before-parent flushing in semantic option
persistence; the service now flushes the immutable projection before its children
within the same transaction. The isolated ingress role can read the resulting
option but still cannot update the utterance ledger.

Local verification passed 410 tests, Ruff, and strict mypy. Isolated Compose smoke
passed the new 23-tool MCP flow, PostgreSQL races, governance restore, and migration
downgrade/re-upgrade. No production data, deployment, or historical replay changed.
The older tool count in the governing `AGENTS.md` initially remained unchanged
pending separate authorization. The Operator subsequently authorized updating
only its tool count and mutation-protocol description; the guide now matches the
23-tool, staged-only API. Authority and production-safety rules are unchanged.
The signed amendment and generated runtime contract define this API cutover.
Exact semantic repair, compact compilation, delivery recovery, diffs, and timing
acceptance remain open; this is not full-amendment completion evidence.

## Compact scheduled-occurrence compiler

The scheduled-entry input now contains one title, one timed/all-day interval,
location, exact lane reference (or same-draft lane create), and source evidence.
Docket derives the Item, TemporalBinding, Event, routing and provider intent.
The entry scope names semantic entry types instead of requiring enumeration of
compiler-owned mutations. Duplicate Item/Event title and time payloads are not
accepted as aliases. The compiler resolves the provider lane slug from canonical
or same-draft lane state; it does not infer dates, recurrence or missing intervals.

Compiler/input schema version 2 records the new entry representation. The
instruction bundle and generated v22 contracts describe this shape. Replacing a
scheduled entry with a no-occurrence entry removes the complete derived Event and
route set; direct edits to compiler-owned actions remain prohibited.

Local verification passed 411 tests, Ruff and strict mypy, plus isolated Compose
smoke including a 30-entry PostgreSQL commit. The three-entry career-fair fixture
asserts exact September 16/17/18 titles, times, location, Meetings destination,
linked Item/Time values and three pending provider Operations. It uses one stage
and one commit with no review. These are synthetic deterministic fixtures, not
live Calendar delivery or latency evidence. Saved-error retention, immutable
semantic repair, compiler-deployment continuity and other remaining amendment
gates are still open; this compiler slice has not been deployed.

## Retained failed drafts and explicit readiness

Stage now separates editable action inputs from executable compiled snapshots.
An entry-level or whole-ChangeSet compilation error preserves the entire input
batch, original evidence, expected versions and immutable failed revision.
Successful neighboring entries cannot commit on their own. A corrected patch is
a new operation within the same request; replay of the failed operation returns
its old receipt without restoring obsolete input. A rejected commit records
`blocked_validation` without consuming authority or making intent ambiguous.

Receipts distinguish `saved_with_errors` and `ready_to_commit`. They include a
bounded semantic entry preview, exact entry totals and omitted-entry count.
Compilation diagnostics carry entry/path/constraint/category/next-action fields
without copied validation inputs. Entry review paginates the same immutable
revision. A ready draft still needs no mandatory review before commit.

Migration `20260911c2f1` adds nullable `staged_actions_json` to ChangeSet and its
immutable revisions. It does not backfill or execute old requests. Null inputs
on a pre-cutover draft require explicit adoption; the complete adoption path is
still a deployment prerequisite. Downgrade is for isolated rehearsal only once
failed drafts exist: production recovery must retain these inputs through the
verified backup or a reviewed forward repair.

Local verification passed 413 tests, Ruff and strict mypy. The PostgreSQL smoke
injects a compiler error into a 30-entry draft, reconnects in a fresh session,
verifies all 30 entries and the surviving action inputs, rejects an attempted
rewrite of the immutable failed revision, repairs only the failed entry, and
commits exactly 30 events/provider intents once. Isolated MCP, governance restore
and migration downgrade/re-upgrade also passed. These fixtures do not establish
full semantic-equivalence repair or live provider delivery; those gates, explicit
adoption, executable compiler pinning, full diffs and timing remain open.

## Pinned executable draft revisions

Each new revision stores a versioned executable pin in the existing compiler
manifest. The pin binds editable inputs/ownership, each normalized entry's
compiler/input-schema versions, exact compiled effects, execution preconditions
and request authority separately. Failed uncompiled drafts pin their retained
inputs without inventing executable effects. No historical pin backfill or
old-schema decoder is introduced.

The shared commit service locks the ChangeSet and verifies it against the
immutable revision before applying anything. It parses stored effects with the
current canonical schema and checks that parsing did not change their digest.
It does not rerun the normalized-entry, occurrence or provider compiler. Current
canonical preconditions still validate. Snapshot drift fails with preserved
authority; an incompatible executable schema requires explicit migration.
An unchanged patch also cannot silently install changed compiler products.
Committed receipt replay bypasses migration and never executes again.

The new regression fixtures exercise changed compiler versions after resumption,
mutated inputs/effects/preconditions/bindings/manifests, incompatible schemas,
and unchanged input producing different compiler output. The PostgreSQL smoke
also reconnects before committing the 30-entry draft under changed compiler
versions, forbids either compiler from running, and verifies database rejection
of edits to an immutable revision's pin. Local verification passed 421 tests,
Ruff and strict mypy. Isolated Compose smoke passed, including PostgreSQL
pin/revision enforcement, concurrency, governance restore and migration
downgrade/re-upgrade. Explicit migration/adoption and semantic repair remain
separate open gates, so this does not establish full amendment readiness.

## Provider creation identity and recovered execution ownership

New Calendar creation Operations persist a provider event ID in their target
parameters in the canonical transaction. The adapter sends that same ID on
each insert; a duplicate-ID response requires an exact read matching the
committed snapshot and correlation. It never changes the ID or overwrites a
conflicting event. An unreadable/malformed successful response or uncertain
write-side server error enters reconciliation. Failed reconciliation reads stay
in reconciliation rather than being reclassified as permission to execute.

Completion now locks the Operation and checks the exact lease, target and
attempt. Late success/error/unknown callbacks from an expired owner cannot
overwrite a newer result. A mismatched correlated event remains unresolved;
it is not filtered away and mistaken for an absent event.

Deterministic fixtures cover provider acceptance followed by a lost response,
worker restart, temporary missing reads, stale callbacks, failed reconciliation
reads, and three deliveries with recovery of only the failed sibling. The
Google REST mock verifies the same insert ID and duplicate-ID verification.
The PostgreSQL smoke races a dead worker's late failure against the replacement
worker's successful reconciliation and checks one provider event and preserved
attempt outcomes. Local verification passed 432 tests, Ruff and strict mypy.
Isolated Compose smoke passed, including that PostgreSQL race, immutable draft
pins, governance restore and migration downgrade/re-upgrade.

This slice does not claim the per-entry delivery-status UX complete or any live
Google result. Pre-cutover pending/uncertain creation Operations without a
pinned provider ID need disposition inventory before deployment. Their existing
correlation may prove a prior success, but an absent match cannot authorize a
new creation; no automatic backfill or historical request replay is introduced.

## Actual draft-revision diffs and lossless bounded review

Optional diff review now compares semantic staged inputs with the preceding
immutable revision. Rows contain actual before/after values and presence flags,
including removed entries, changed titles/timing/destination and occurrence
scope. Compiler-owned support records are represented by their owning entry,
not repeated as duplicated Item/Time/Event values. Type filters match either side
so removed or reclassified entries remain visible. Provenance/compiler
bookkeeping alone does not masquerade as a semantic edit.

The response explicitly identifies the draft comparison and its base revision;
it does not claim comparison with live canonical or provider state. Both sides
remain fixed across cursor pages. Cursor formats are versioned and malformed
positions fail closed. Large individual details are losslessly fragmented as
UTF-8 JSON with offsets/digests, while samples and pages remain bounded. Exact
logical-detail and transport-row counts distinguish pagination from omission.

Local verification passed 439 tests, Ruff and strict mypy. Isolated Compose
smoke passed, including a fresh-connection PostgreSQL diff read spanning another
attempt's edit; subsequent pages kept the original values, and the old reader's
commit received a revision conflict. Other fixtures cover removals, type/scope
changes, absent versus null values, large Unicode fields and reconstruction of
all fragments. Contracts and the reviewed skill describe the optional flow.

This is partial ONT-UX-ACC-0008 evidence. Pinned canonical-before/after previews
and explicit compiler-migration effect diffs remain open, alongside exact
source-grounded semantic repair, one-time adoption, delivery status and timing.
No production changes, provider calls or historical replay were performed.

## Receipt-bound provider delivery status

New commit receipts now identify the bounded `docket_get_history_entry` delivery
read for their exact `chg_`. This read uses only that request's durable provider
Operations/targets, returning current counts and per-target committed title,
timing, lane, public Operation reference, error and next action. No staging,
provider call, retry or authorization mutation occurs. Counts and page rows use
one SQL statement snapshot; pagination orders immutable target identities while
allowing status to advance between reads. Provider IDs, correlation tokens, raw
parameters and source/provenance dumps are not projected.

Local verification passed 441 tests, Ruff and strict mypy. Isolated Compose
smoke passed the PostgreSQL status query for one confirmed and 29 queued
deliveries, including a fresh-connection second page with no repeated targets.
The partial-failure fixture confirms two deliveries and identifies the failed
third by exact title/time/error; recovery reuses its Operation and the next read
confirms all three. Thirty-target pagination, no-provider-work receipts,
uncommitted requests and wrong-object/malformed-cursor reads are also covered.

These results establish deterministic delivery-status and partial-recovery
behavior, not live Google or Discord outcomes. The exact semantic-request,
adoption/migration, canonical-preview and timing gates remain open. No production
deployment or historical request replay was performed.

## Typed semantic comparison and qualified provenance substitution

The prior scope serializer recursively removed names such as `basis_refs`,
`source_refs`, `change_id` and `expected_versions` from arbitrary domain JSON.
Consequently different policy data could share a scope hash. Selection
substitution likewise traversed opaque data and could rewrite a literal string
as though it were an authority slot. Both operations now follow declared schema
locations. Opaque policy data and dollar-prefixed strings remain literal.

New scope format 2 retains semantic values, source identities, explicit patch
presence, cardinality and occurrence scope. It normalizes action ordering and
same-draft dependency names through structural planned-effect slots, retaining
target sharing and multiplicity. Indistinguishable referenced creates fail
comparison rather than guessing their identity. Cyclic drafts can be described
without recursive failure; the existing transaction preflight still rejects
their execution and preserves the failed request.

An unfinished unversioned binding requires explicit migration. It is not silently
rehash-bound to a new request, decoded through an old public protocol, or treated
as missing Operator permission. Existing evidence/hashes are not rewritten.
The complete audited migration/adoption path remains a deployment gate.

Local verification passed 466 tests, Ruff and strict mypy. Isolated Compose
smoke passed the current MCP/selection flow, PostgreSQL assembly/occurrence and
delivery checks, governance restore and migration round-trip. New adversarial
fixtures cover opaque-key collisions, literal authority symbols, dependency
renaming, duplicate creates, cyclic graphs, missing/null patches, changed source
identity, the three-event cardinality/date/time/title/location/lane boundary,
and occurrence-to-series expansion. A retained old-request fixture proves
migration rejection leaves its authority available and creates no second request
or canonical object.

This is comparison infrastructure, not proof of source-grounded authority. The
exact immutable freeform request specification, evidence-grounded title repair
and audited adoption remain open. No production change or historical replay ran.

## Actionable import diagnostics and bounded error receipts

Calendar import validation now distinguishes the linked Item, realized Time,
single-occurrence, representation type and statement-basis constraints. Title
mismatches identify the entry/action, exact field path and compared staged
values instead of reporting only `import_entry_calendar_representation_invalid`.
The expected title is explicitly the linked staged Item's title, not a claim of
independent verification against attachment bytes. Compiler exceptions retain
their qualified category and constraint without copying arbitrary exception
payloads or prose into the diagnostic.

Stage, no-op, review and blocked-commit receipts use a byte-bounded diagnostic
sample, exact omitted count and `diagnostic_review` arguments bound to the failed
immutable revision. A diagnostic too large for the sample remains retained and
readable through lossless paginated detail. It cannot turn a saved draft into an
output-budget failure or cause transport compaction to silently drop page rows.
An old error receipt still reads its original diagnostics after a new revision
passes validation. Review remains optional for a ready draft.

Local verification passed 475 tests, Ruff and strict mypy. Isolated Compose
smoke passed MCP/profile checks, existing PostgreSQL assembly and occurrence
races, governance restore, and migration downgrade/re-upgrade. New fixtures
cover three title-mismatched entries with zero canonical/provider effects,
large Unicode title diagnostics, distinct dependency/recurrence/basis errors,
and reconstruction of a 36 KB diagnostic after a later successful validation.
Generated contract v26 and the reviewed skill describe the diagnostic cursor.

This advances ONT-UX-REQ-0012; it does not establish source-grounded repair or
complete diagnostic coverage of every mutation. In particular, replacing
compiler products for an unchanged retained entry still needs the explicit
audited migration/recompile path. No production deployment or provider call ran.

## Trace attempt visibility and measured invocation intervals

Trace cards no longer label every wrapper attempt as an authenticated Docket
call or compute timing from just the first 20 visible rows. They report exact
counts across the retained trace and separate local pre-MCP rejections,
authenticated invocations and unreconciled attempts. Stage/review/commit totals
cover the whole retained trace. The byte-bounded card preview shows recent
attempts with an omitted count; `docket_get_history_entry(view="calls")` provides
bounded details for a `trace_` reference. Cursors bind the trace revision and
reject mixed-revision pagination. Invocation outcomes are live observations.

The trusted callback carries an explicit execution-boundary marker. A known
local rejection cannot claim an unrelated invocation with matching arguments.
Old callback formats are not compatibility-decoded. Docket's measured duration
is the union of closed durable invocation intervals, clipped to the trace
window. Wrapper duration is reported separately across all retained attempts;
it is not subtracted from wall time. Unfinished invocation intervals and time
not covered by measured Docket intervals remain unattributed. Queue, schema,
model, local-validation and provider-wait attribution are explicitly unmeasured,
not fabricated zeroes or guesses about the cause of delay.

Local verification passed 479 tests, Ruff and strict mypy. New fixtures cover
100-attempt complete pagination and a visible late commit, Unicode/byte bounds,
overlapping invocation intervals, unrelated invocation exclusion, stale/foreign
cursors, and the actual Docket renderer through the plugin's digest and Discord
field validation. Inconsistent totals, origins, timing and oversized previews
reject before Discord access. Isolated Compose smoke passed the updated trace
callback, PostgreSQL assembly/occurrence checks, governance restore and migration
round-trip. Generated contract v27 and the reviewed skill describe calls view.

This is partial ONT-UX-REQ-0016 evidence, not complete telemetry or a measured
latency improvement. Exact cross-transport invocation correlation, trace-bound
pre-dispatch rejections, the retained-call safety limit and separate phase hooks
still require completion/audit. The existing nonlocal argument-hash correlation
must not be mistaken for a transport-bound proof. No production changes ran.

## Exact invocation correlation and retry-safe receipt recovery

The gateway now binds each MCP dispatch to its captured utterance, trace,
upstream call/ordinal, gateway lifetime, tool, public argument digest and loaded
contract through a short-lived signed infrastructure envelope. The model does
not supply or retain it. Docket verifies it after authenticated `call_` creation
and before dispatch; invalid envelopes produce a durable rejection without
executing the tool. A later trace callback must agree with that binding,
including the original message. No argument-hash/time-window fallback remains.

Transport retries receive separate `call_` records under the original exact
binding. Correlation does not suppress service execution or replace operation
idempotency: replayed stage and commit calls recover the original durable
receipts. One known committed outcome takes precedence over a still-running
retry in the displayed attempt. All authenticated invocations remain counted
and individually inspectable, even when several belong to one upstream attempt.
The envelope grants no semantic authority and is neither logged nor persisted.

Local verification passed 492 tests, Ruff and strict mypy. The assembled MCP
fixture replays staging and commit without review and still creates exactly one
Item and one ChangeSet. Additional fixtures cover identical arguments in
different traces, invalid/tampered/expired/foreign bindings, signer parity with
the actual plugin, late callback source/hash mismatch, and a running retry that
cannot erase a committed outcome. Isolated Compose smoke passed authenticated
MCP correlation before its callback, simultaneous PostgreSQL transport retries,
the existing assembly/occurrence races, governance restore and migration
round-trip. The generated contracts are v28; profile counts remain 23/4.

This closes the prior heuristic-correlation gap, not all of ONT-UX-REQ-0016.
Pre-dispatch trace coverage, the retained-call safety limit and separate phase
instrumentation remain open. No live latency improvement, production deployment
or historical request replay is claimed.

## Explicit compiler migration and renewed revision observation

A sole `draft_recompile` stage operation recompiles unchanged pinned inputs with
the current schemas. It cannot include edits, changed scope or expected versions.
The original immutable revision, input/effect hashes and authority bindings are
verified first. Only the executable-version equality check is relaxed for this
explicit migration; no old-schema decoder or unpinned draft backfill is added.

Typed canonical semantics and provider targets/parameters must remain equal.
Complete owned action sets are rebuilt, so removed compiler products cannot
linger. A changed title, date, provider account, added event or removal of all
effects preserves the old draft and returns a semantic conflict. Successful
equivalence writes a new immutable revision and audit, retaining authority and
original statements. It creates no canonical objects or provider Operations.

Migration invalidates observation even for its requesting attempt. A bounded
fresh summary or the receipt's first diff page can observe that exact revision
when still current; later or older pages cannot observe a newer draft. Diffs
expose actual compiler products and executable-pin changes. Ordinary staging
still permits immediate commit without review. Lost-response replay recovers
the original migration receipt, including after a later canonical commit.

Cross-field stage validation also terminalizes its admitted operation, so a
malformed migration request cannot strand subsequent corrected attempts behind
a running admission. Generated contract v29 and the reviewed skill describe the
new operation without changing the 23/4 tool counts.

Local verification passed 501 tests, Ruff and strict mypy. Isolated Compose
smoke passed explicit PostgreSQL migration across fresh connections, stale
commit rejection, new-revision observation, receipt replay and database rejection
of an attempted rewrite of the old revision. Existing MCP, concurrency,
governance restore and migration downgrade/re-upgrade checks also passed.

This implements the equality-preserving recompilation branch of ONT-UX-REQ-0017,
not independent source verification. Exact source-grounded request/repair,
unsupported-input adoption, canonical-before/after preview and complete timing
remain open. No production deployment or historical replay was performed.

## Pre-dispatch rejections and complete retained trace history

The gateway records early instruction, trusted-binding, schema, signing and
admission failures in the live request's trace and closes those local attempts
without waiting for a post-tool callback. A replay of that rejected attempt
cannot start executing after configuration changes; correction uses a new call.
No live captured request means fail closed, not untraced execution or attachment
to another turn. Non-Docket instruction restrictions remain unchanged.

Signing now precedes assembly admission. A lost admission response can still
leave a durable predecessor without a Docket invocation; later assembly recovers
that exact undispatched operation from terminal local trace evidence. Source,
tool, call identity and argument digest must all match. Foreign-source,
changed-argument and MCP-attempted evidence cannot release the predecessor.
Started/committed operations remain governed by their durable domain outcome.

The old 100-call limit could stop tracing while permitting dispatch. It is now
removed from gateway, authenticated invocation/admission schemas, persistence
and projection validation. Ordinals still obey monotonicity and database integer
bounds. Cards remain byte-bounded recent samples, and pages retain their 100-row
and output-budget limits with exact whole-trace totals. No aliases or backfill
are introduced. Migration `20260911d3a2` preserves all existing rows and refuses
a downgrade that would require discarding a trace longer than 100 calls.

Local verification passed 516 tests, Ruff and strict mypy. Fixtures exercise
150 calls through actual plugin signing/rendering and complete history pages,
MCP binding at ordinal 150, no-post-hook rejection, and exact predecessor
recovery. Isolated Compose smoke passed fresh-connection PostgreSQL history
past ordinal 100, refusal of lossy downgrade with evidence intact, rejection of
negative ordinals, and lost-admission recovery without creating an invocation
or canonical effect. The subsequent safe migration round-trip and existing
concurrency/governance/MCP checks also passed.

This advances ONT-UX-REQ-0016; asynchronous telemetry delivery/recovery and
separate queue/context/model/validation/provider timing remain open. Unmeasured
time is still unattributed. This does not establish live latency improvement,
full amendment readiness or production deployment.

## Retained PDF fragment verification

The shared ChangeSet validator now recomputes citations claiming
`docket.pypdf.text` from the retained encrypted attachment. It verifies the
exact extractor version, strict page/character coordinates, nonempty bounded
fragment and SHA-256. It does not accept copied locator text, model-computed
digests that disagree with storage, or implicit version migration. A request-local
reader cache avoids decrypting/parsing the same PDF once per cited field.

An invalid citation keeps the complete staged batch and prior immutable
revisions. Commitment is blocked with an entry, field, constraint and source-read
repair action. A new operation can correct the citation under the same request,
stage successfully and commit without renewed authorization or mandatory review.
Tests cover this path through the shared service as well as malformed, stale,
oversized and fabricated citation inputs, Unicode coordinates and bounded errors.
Neither the proof object's representation nor its metadata copies source text.
Local verification passed 529 tests, Ruff, strict mypy and isolated Compose smoke
with the existing PostgreSQL locking, governance and migration round-trip checks.

This proves fragment integrity, **not semantic interpretation**. It does not
yet verify an image/vision extraction, infer that a date belongs to a title, or
establish the exact immutable request specification of ONT-UX-REQ-0009/0010.
Those remain open; an arbitrary extractor identifier does not become a verified
source merely because it is present in an input. No new authority, schema alias,
public tool, historical replay or production change is introduced.

## Canonical Calendar presentation previews

Staging captures a scoped canonical-before/planned-after Calendar presentation
snapshot in the immutable draft revision. The sample exposes manual event
title/time/location/destination/status/scope; normalized entries remain represented
once rather than repeated as compiler-owned Event boilerplate. Combined entry
and event samples share the existing bounded-output budget with exact omitted
counts. Review is still optional.

Optional diff adds `canonical_event_effect` rows based exclusively on that stored
snapshot, including captured and expected versions. It does not reread live
canonical or provider state. The ordinary draft-to-draft diff remains separately
identified. Calendar presentation covers title, timing, location, notes, lane,
status, recurrence, tags and priority; other changed inputs remain in the input
diff and are explicitly identified when not in this projection.

Occurrence previews retain the original coordinate and show an existing moved
timeslot. Cancellation displays a status change on that occurrence, not master
retraction. Repeated cancellation is an explicit no-op. An uncompiled occurrence
has an unavailable preview, not a misleading proposed master cancellation.
Old revisions lacking a captured preview remain unavailable; no reconstruction,
compatibility alias or backfill occurs during review.

Fixtures cover captured values surviving a later canonical update, moved/renamed
and already-cancelled occurrences, exact one-time/series scope, title disagreement,
bounded lossless diffs and normalized-entry deduplication. PostgreSQL rehearsal
checks snapshots across connections and rejects a direct attempt to rewrite the
immutable preview. The generated tool contract is v30, still 23 interactive and
four triage tools. These are planned effects, not provider confirmations.

The moved-occurrence fixture models completed provider projection explicitly.
An occurrence child whose initial provider creation is still queued can currently
be blocked by `provider_event_binding_required`; that distinct recovery/diagnostic
path is not silently treated as a completed projection by these tests.

Validation for this slice: all 533 tests, Ruff and strict mypy (134 source
files) passed. The isolated Compose/PostgreSQL smoke passed, including the
immutable snapshot assertions and migration downgrade/re-upgrade checks. No
production or live-provider operation was performed.

## Executable reviewed instruction example

The repository-managed manual-intent skill now describes the actual staged patch
shape rather than internal grouped ChangeSet arrays. Its sender example includes
the exact `identity_handle` discriminator, required provenance/field metadata,
same-draft dependency, initial scope and expected versions. A regression test
substitutes fixture public refs and validates the complete example against
`StageChangesInput`. Top-level request bindings remain infrastructure-only.

Semantic readiness is explicitly distinct from schema/provider execution
readiness; infrastructure failure does not imply missing Operator intent. The
23-tool surface, optional review and parameterless commit are unchanged. All
534 tests, Ruff, strict mypy and isolated Compose/PostgreSQL smoke passed. No
runtime instruction was edited and no production deployment occurred.

## Missing provider binding recovery diagnostics

The shared event validator and provider-intent service now distinguish missing
bindings from pending, executing, uncertain, failed or confirmed original create
operations. Diagnostics name only the exact canonical target and operation, with
an executable bounded delivery-status read. Ambiguous or absent create history
does not select an arbitrary operation. No provider work is retried or fabricated.

Five moved-occurrence fixtures preserve the entire cancellation draft and its
available authority, block canonical commit while the binding is absent, then
revalidate the unchanged patch as a new operation after fixture binding recovery.
They commit under the same `sreq_`, keep the series active and create no extra
event. This is an actionable wait/recovery path, not support for committing an
unbound dependent provider mutation before initial delivery.

Bound stage/review, migration and failed-commit receipts expose the actual
`semantic_request_ref` and `authority_availability_at_operation`. Replaying a
receipt preserves its historical observation; it does not promise the authority
is still available now. The generated v31 contract removes remaining internal
import-scope/coverage instructions from model protocol guidance. The existing
turn-context budget remains unchanged and passes its real rewritten-turn test.

All 539 tests, Ruff, strict mypy and isolated Compose/PostgreSQL smoke passed.
No migration, provider action or production deployment was performed by this
slice. Full amendment acceptance and deployment readiness remain incomplete.

## Exact invocation outcome recovery

Gateway reconciliation now resolves each authenticated invocation against its
own durable assembly operation. It no longer infers an earlier call's result
from the current SemanticRequest state. Stage, review, rejection and commit keep
their exact dispositions; an unknown stage reconstructed from its immutable
revision cannot become committed merely because the draft committed later.
Original signed call/source/tool/argument bindings are required, including for
transport retries. No source text or receipt payload is copied into `call_`.

Recovery also covers invocations absent from the asynchronous trace callbacks.
A later durable operation or MCP completion may resolve gateway-interrupted
unknowns while retaining the interrupted conversation. Known outcomes cannot be
replaced by later transport errors, and finalized-call attribution no longer
selects the newest request attempt. Evidence-free unknowns do not cause repeated
row locking on every retired-gateway reconciliation pass.

If a tool commits but response assembly raises, MCP returns that exact bounded
durable receipt; it does not tell Hermes the committed request failed. Repeating
this failure returns the same commitment without another canonical item. These
checks use real isolated service transactions, not fabricated provider success.

Fixtures cover missing callbacks, post-expiry outcomes, rejected/staged/reviewed
calls preceding a commit, original/retry binding mismatches, late attempt
attribution, unknown-stage reconstruction and response-assembly failure. The
PostgreSQL smoke contends gateway recovery against late MCP completion in both
lock orders and verifies one committed ChangeSet with no lost durable outcome.
The public 23-tool contract remains v31. Full callback durability, complete phase
timing, semantic request/repair/adoption acceptance and deployment gates remain
separate unfinished work.

Validation for this slice: all 557 tests, Ruff, strict mypy (135 source files)
and isolated Compose/PostgreSQL smoke passed, including both contention orders,
governance restore and migration downgrade/re-upgrade. No new migration,
production state change or provider call was required.

## Trace rows without wrapper callbacks

The v32 contract preserves signed stage/review/commit invocation rows in both
bounded history and Discord projections even if no wrapper call record arrives.
One upstream call remains one attempt while authenticated retransmissions retain
their separate invocation count. The projection labels whether transport state
comes from the wrapper or Docket; it neither adds a ToolInvocation state nor
claims response delivery from successful server processing. Missing wrapper
latency remains null and absent argument previews are explicitly unrecorded.

MCP finalization requests a projection refresh independently of wrapper callbacks.
Call cursors pin the projected invocation snapshot as well as the trace version;
newly recovered rows cannot shift an ongoing page silently. Old cursors require a
fresh read, not a compatibility decoder. Optional draft review and the 23-tool
registry are unchanged. These projections do not reconstruct lost local-only
pre-MCP rejections; durable delivery of those callbacks remains separate work.

Validation: all 558 tests, Ruff, strict mypy and isolated Compose/PostgreSQL
smoke passed. Tests cover late rows appearing without a trace-version update,
retry deduplication, exact full counts and bounded pages, strict rendering of
unmeasured wrapper latency, and finalization-triggered refresh without a callback.
No production deployment or provider action occurred.

## Immutable request interpretation records

Staging now records each new draft revision's typed request interpretation in
`semantic_request_specifications`, addressed internally by `sreq_` and version.
The record binds original utterance refs/content hashes and source/attachment
digests. It retains all normalized entries (including those with compilation
errors) and independently staged typed actions, not compiler-owned Item/Time/Event
copies, provider intents or execution preconditions. Reading requires the exact
version and verifies the stored integrity digest; it never picks the latest one.
Replays create no new proposal. Repairs retain the earlier proposal unchanged.

The record explicitly has `interpretation_state=pending_evidence_validation`.
Neither successful staging nor a source content hash proves semantic agreement.
There is no model-supplied verification flag or new authority grant, and this
record is **not yet the authority oracle for repairs**. Evidence-backed validation,
mechanical-equivalence enforcement and direct-request adoption remain open. The
existing authority hash is unchanged; `specification_hash` is only an integrity
digest of this proposal, not the semantic authority hash from ONT-UX-REQ-0009.

Migration `20260911e4b3` creates the empty append-only table without translating
historical requests. ORM guards and PostgreSQL triggers reject updates/deletes.
Downgrade refuses a nonempty table to preserve evidence. Empty-database upgrade,
downgrade and re-upgrade are rehearsed separately from the populated concurrency
database, whose immutable evidence is no longer deleted to enable a round trip.
Production recovery requires a verified pre-migration backup or forward repair,
not just rolling back the image. No production change is part of this slice.

Validation passed all 562 tests, Ruff and strict mypy (138 source files), plus
isolated Compose/PostgreSQL smoke. Fixtures cover retained invalid entries,
same-operation replay, exact-version reads, digest mismatch, rejected promotion
to a verified state, ORM/database immutability, downgrade refusal and the empty
database round trip. Source-semantic correctness is not claimed by these tests.

## Source-grounded duplicated-title repair

The v33 contract adds one deterministic repair rule to explicit `draft_recompile`.
The immutable request proposal and original normalized statement must agree on
the selected entry and title. Original utterance hashes, retained attachment
digest, extractor version and fragment coordinates/hash are checked, and the
selected title must occur literally in that PDF fragment at a word boundary.
Only the duplicated canonical/provider Event titles may be coalesced to the
already selected Item title. Every other effect remains fixed, including
cardinality, dependencies, dates, timezone, location, lane and recurrence.

Two indistinguishable Item creates can legitimately represent the same named fair
on different dates. Their already pinned graph is compared using exact existing
change IDs; rewiring or renaming one is non-equivalent in this fallback. Ordinary
semantic authority hashing still rejects ambiguous shared-target inference and
does not include those execution IDs. Its existing representation is unchanged.
The separate compiler comparator normalizes creation defaults but preserves
explicit null patches; deep typed copies keep that field-presence meaning too.

Both Event title fields are validated against the linked Item, so an outer
canonical-title mismatch cannot commit merely because its provider title agrees.
The rule records source coordinates/digests and a request-entry digest, not source
text. Compact receipts expose the count and proof hash; immutable revisions keep
the full proof bindings and original failed effects. Replaying the operation
after commit returns its recorded result without another repair or provider intent.
Explicit migration still requires a fresh observation; a bounded summary suffices.

The integration fixture verifies all three selected fairs, exact times/location/
Meetings lane, failures in either or both title fields, and rejection of an extra
event, changed title/date/destination or series expansion. A substring such as
`Fairness` does not prove the selected title `Fair`. PostgreSQL coverage stages a
faulty batch, repairs/observes/commits through separate connections, and replays
after commit with exactly three Operations and one repair audit.

This is not a general source-interpretation oracle: proposal records remain
`pending_evidence_validation`, arbitrary image interpretations are not certified,
and the exact semantic-authority binding/adoption gates remain open. The 23-tool
registry and signed artifact are unchanged. No new migration, automatic historical
execution or production deployment is part of this slice.

Validation passed all 577 tests, Ruff and strict mypy (139 source files), plus
isolated Compose/PostgreSQL smoke including source repair across fresh sessions,
existing concurrency/reconciliation cases, governance restore and migration
downgrade/re-upgrade. These are local implementation results, not live Calendar
or screenshot-interpretation verification.

## Exact update-field presence

Input serialization now preserves explicit nulls inside typed update payloads
without materializing absent fields. The old stripping path rejected valid
clear-only Item and Task-reopen patches, and silently omitted the clear when
combined with a non-null rename. This was reproduced before the fix. Conversely,
unqualified model dumps could turn omitted patch fields into explicit clears.

The same exact representation now passes through staging, immutable request
proposals/revisions, executable hashes, source/provenance completion, occurrence
compilation, optional compiled diffs and canonical application. Create/envelope
defaults and opaque JSON retain their separate semantics; no new mutation type,
schema alias or tool is introduced. Explicit clear and omitted input have
different executable hashes. Stored omissions are not reconstructed as clears,
and existing evidence/authority hashes are not rewritten.

Fixtures verify clear-only, rename-and-clear, rename without clearing, and task
reopening while keeping unrelated fields unchanged. PostgreSQL coverage stages
two updates, reloads/recompiles them through separate connections, verifies both
immutable proposals, observes the migrated revision and commits exactly once.
Ordinary staging still permits immediate commit without review; explicit
recompilation retains its fresh-observation requirement. No provider actions or
production data repair are part of this slice.

Validation passed all 586 tests, Ruff, strict mypy (139 source files) and the
isolated Compose/PostgreSQL smoke, including the new clear/reopen fixture,
existing source repair, concurrency and migration round-trip checks. No live
provider verification or production deployment is claimed.

## Exact request resumption across protocol cutover

Admission now finds the request bound to the exact originating utterance without
filtering out pre-staging or unavailable authority. Execution rechecks that
binding under the same utterance lock: a request created after admission cannot
be bypassed by a second implicit draft. Request-level locking also serializes
attempt-number allocation. Competing first stages bind one request; the stale
execution must reconcile rather than treating the other's revision as observed.

A committed direct request recovers its original receipt through stage or commit
without compilation, canonical mutation, new revisions or another request.
Cancelled, superseded and state-invalidated requests cannot be reopened by a new
trace. Multiple requests bound to the original message produce an exact binding
conflict, not a latest-request guess. Replaying a terminal failed operation does
not attach that historical failure to a request created afterward.

Integration coverage uses actual service-produced direct commits and failed
drafts, restart/reload, early admission, terminal authority and ambiguous bindings.
PostgreSQL fixtures race two pre-admitted initial stages and two resumptions of a
direct committed request. They assert one request/ChangeSet, distinct attempt
numbers, stable receipt recovery and no duplicate canonical effects.

The v34 contract keeps the 23/4 tool registries and the existing 8 KiB injected
context bound. Recovery guidance lives in the relevant tool's scoped description,
not another paragraph injected into every turn. No migration, compatibility
decoder, production change or historical auto-execution is introduced.

This closes the request-discovery and committed-receipt portions of REQ-0018.
Unfinished direct work remains preserved with `semantic_request_migration_required`;
the explicit evidence-checked adoption transition is still required before full
amendment deployment. The broader semantic-request and timing gates remain open.

Validation passed 598 tests, Ruff and strict mypy (139 source files), plus the
isolated Compose/PostgreSQL smoke with both new races, the existing occurrence,
recovery, governance and migration round-trip checks. These are local results;
no production deployment or live provider verification is claimed.

## Explicit adoption of unchanged typed direct requests

The sole `draft_adopt` staging operation provides a one-time transition for an
unfinished direct request whose current typed scope, stored revision digest and
retained evidence agree exactly. It accepts no replacement content, new scope or
expected versions. The trusted execution must have observed that exact revision;
adoption locks the request/draft and produces one new immutable revision and audit.
Neither the original utterance nor the request's original scope/hash is rewritten.

An internal `request_assembly_adoptions` row records the proof and both revision
identities. Later staging, recompilation and the shared commit service compare
against the original canonical effects and provider target/identity. A changed
title, extra effect or removal preserves the prior draft and returns a bounded
conflict. A genuine compiler failure retains the same authorized inputs as
`saved_with_errors`, not renewed Operator ambiguity. A corrected new operation
can continue; replay still returns the original failed result.

The adopted revision requires fresh observation; a bounded summary is sufficient,
not mandatory pagination. Duplicate adoption cannot create another revision/proof.
Lost-response replay and committed receipt recovery survive fresh sessions.
Old internal direct calls, with or without an explicit request binding, cannot
bypass the assembled path after adoption. The public direct protocol remains
removed. This adds a staging operation, not a new tool: the registries stay 23/4
and the generated contract is v35.

Migration `20260911f5c4` creates the proof table without backfills or historical
execution. PostgreSQL protects it against update/delete; downgrade refuses to
discard a nonempty proof table. The isolated smoke includes simultaneous adoption
attempts (one revision, one stale conflict), reobservation, fresh-session commit,
old operation replay, proof trigger enforcement and downgrade refusal. Empty
migration upgrade/downgrade/re-upgrade runs on a separate isolated database.

This is the unchanged-current-typed branch of REQ-0018, not general source
interpretation. Unversioned semantic scopes, unprovable ownership, missing evidence
or effect changes fail with their specific preserved-request constraint. Exact
source-grounded request authority and broader historical adoption remain open;
evidence digests alone do not claim the source was interpreted correctly.

Validation passed all 618 tests, Ruff and strict mypy (141 source files), plus
the isolated Compose/PostgreSQL smoke. The Calendar fixture preserves the exact
title, location, lane and provider intent through adoption and recompilation,
then queues one Operation on commit; it does not claim provider delivery. These
are local results, with no production mutation or deployment.

## Scoped canonical patch before/after previews

New staged revisions retain `canonical_patch_preview` alongside the existing
Calendar presentation snapshot. Optional diff pages use only those immutable
fields. Item, Task, Time, Reminder, temporal projection, policy, lane and registry
patches show the actual targeted canonical values and proposed replacements.
Identity bindings expose public refs rather than internal UUIDs; sender email
membership changes expose the exact ref set. Unrelated object attributes and
provenance chains are not copied into the preview.

Retraction follows each object's actual lifecycle field. Supersession shows the
old assertion becoming historical rather than falsely overwriting it with the
replacement's values. Same-draft dependencies remain explicit symbolic targets.
Case closure separates selected dispositions from system-derived `not_pursued`,
with stale/unresolved prerequisites marked unavailable rather than resolved.
Adds and replacement specifications remain in the ordinary input diff; no
duplicate compiler-owned create records or invented future IDs are introduced.

These are planned presentation effects, not commit success or new semantic
authority. Commit continues to validate exact versions. Preview fields live in
the existing versioned immutable revision manifest; this introduces no migration,
backfill or legacy decoder. Revisions without a capture stay unavailable; paging
never reconstructs their history from current state. Ordinary review stays
optional, and the tool registry remains 23/4 under contract v36.

Validation passed 641 tests, Ruff and strict mypy (142 files), plus isolated
Compose/PostgreSQL smoke. Tests cover explicit clears, Task reopening, eleven
distinct lifecycle mappings, identity association/binding, case dispositions,
filtering, lossless bounded Unicode detail, and stable pagination after another
canonical change. Stage-to-commit fixtures match captured planned fields against
the actual committed Item/Task with no review. PostgreSQL recompile/reconnect and
post-commit reads retain the original snapshot. No production change occurred.
