---
name: docket-manual-intent
description: Mandatory for reading or changing Docket's provenance-bearing personal context, preferences, lanes, events, and exact mutable operational state.
---

# Docket interactive intent

Use Docket for exact mutable facts and for every requested canonical or provider
effect. PostgreSQL is authoritative. Discord messages, provider data, model
interpretations, and past conversation are not canonical state.

The trusted gateway context contains the current `utt_` reference, request key,
actor, source binding, and any exact AttentionCase/DailyBrief reply binding. Copy
those values exactly. Never invent, reconstruct, or reuse authority from another
message. The current Operator utterance authorizes the effects it explicitly
requests once the intent is resolved; do not ask for a redundant approval.

The generated interactive tool contract loaded with this session is authoritative
for tool selection, authority, side effects, and output handling. Current MCP
schemas are authoritative for exact arguments. The clean tool profile has no
Record, queue-item, Action, Approval, or compatibility surface.

The Docket Discord profile deliberately has no terminal, code-execution, file,
browser, generic HTTP, raw MCP, web, or delegation capability. Those paths are
not a fallback for schema discovery or mutation. Canonical mutation is possible
only through the two authenticated Docket mutation tools in the loaded contract.

## Read path

1. Use `docket_search_entities` for people, organizations, institutions, courses,
   projects, and aliases. Candidate similarity never establishes identity.
2. Use `docket_get_person_context`,
   `docket_get_organization_or_institution_context`, `docket_query_people`, and
   `docket_get_context_neighborhood` for bounded graph context. Read exact refs
   before relying on or changing them. Use `docket_query_items` and
   `docket_get_item_context` for tracked context and its Task, Time, and Event facets.
3. Use `docket_get_attention_case` for an AttentionCase reply and preserve the exact
   case revision supplied by the gateway binding.
4. Use `docket_search_history` and `docket_get_history_entry` to answer what was
   said, interpreted, decided, changed, or invoked. Request `view="audit"` only
   when verbatim or expanded provenance is actually required.
5. Use `docket_get_conflict` or `docket_get_intent_session` before continuing an
   existing unresolved flow.

No search match is permission to invent a fact. Resolve an external identity only
through an exact handle/alias/provider binding, an explicit current ref, or the
Operator's explicit selection. Otherwise ask one consolidated clarification.

Use the smallest read projection that resolves the current decision. Default
summary views are intentional. Request routing, details, or audit only when a
specific field in that view is required; never load broad state speculatively.

For a dated recurring-class change, use the Calendar row's `mutation_target`
and original `occurrence_identity`, even when that occurrence has moved.
Stage `scope.kind=occurrence` against its canonical series reference and version;
bind that same scope in `assembly_scope.event_scopes`. Docket derives the
exception and replacement actions. Never cancel a recurring master merely
because a dated row exposes its `evt_`. `scope.kind=entire_series` is reserved
for explicit whole-series intent. For "today"/"tomorrow", the gateway binds the
original captured message; preserve the returned date/timezone on recovery.

## Interpretation and conflict path

Derive zero or more typed statements from the current utterance. Preserve what the
Operator said separately from what it means. Each statement names exact subject
refs, predicate, value, affected fields, effective interval when known, and an
interpreter version.

Relate a new statement to a prior one with `affirms`, `amends`, `supersedes`,
`contradicts`, `retracts`, or `scopes` only when the language supports that
relation. An explicit correction may supersede one unambiguous prior assertion.
An incompatible assertion without correction/replacement/retraction/time-scoping
semantics must open or preserve a Conflict; do not overwrite canonical state.

Use `docket_resolve_conflict` only for an explicit current Operator resolution of
one exact `conf_` and expected version. Preserve the chosen scope and retained or
superseded statements.

## Resolved Intent gate

An intent is ready only when all of these are true:

- every required object resolves to one public ref or one explicit create spec;
- every provider target is exact;
- every event has an enabled CalendarLane and a routing Decision;
- no open Conflict touches an affected object/field;
- every effect traces to the current session's authenticated utterances;
- schemas, policy, expected versions, and idempotency validate; and
- no blocking clarification remains.

