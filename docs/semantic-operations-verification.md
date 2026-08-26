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
* Normal overnight Gmail presentation is silent. Durable, timezone-bound
  morning and night windows publish at most one brief per kind and local date.
  Morning decisions remain separate canonical actions, but are reviewed and
  decided through one navigable Discord message rather than a per-email card
  stream.
* Candidate window ownership is durable even when classification is delayed
  across more than one already-published boundary. Docket advances the
  candidate through each closed morning/night window until it reaches the next
  open window; it never inserts new work into a brief that was already sent.
* Isolated model triage claims one batch of at most 5 sources per run and sees
  at most 20,000 body characters per source. This keeps the claim set within
  the configured turn/context budgets, while a five-minute lease preserves
  recovery after interruption.
* A terminal canonical decision cannot retain a live actionable Discord card.
  Duplicate interactions repair the exact clicked projection.

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
* durable conflict bundles and partial provider failure;
* clicked-card convergence, duplicate-click repair, edit failure recovery, and
  restart reconciliation;
* silent multi-source overnight triage, one idempotent morning brief, one night
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
7. Observe one overnight and one waking-day boundary. Ordinary overnight
   messages must not stream individually. In the morning brief, traverse at
   least two distinct decisions, complete one proposal and one awareness or
   clarification action, and confirm every interaction updates the same single
   Discord message. Each window must emit no more than its cohesive brief.

Use the symptom table in [operations-runbook.md](operations-runbook.md) for the
first diagnostic checks when any live behavior diverges.
