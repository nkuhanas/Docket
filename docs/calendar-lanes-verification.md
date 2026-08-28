# Calendar lane verification

This record captures the production rollout of the five-lane Google Calendar
model on 2026-08-27 (America/Los_Angeles).

## Published and deployed

* Commit: `67d78ee4d229a4e4fa9c0b10e8f04095daf249b6`
* Migration: `0026`
* Hermes plugin: `0.15.9`
* Local gate: 299 tests, Ruff, Mypy, migration coverage, and isolated Compose
  MCP smoke passed.
* GitHub CI: test/static-analysis and isolated container jobs passed.
* Deployment created an encrypted PostgreSQL backup and retained the previous
  image under a rollback tag before replacing Docket and Hermes.

The operator completed a fresh OAuth consent flow before deployment. Docket's
credential validator accepted the Calendar event, Calendar property, and
Calendar-list presentation scopes without exposing token contents.

## Live provisioning evidence

The trusted Discord agent read the lane registry twice and submitted four
explicit configuration mutations. Durable evidence converged as follows:

| Lane | Name | Color | Version | State |
| --- | --- | --- | --- | --- |
| `academic` | `Docket · Academic` | `#3F51B5` | 2 | active |
| `work` | `Docket · Work` | `#D50000` | 2 | active |
| `organizations` | `Docket · Organizations` | `#0B8043` | 2 | active |
| `personal` | `Docket · Personal` | `#8E24AA` | 2 | active |
| `unsorted` | `Docket` | `#F6BF26` | 1 | active |

All four `calendar_configure_lane` operations succeeded. The corresponding
four `action.execution_queued` and four `calendar_lane.configured` audit rows
exist, no lane has a provider error, and the MCP trace records four successful
configuration calls. The Google provider accepted secondary-calendar
creation/property updates and RGB Calendar-list presentation updates. Opaque
Calendar IDs are intentionally omitted from this record.

## Sync and migration boundary

Each active lane has a current, successful bounded Calendar snapshot. The four
new lanes contained zero events at verification time; `unsorted` retained all
209 existing cached events. Therefore provisioning did not move, copy, or
recolor historical events.

Existing course series linked to `unsorted` are protected by
`course_lane_migration_required`. Academic reconciliation cannot duplicate
them into the new lane. A future explicit lane-migration workflow must preserve
provider identity and partial-failure honesty before those series move.

At handoff, Docket and PostgreSQL were healthy, Hermes was running with the new
plugin/tool registry, and the durable operation plus outbox counts were both
zero.