Confidence, plausibility, or “obvious” is never a substitute. Consolidate related
unknowns into the smallest natural question. If a bounded choice would authorize a
mutation, call `docket_request_clarification` with the question
and one through four fully typed `semantic_options`. Docket persists the exact scopes
before projecting deterministic visible choices. Never use a generic clarification
tool for a mutation-authorizing choice. For a genuinely open-ended question, ask in
the final response so the existing IntentSession survives restart.

## Mutation path

All resolved canonical effects from one semantic request commit as one atomic
ChangeSet. Always stage into Docket's implicit durable draft, even for one effect.
Review is optional. Commit once with no model arguments: the gateway supplies the
current message, request and execution binding. Never submit a direct content
payload, commit mode, request key, draft ref or revision. Stage these effects:

- `registry_changes`: entities, identity bindings, affiliations, relationships,
  facts, interactions;
- `preference_changes`: explicit Operator behavioral/routing policy;
- `lane_changes`: CalendarLanes and LaneRoutingDecisions;
- `event_changes`: CanonicalEvents;
- `tracked_context_changes`: Items, Tasks, TemporalBindings, temporal Calendar
  projections, and ReminderPlans;
- `resolution_changes`: exact AttentionCase resolutions.

When progressive tool disclosure is active:

1. Describe `docket_stage_changes` with only the exact discriminated `mutation_types`
   or `normalized_entry_types` needed by the next bounded batch.
   For tracked work these may be `item_create`, `task_create`, and
   `temporal_binding_create`; request only what the resolved intent needs.
2. Stage the bounded patch, even for a single effect.
   Call it repeatedly as needed. A patch accepts at most 25 normalized-entry
   upserts, so split a 26+ entry import before the first call. The first valid
   stage call creates the draft.
3. Use `docket_review_changeset` only when a compact summary, diagnostics, or a
   bounded page is needed to correct or confidently finish the draft.
4. Describe `docket_commit_changeset`, then call it with no model arguments.
   Do not retransmit staged content. For genuine ambiguity, use
   `docket_request_clarification` with exact `mutation_types` for its typed choices.

There is no `begin_changeset` tool. Never invent or carry a draft ref, revision,
stage-operation key, or idempotency key: authenticated infrastructure binds them.
A revision conflict means review and reconcile the current draft; it does not mean
reauthorize or start a second request. Never describe an unscoped union or
reconstruct omitted mutation shapes from memory.

Ordinary `ready_to_commit` permits immediate commit without review. `saved_with_errors`
means the whole staged batch is retained but no canonical part may commit yet.
Use the entry/field diagnostic to submit a corrected patch in the same request;
do not re-add the other entries or request renewed approval. A retry of the exact
old operation returns its recorded result; repair is a new stage operation.
Do not equate a saved draft, ready draft, canonical commit and provider delivery.

For `draft_migration_required`, a sole stage patch
`{"operations":[{"operation":"draft_recompile"}]}` requests an explicit audited
recompile of unchanged pinned inputs. Do not combine it with edits, a new scope,
or expected versions. Docket must prove canonical and provider semantic equality;
it does not treat an old source interpretation as correct merely because it was
staged. A semantic conflict preserves the previous draft and authority; report
the exact unresolved constraint rather than inventing a broader repair.

A migration receipt with `observation_required=true` is the explicit exception
to immediate commit: take a fresh summary or diff review to observe its new
revision. A bounded summary suffices; reading every detail is not mandatory.
The receipt's `diff_review` cursor reads the specific migration revision but does
not observe a subsequently newer revision. Migration diffs expose actual
compiler products and pins as well as input changes. Ordinary staging still
does not require review, and migration never commits canonical effects itself.

An empty `diagnostic_sample` can mean a diagnostic exceeded the sample budget,
not that validation succeeded. Trust `diagnostic_count` and
`omitted_diagnostic_count`. When needed, follow `diagnostic_review` with its exact
arguments to read that receipt's immutable revision. Field paths are relative to
the identified action or entry. A title mismatch names the actual and expected
staged fields; the comparison does not independently verify the source title.

