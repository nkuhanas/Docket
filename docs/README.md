# Docket documentation

Start here when operating or changing the deployed stack:

* [Operations runbook](operations-runbook.md) — symptom-first diagnosis,
  reload/rebuild rules, safe recovery, and schedule/Calendar verification.
* [Pinned integration contracts](pinned-integration-contracts.md) — fragile
  Hermes, MCP, container, and Compose assumptions that must be revalidated on
  upgrades.
* [Specification deviations](deviations.md) — accepted differences from the
  private implementation specification and their compensating controls.
* [Milestone 2.5 verification](milestone-2.5-verification.md) — pinned runtime,
  automated evidence, live Discord transcript, and the remaining operator gate.

These notes describe the current pinned stack. They are not a substitute for
the specification or migrations. When behavior and documentation disagree,
capture live evidence, fail closed, and update both the implementation and
these notes in the same change.
