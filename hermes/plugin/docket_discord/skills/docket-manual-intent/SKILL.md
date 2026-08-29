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
schemas are authoritative for exact arguments. Legacy read tools exist only to
inspect undrained state; legacy mutation tools are unavailable.

The Docket Discord profile deliberately has no terminal, code-execution, file,
browser, generic HTTP, raw MCP, web, or delegation capability. Those paths are
not a fallback for schema discovery or mutation. Canonical mutation is possible
only through the two authenticated Docket mutation tools in the loaded contract.

## Read path

1. Use `docket_network_search` for people, organizations, institutions, courses,
   projects, and aliases. Candidate similarity never establishes identity.
2. Use the specific person/organization/query/neighborhood tools for bounded graph
   context. Read exact refs before relying on or changing them.
3. Use `docket_get_triage_case` for an AttentionCase reply and preserve the exact
   case revision supplied by the gateway binding.
4. Use `docket_search_history` and `docket_get_history_entry` to answer what was
   said, interpreted, decided, changed, or invoked. Request `view="audit"` only
   when verbatim or expanded provenance is actually required.
5. Use `docket_get_conflict` or `docket_get_intent_session` before continuing an
   existing unresolved flow.

No search match is permission to invent a fact. Resolve an external identity only
through an exact handle/alias/provider binding, an explicit current ref, or the
Operator's explicit selection. Otherwise ask one consolidated clarification.

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
one exact `cnf_` and expected version. Preserve the chosen scope and retained or
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
mutation, call `docket_commit_changeset` with no content, the blocking clarification,
and one through four fully typed `semantic_options`. Docket persists the exact scopes
before projecting deterministic visible choices. Never use a generic clarification
tool for a mutation-authorizing choice. For a genuinely open-ended question, ask in
the final response so the existing IntentSession survives restart.

## Mutation path

All resolved canonical effects from one semantic request should be submitted in
one `docket_commit_changeset` call:

- `registry_changes`: entities, identity bindings, affiliations, relationships,
  facts, interactions;
- `preference_changes`: explicit Operator behavioral/routing policy;
- `lane_changes`: CalendarLanes and LaneRoutingDecisions;
- `event_changes`: CanonicalEvents;
- `resolution_changes`: exact AttentionCase resolutions; and
- `provider_intents`: external effects that follow canonical commit.

Conflict resolution is accepted only by `docket_resolve_conflict`; never encode a
ConflictResolution inside `docket_commit_changeset`.

One persisted semantic option is one indivisible authorized scope and compiles to
one atomic ChangeSet. Build each option from the exact discriminated ChangeSet
schema and set `selection_authority_ref` to the current `utt_`; Docket replaces only
that provenance slot with the future selection `utt_` after the Operator clicks.
Visible option text is rendered by Docket from the typed effects. Do not supply or
reparse button prose, split a selected option, narrow it after validation failure,
or ask the Operator to authorize the same scope again.

Every change and provider intent carries `basis_refs`. Every canonical change uses
the exact discriminated `mutation_type` shown by the MCP schema. Use stable
`change_id` values and `*_change_id` references when one create depends on another
in the same ChangeSet. The full dependency graph must validate before any handler
runs. Use exact expected versions for existing objects. Never expose internal UUIDs
when a public ref exists.

For an AttentionCase or DailyBrief reply, read each addressed `case_` once and use
the returned current `caserev_`, version, item refs, roles, and statuses. Submit the
first structurally valid ChangeSet; do not probe the mutation tool with guessed
payloads. The typed resolution change uses `object_ref`, `case_revision_ref`,
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
is not matching evidence. Follow a DailyBrief basis `item_` to its exact `src_`, or
use `docket_get_triage_case` source identities. If no exact address is available,
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

After a Preference commit, inspect/report the stored target, associated email
table, and executable policy fields. Never describe an unassociated display label
or missing disposition as active sender suppression. Historical behavior is
advisory and never silently becomes Preference policy.

For Calendar work, current explicit lane direction wins, followed by exact active
Preference/routing rules, entity rules, deterministic three-decision precedent,
semantic metadata suggestion, then clarification. A new or rerouted event must
create or reference a `route_`. For an ordinary CanonicalEvent create, omit a
separate create-event provider intent: Docket deterministically compiles the
required Google projection and, when needed, lane-configuration Operation into
the same ChangeSet transaction. Never ask the Operator to authorize a later
"push to Google" for an event they already authorized creating. An `op_` proves
the provider projection is queued; provider completion still occurs later through
Operation execution and reconciliation.
For a general availability lookup, call `docket_list_calendar_events` once with
`calendar_id` omitted; Docket returns one globally ordered page across all active
lanes. Supply `calendar_id` only when the Operator's request is lane-specific.

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

`committed` means canonical state and provider intents are durable; it does not
mean the provider call has completed. `needs_clarification` means the session and
evidence are preserved. `replayed_request` is only a replay of an already terminal
successful/no-op result. A duplicate failed draft remains failed: follow its
`next.changeset_ref` and `expected_changeset_version`, revise that exact ChangeSet,
and preserve the returned `semantic_request_ref`. Follow compact public refs; do
not include unsolicited history. Do not reproduce raw provenance chains, tool
transcripts, or provider payloads in chat.

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