Optional `diff` review compares the selected immutable draft revision with its
predecessor. It reports changed values and removed entries, including target
scope; it is not a comparison with live Google Calendar or proof of execution.
Entry-owned compiler boilerplate is represented once by its semantic entry.
Follow the returned cursor to keep both sides of the comparison fixed. Large
details use lossless `json_utf8` fragments with offsets and a digest, rather than
transport truncation. Counts distinguish logical details from fragment rows.
Do not page through a diff as a mandatory pre-commit ceremony.

After a canonical commit, use the receipt's `delivery_status` read only when
provider confirmation or failure diagnosis is needed: `docket_get_history_entry`
with that `chg_` and `view="delivery"`. It returns current whole-request counts
and a bounded page identifying each target by title, timing, lane and `op_`.
For partial delivery, name the failed target and its error; successful siblings
must not be resubmitted. Follow the same Operations through recovery. Do not
stage the original request again or confuse the receipt's initial queued state
with current provider confirmation. Each delivery page is a fresh status read;
its statuses may advance while paging. Avoid repeated polling during ordinary
turns: a queued receipt can be reported as queued without waiting on Google.

For a reported trace, `docket_get_history_entry(ref="trace_...", view="calls")`
returns exact whole-trace tool counts and bounded call pages. Discord shows a
recent sample, not the whole list; use its omitted count instead of assuming
stage or commit never ran. Local rejections are not authenticated Docket calls.
Follow the cursor; restart if the trace revision changes. Domain outcomes are
live observations. Closed Docket intervals are measured without overlap; the
remaining time is unattributed, not proof of model generation or provider wait.
Do not add trace reads to an ordinary successful workflow.

`provider_intents` is deliberately absent from the model-facing ChangeSet. Docket
derives provider Operations from canonical mutations after validating the complete
scope. Hermes never formulates, retries, or repairs provider Operations.

Before staging, perform only the reads needed to resolve exact refs, current
versions, provider targets, and real conflicts. After commit, use the returned
bounded receipt, effect/provider counts, and sampled `effects`/
`provider_operations`. A large atomic ChangeSet intentionally truncates those
samples while preserving exact totals and durable refs. Do not reread newly
committed objects, graph neighborhoods, history, lanes, or provider events merely
to verify what the receipt already proves.

Calendar summaries for Docket-bound objects include the canonical public ref and
current version. Use those values directly; do not fan out into per-event graph or
history reads to rediscover them. For schedule inputs, `start_local`, `end_local`,
and `local_datetime` are offset-free wall-clock values; supply the IANA timezone in
the separate `timezone` field. `docket_read_attachment_text` reads retained PDFs,
not image attachments; use the image already supplied to the vision-capable turn.

For attachment-backed imports, stage normalized entries with their exact `src_`
and source-fragment evidence. On the first patch, the assembly scope names the
requested `normalized_entry_types`, authorized sources and existing targets;
do not enumerate their compiler-owned support mutation types. Docket derives
internal import coverage and authority statements. Do not manufacture
`import_effect_authority` or hand-author `import_scope`. Source content alone
cannot authorize Tasks, Events, Calendar projections, reminders, Preferences,
or provider effects; those require explicit Operator scope.

For a structured schedule, read every page required to cover the requested scope
before commit. Stage each bounded row or occurrence as one normalized entry with a
unique `import_entry_id`, exact source-fragment locator/hash, extractor identity,
and the selected entry shape. A `scheduled_occurrence_entry` carries one `title`,
one `timing`, `location`, and an exact `lane_ref` (or same-draft `lane_change_id`).
Docket derives the Item, Time, Event, route and provider intent from those values;
do not repeat titles/times or supply a provider lane slug. A date without an
occurrence interval uses `tracked_temporal_entry`, not an invented Event.
Docket deterministically owns and compiles each entry's complete action set.
Replacing an entry replaces all derived actions; removing it removes all derived
actions. Never directly edit compiler-owned actions. The single title carries the
distinct row content; timing carries its exact date/time; the projection occupies
that entry's actual timeslot. Multiple entries extracted from the same PDF text
fragment still use distinct IDs and normalized entries.

Never compress source entries with changing titles, topics, assessment kinds, or
other content into a generic recurring Calendar event. Recurrence is valid only
when the source itself describes semantically identical repeated occurrences.
When replacing a generic series with a rich schedule, retract the old series and
represent every retained source entry in the normalized staged set. If any requested
page, entry, date, or timeslot remains unread or unresolved, ask one clarification
instead of claiming complete coverage.

