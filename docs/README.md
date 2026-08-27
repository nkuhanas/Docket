# Docket documentation

Start here when operating or changing the deployed stack:

* [Operations runbook](operations-runbook.md) — symptom-first diagnosis,
  reload/rebuild rules, safe recovery, and course/Calendar verification.
* [Pinned integration contracts](pinned-integration-contracts.md) — fragile
  Hermes, MCP, container, and Compose assumptions that must be revalidated on
  upgrades.
* [Specification deviations](deviations.md) — accepted differences from the
  private implementation specification and their compensating controls.
* [Milestone 2.5 verification](milestone-2.5-verification.md) — pinned runtime,
  automated evidence, live Discord transcript, and the remaining operator gate.
* [Milestone 3 verification](milestone-3-verification.md) — daily rollover,
  queue controls, archival recovery, system alerts, and the live-smoke boundary.
* [Milestone 3.5 verification](milestone-3.5-verification.md) — bounded Calendar
  synchronization, cache freshness, reminder delivery, and the controlled live gate.
* [Milestone 3.6 verification](milestone-3.6-verification.md) — full Calendar
  control, independent course reconciliation, and durable batch history.
* [Milestone 3.7 verification](milestone-3.7-verification.md) — independent
  course reconciliation, durable drop, restore, and the operator-present gate.
* [Milestone 4 Gmail verification](milestone-4-verification.md) — historical
  ingestion and isolated-triage evidence; its autonomous label-mutation model
  was retired by the authority-aware semantic workflow.
* [Semantic operations verification](semantic-operations-verification.md) —
  current authority, entity, event-correlation, brief, projection, and alpha
  retirement evidence.
* [Entity-registry hardening verification](entity-registry-verification.md) —
  validated profiles, bounded inference, relationship lifecycle, idempotent
  writes, and deployed MCP evidence.
* [Production Calendar write-gate verification](calendar-write-gate-verification.md)
  — fail-closed production writes, paused execution, stale rejection, and the
  deployed pending-card state.

These notes describe the current pinned stack. They are not a substitute for
the specification or migrations. When behavior and documentation disagree,
capture live evidence, fail closed, and update both the implementation and
these notes in the same change.
