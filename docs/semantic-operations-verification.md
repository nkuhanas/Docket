# Semantic operations verification

This report records the automated and deployed evidence for Docket's
authority-aware semantic workflow. The private handoff/specification remains
outside version control.

## Product invariants

* Explicit operator commands carry `explicit_user` authority and execute
  directly once their required entities and fields are resolved. Provider
  mutation alone does not create an approval requirement.
* Gmail is an evidence source. Isolated triage emits zero or more typed
  candidates; it cannot authorize a provider mutation.
* Inferred event creates, changes, and cancellations remain versioned
  formulations until the operator decides them. Existing matching Calendar
  events, duplicate source threads, unchanged updates, and repeat cancellations
  converge without another proposal. Materially newer evidence supersedes the
  current immutable proposal revision, including an operator-edited revision.
* Required provisional, unresolved, or ambiguous entities pause compilation.
  Explicit registration or correction persists the binding and resumes the
  same candidate.
* Conflict decisions are advisory choices which compile into durable provider
  operations. Partial execution remains visible as failed or reconciliation
  work rather than being reported as atomic success.
* Actionable Gmail cards project immediately at every hour. Durable,
  timezone-bound morning and night windows buffer summaries and publish at
  most one brief per kind and local date; they do not gate cards. Decisions
  remain separate canonical actions, while the cohesive brief references
  their current state and may provide aggregate review navigation.
* Candidate window ownership is durable even when classification is delayed
  across more than one already-published boundary. Docket advances the
  candidate through each closed morning/night window until it reaches the next
  open window; it never inserts new work into a brief that was already sent.
* Isolated model triage claims one source per run and sees at most 20,000 body
  characters. This keeps a provider timeout from stranding more than one
  short-lived claim, while the five-minute cadence and lease preserve bounded
  recovery after interruption.
* Non-event brief consolidation uses an explicit evidence-derived topic key,
  never title equality alone. Related follow-ups can collapse to one outcome,
  while unrelated obligations with the same generic title remain distinct.
* A terminal canonical decision cannot retain a live actionable Discord card.
  Duplicate interactions repair the exact clicked projection. The first
  acknowledged render after a decision records one content-free
  `approval.projection_converged` audit event with `decision_to_card_ms`.
  Periodic reconciliation also clears stale terminal approval-control pointers
  even when the external message already rendered its noninteractive state.
* A generic title is not event identity. Independent inferred creates with the
  same title but different material event fingerprints remain distinct unless
  a provider event ID, sender event ID, or exact event fingerprint correlates
  them.
* Even the retired classifier-facing service boundary rejects autonomous Gmail
  archive/mark-read formulations. Those actions remain isolated to the
  explicit operator path.

## Schema and migration boundary

Migrations `0017` through `0022` introduce intent authority, typed semantic
candidates, the entity registry, canonical events/provider bindings, durable
brief windows, and operation bundles. Migration `0023` terminalizes residual
pending, failed, or snoozed alpha Gmail archive/mark-read cards. Its downgrade
deliberately does not recreate obsolete approval authority. Migration `0024`
adds durable aggregate-brief review state and expands the signed navigation
range without changing the canonical identity of any contained decision.

The waking interval is configured once with
`DOCKET_WAKING_WINDOW_START_HOUR`, `DOCKET_WAKING_WINDOW_END_HOUR`, and
`DOCKET_TIMEZONE`. Start must precede end on the same local day.

## Automated evidence

The repository gate exercises:

* direct create/update/cancel and canonical reconciliation;
* entity creation, aliasing, correction, relationship preservation, merge, and
  clarification resumption;
* zero-candidate extraction and duplicate-thread idempotency;
* inferred proposal adoption through one approval and one provider operation;
* inferred proposal versioning, cross-source exact-match no-op, unchanged-update
  no-op, pending-create update/cancellation reconciliation, and repeat
  cancellation correlation without replacement event details;
* independent Google Calendar edits recorded as provider divergence without
  rewriting canonical event state;
* source-safe Discord proposal rendering;
* adversarial same-title/different-time event independence and inferred
  conflict presentation;
* durable conflict bundles and partial provider failure;
* clicked-card convergence, one durable convergence-latency sample,
  duplicate-click repair, edit failure recovery, and restart reconciliation;
* always-on actionable projection, one idempotent morning brief, one night
  closeout, delayed classification blocking, missed-boundary catch-up, and
  multi-boundary delayed-source carry-forward without loss;
* one-message morning review navigation, authenticated field edits, approval,
  local actions, child-operation completion, and aggregate-card refreshes with
  no separately published child cards;
* night closeout topic consolidation across sources, resolved outcomes,
  unresolved obligations, and awareness without per-email replay;
* migration `0023` cleanup of a residual snoozed housekeeping card.

Run the complete gate with:

```text
UV_CACHE_DIR=/tmp/docket-uv-cache scripts/docket check
scripts/docket compose-smoke
```

## 2026-08-26 authority-path closure

Revision `f8acdef` is deployed on the local stack. Its ancestry includes three
alpha-contract corrections that keep the implementation and its tests on the
same authority model:

* `d5f2bcc` rejects request keys owned by retired `docket_remember_record`
  commands instead of replaying them through `docket_store_record`. The four
  historical rows remain unchanged as evidence.
* `c20ccc5` removes the forced course-proposal service path. Explicit,
  conflict-free course reconciliation now executes directly in lifecycle
  coverage; only a real Calendar conflict creates a decision. The signed
  course conflict selector now produces a fresh, approvable revision.
* `48cb4af` removes the forced standalone Calendar proposal path. Tests use
  `apply_explicit` for trusted Discord intent and provenance-bound
  `formulate_inferred` for Gmail-derived formulations.

The release gate passed 279 tests plus Ruff and strict mypy, and GitHub CI run
`33028476609` passed both project checks and the isolated Compose smoke. The
deployed stack reported all services healthy with zero active operations and
zero pending outbox rows. The image contains `cryptography` 50.0.0; GitHub
marked CVE-2026-69247 fixed after commit `f8acdef`.

These checks prove the service boundary and deterministic behavior. They do
not replace the remaining operator-present observations in
[semantic-acceptance-matrix.md](semantic-acceptance-matrix.md).

## Operator-present verification

After a deploy or Hermes plugin change, run `/reload-mcp` and begin a fresh
turn. Verify these bounded behaviors before treating the live integration as
accepted:

1. Describe a complete event using an existing entity. Docket should execute
   it directly and produce no approval card.
2. Mention an unknown required organization. Hermes should offer registration;
   after confirmation, the original operation should resume without restating
   the event.
3. Feed one inferred invitation. Exactly one proposal should identify its
   concise email relationship, and approval should execute without a second
   decision.
4. Feed a confirmation matching an event already on Calendar. It should produce
   no proposal.
5. Exercise a real conflict through Keep both, Proposed event wins, and Existing
   event wins. Confirm the provider result and terminal card in each case.
6. Click an already-consumed card. The response should report the existing
   decision and the exact visible card should converge to terminal state.
7. Observe one overnight and one waking-day boundary. Proposals, conflicts,
   clarifications, and action-required cards must project immediately in both
   windows. Routine awareness/noise must not stream individually. The morning
   brief should summarize the overnight window without duplicating decision
   objects, and each window must emit no more than its cohesive brief.

Use the symptom table in [operations-runbook.md](operations-runbook.md) for the
first diagnostic checks when any live behavior diverges.