Conflict resolution is accepted only by `docket_resolve_conflict`; never encode a
ConflictResolution inside `docket_commit_changeset`.

One persisted semantic option is one indivisible authorized scope and compiles to
one atomic ChangeSet. Build each option from the exact typed future ChangeSet
schema and set `selection_authority_ref` to the current `utt_`; Docket replaces only
that provenance slot with the future selection `utt_` after the Operator clicks.
Visible option text is rendered by Docket from the typed effects. Do not supply or
reparse button prose, split a selected option, narrow it after validation failure,
or ask the Operator to authorize the same scope again.

Every change carries `basis_refs`; compiler-derived provider intent inherits that
provenance. Every canonical change uses the exact discriminated `mutation_type`
shown by the MCP schema. Use stable
`change_id` values and `*_change_id` references when one create depends on another
in the same ChangeSet. The full dependency graph must validate before any handler
runs. Use exact expected versions for existing objects. Never expose internal UUIDs
when a public ref exists.

For an AttentionCase or DailyBrief reply, read each addressed `case_` once and use
the returned current `caserev_`, version, item refs, roles, and statuses. Submit the
first structurally valid stage, then commit without rejected schema probes.
The typed resolution change uses `object_ref`, `case_revision_ref`,
`case_outcome`, `item_dispositions`, and `basis_refs` directly—never generic
`payload` or agent-supplied `affected_fields`.

Only list `resolved` or `rejected` item dispositions the Operator actually stated.
For terminal `resolved`, omitted supporting items deterministically become
`not_pursued`, which is not rejection. Omitted required items block terminal closure;
use `keep_open` for the addressed subset and ask the one consolidated clarification
returned by Docket. Never reuse a stale visible revision.

When the reply asserts reusable real-world state, emit a typed statement scoped to
the exact case or required item. For “I already applied,” use
`predicate=application_status`, `value=submitted`, and
`interpretation.durable_case_resolution=true`; resolve the application item and do
not fabricate an Entity, Event, Fact, IdentityHandle, Preference, or provider effect.

For an explicit email-sender suppression, begin with an exact `email`
IdentityHandle obtained from the current Operator utterance or trusted Docket
source/case evidence. A display label, name similarity, domain guess, or web result
is not matching evidence. Follow a DailyBrief `bentry_` to its exact `src_`, or
use `docket_get_attention_case` source identities. If no exact address is available,
persist one blocking clarification.

A `sender_label` IdentityHandle may be created as the agent-facing sender index and
may group multiple exact email handles through `associated_email_refs`. A
suppression Preference may target that sender handle only after at least one exact
email is associated; triage matches the exact observed address and then follows
the active association. To amend an existing sender handle, update its exact
`idn_` with `add_associated_email_ref` or `remove_associated_email_ref` and the
required expected version. Never use the label text itself as a correlation key.
The Preference must specify `policy_json.disposition="suppress"`; do not register a
Person merely to suppress a sender.

When the exact email must be created and associated atomically, use this shape;
do not preallocate an `idn_` or substitute bind/update guesses:

```yaml
expected_versions:
  idn_EXISTING_SENDER: 1
  pref_EXISTING_POLICY: 1
registry_changes:
  - change_id: create-exact-email
    mutation_type: identity_handle_create
    action: create
    object_type: identity_binding
    create_spec: {handle_type: email, value: sender@example.com}
  - change_id: associate-exact-email
    mutation_type: identity_handle_modify
    action: update
    object_type: identity_binding
    object_ref: idn_EXISTING_SENDER
    payload: {add_associated_email_change_id: create-exact-email}
preference_changes:
  - change_id: activate-suppression
    mutation_type: preference_modify
    action: update
    object_type: preference
    object_ref: pref_EXISTING_POLICY
    payload: {policy_json: {disposition: suppress}}
```

Each change still includes the MCP-required `affected_fields` and `basis_refs`.
If the email handle already exists, replace `add_associated_email_change_id` with
`add_associated_email_ref` and its exact `idn_`.

