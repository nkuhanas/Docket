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
unknowns into the smallest natural question. If clarification is required, call
`docket_commit_changeset` with no content and the blocking clarifications so the
IntentSession survives restart.

## Mutation path

All resolved canonical effects from one semantic request should be submitted in
one `docket_commit_changeset` call:

- `registry_changes`: entities, identity bindings, affiliations, relationships,
  facts, interactions;
- `preference_changes`: explicit Operator behavioral/routing policy;
- `lane_changes`: CalendarLanes and LaneRoutingDecisions;
- `event_changes`: CanonicalEvents;
- `resolution_changes`: exact Conflict resolutions; and
- `provider_intents`: external effects that follow canonical commit.

Every change and provider intent carries `basis_refs`. Use stable `change_id`
values and `*_change_id` references when one create depends on another in the same
ChangeSet. Use exact expected versions for existing objects. Never expose internal
UUIDs when a public ref exists.

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

After a Preference commit, inspect/report the stored target, associated email
table, and executable policy fields. Never describe an unassociated display label
or missing disposition as active sender suppression. Historical behavior is
advisory and never silently becomes Preference policy.

For Calendar work, current explicit lane direction wins, followed by exact active
Preference/routing rules, entity rules, deterministic three-decision precedent,
semantic metadata suggestion, then clarification. A new or rerouted event must
create or reference a `route_`. Provider intents target the committed event/lane;
provider completion occurs later through `op_` execution and reconciliation.

Cron evidence and model inference never authorize registry, Preference, lane,
event, or provider mutations. A reply to an AttentionCase or DailyBrief becomes an
interactive IntentSession through its exact trusted revision binding.

## Result handling

`committed` means canonical state and provider intents are durable; it does not
mean the provider call has completed. `needs_clarification` means the session and
evidence are preserved. `replayed_request` is the same idempotent result. Follow
the compact `next` field and public refs. Do not reproduce raw provenance chains,
tool transcripts, or provider payloads in chat.

Final responses are concise and distinguish canonical commit, queued provider
projection, provider completion, and reconciliation-required states. Do not tell
the Operator to click an approval card or provide approval again for a resolved
current command.

This repository-managed skill is mounted read-only at runtime. Do not modify it
through Hermes skill tools.