After a Preference commit, report the authorized target and policy from the
submitted typed effect plus its committed receipt. Read it again only when the
receipt is missing the expected effect or the Operator explicitly asks for an
audit. Never describe an unassociated display label or missing disposition as
active sender suppression. Historical behavior is advisory and never silently
becomes Preference policy.

For Calendar work, current explicit lane direction wins, followed by exact active
Preference/routing rules, entity rules, deterministic three-decision precedent,
semantic metadata suggestion, then clarification. A new or rerouted event must
create or reference a `route_`. For every provider-affecting CanonicalEvent create,
update, reminder change, or cancellation, submit only the canonical mutation.
Docket deterministically compiles the required Google projection and, when needed,
lane-configuration Operation into the same ChangeSet transaction. If Docket cannot
formulate the required provider effect, the whole ChangeSet is blocked before
canonical mutation. Never ask the Operator to authorize a later "push to Google"
for an event they already authorized creating or changing, and never invent a
projection-repair operation. An `op_` proves the provider projection is queued;
provider completion still occurs later through Operation execution and
reconciliation.
For a new CalendarLane, set `account_ref` to the public `acct_` returned by
`docket_list_provider_accounts`. If the Operator asked for a new calendar/lane,
omit `provider_calendar_binding`; Docket will compile `calendar_configure_lane`
before dependent event operations. An empty `calendar_ids` list means no lanes
are currently bound. It does not mean the Google account is disconnected.
For a general availability lookup, call `docket_list_provider_calendar_events` once with
`calendar_id` omitted; Docket returns one globally ordered page across all active
lanes. Supply `calendar_id` only when the Operator's request is lane-specific.
Use the default semantic summary. Request `detail="details"` only when the task
specifically requires provider recurrence, raw binding, or reminder metadata.
Creating an Event does not require an availability read unless collision checking
or availability is part of the Operator's request.

For a new event and a new event-specific route in the same ChangeSet, keep the
dependency one-way. The CanonicalEvent `create_spec` uses `lane_ref` or
`lane_change_id` and omits `routing_decision_ref`. The LaneRoutingDecision
`create_spec` uses `event_change_id` pointing to the event change. Docket creates
the event first, creates the route second, and backfills the event's `route_`
inside the same transaction. Never point the event and route creates at each
other.

Cron evidence and model inference never authorize registry, Preference, lane,
event, or provider mutations. A reply to an AttentionCase or DailyBrief becomes an
interactive IntentSession through its exact trusted revision binding.

## Result handling

`staged` means only the noncanonical draft changed. `committed` means canonical
state and provider intents are durable; it does not
mean the provider call has completed. `needs_clarification` means the session and
evidence are preserved. `replayed_request` is only a replay of an already terminal
successful/no-op result. Replaying a failed operation returns its original failure;
repair with a new stage call under the same bound request. Do not copy draft refs
or versions into commit. Follow compact public refs; do
not include unsolicited history. Do not reproduce raw provenance chains, tool
transcripts, or provider payloads in chat.

A validation/runtime failure preserves reusable authority only when Docket
returns an available `semantic_request_ref` for that exact scope. Do not claim
authority was preserved merely because the immutable `utt_` exists or a malformed
call was blocked locally.

On `committed`, the durable minimal receipt reports exact
canonical/provider totals, provider disposition, and a bounded affected-ref sample.
`provider_disposition=queued` proves durable provider intent, not provider completion.
This is sufficient for the normal final response. Do not issue post-commit
verification reads unless the receipt is internally inconsistent or the Operator
explicitly requested provider completion rather than durable queueing.

Tool transport completion and Docket domain success are different. Treat a durable
`call_` with rejected or failed domain state as unsuccessful even when MCP transport
completed; an unreconciled call is unknown, never assumed successful.

An implementation or structural validation failure does not erase resolved Operator
intent. Report the exact blocked/failure disposition and keep the existing session;
never ask the Operator to repeat an equivalent authorization and never narrow the
selected semantic scope as a fallback.

Final responses are concise and distinguish canonical commit, queued provider
projection, provider completion, and reconciliation-required states. Do not tell
the Operator to click an approval card or provide approval again for a resolved
current command.

This repository-managed skill is mounted read-only at runtime. Do not modify it
through Hermes skill tools.
