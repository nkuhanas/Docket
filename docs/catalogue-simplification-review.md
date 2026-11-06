# Existing catalogue: consolidation review

Review date: November 5, 2026. Source baseline:
`c51e4b388389ad481fa52ff685b689270d0a38e5`.
Status at review: proposed amendment to the
[work-attribution delta](../deltas/2026-11-05T21-18-26Z-work-attribution-and-personal-operations.md),
not an implemented catalogue change. Subsequent adoption is in [SPEC 1.1](../SPEC.md)
and actual phase/release evidence is in the
[implementation report](work-attribution-implementation.md). The projections below
retain their original reviewed baseline; they are not activation evidence.

Subsequent C0 implementation: the primary 84 → 71 reduction is implemented and
checked, with cached-client aliases outside discovery. The four conditional
removals remain deferred because their current routes are not behaviorally
equivalent. Work, policy, documents, expenses, invoices, settlement and operational
review/visibility add 37 purpose-built definitions, making the current source
catalogue 108 tools. This adds domain capability without making a shared command's input more conditional;
runtime introspection also confirms 108 definitions. The hosted plugin is 2.1.0;
fresh-chat discovery remains the separate pending check in the
[activation receipt](../evidence/work-attribution-release/activation.json).
See the [current contract measurements](../evidence/work-attribution-release/contracts.json)
and implementation report; the projection tables below retain their original baseline.

There are clear reductions that preserve the surviving input schemas. The largest
group is twelve retraction wrappers with identical arguments apart from the target
reference pattern. A second group contains convenient aliases of existing reads
and updates. Rich domain reads and provider operations need more care: similar
names do not establish equivalent behavior.

## Primary slice: 84 to 71 tools

| Current discovery | Proposed route | Net reduction | Evidence and implementation boundary |
| --- | --- | ---: | --- |
| Twelve `docket_retract_*` tools | One `docket_retract_record` | 11 | Identical five-field schemas apart from target pattern. Resolve the closed target prefix to its existing typed mutation/handler; preserve domain checks and provider intent. |
| `docket_complete_task` | `docket_update_task(state="completed")` | 1 | `RoutineCommandService.task` uses the same task-update path and completion timestamp rule. The surviving schema already has `state` and `completed_at`. |
| `docket_get_conflict` | `docket_get_context(refs=[conflict_ref])` | 1 | Both already use `ConflictService.projection`; general context additionally supplies an owner-bound snapshot/version and resolution action. |

The retraction command retains `context_id`, `operation_id`, `target_ref`, optional
`preview` and optional `replaces_change_id`. It adds no action/type selector or
payload. The reference prefix selects the internal effect; it is not authority.
The prototype input is 612 bytes, with the same required fields and structure as
the old wrappers. Twelve supported targets are a closed allowlist:
Entity, IdentityHandle, Affiliation, Relationship, Fact, Preference, CalendarLane,
Item, TemporalBinding, Task, TemporalCalendarProjection and ReminderPlan.

This is logical retraction using each domain's existing handler, not generic row
deletion. Dependencies, historical versions, source roots, lane/projection behavior,
and atomic provider intent remain domain-specific. A context advertises whether
retraction is allowed for that target. Event cancellation and identity unbinding
are excluded from this command.

The [measurement record](../evidence/catalogue-simplification/metrics.json) maps all
47 current canonical mutation variants to proposed primary tools. This verifies
that the design names a route for every effect; it does not prove a service adapter
has been implemented or tested. Non-mutation capabilities such as context, triage,
capture and saved-work recovery must also remain available.

## Conditional slice: up to four more removals

| Candidate | Surviving tool | What must be proven before removing the wrapper |
| --- | --- | --- |
| Person, organization/institution and Item detail reads | `docket_get_context(refs=[...])` | Add bounded domain facets based on stored entity kind, not a new input selector. Preserve profile/handle, affiliation, hierarchy, task/time/event/source and provenance coverage. The current generic row projection is not an equivalent replacement. |
| `docket_cancel_event` | `docket_update_event(status="cancelled", ...)` | Preserve exact event/occurrence/series cancellation semantics, guards, canonical cancel effect, one provider intent and uncertain-delivery recovery. |

For rich reads, reuse domain projection services rather than teaching the agent to
reconstruct graph joins. Supply compact facets, public links, coverage and allowed
actions within the existing packet budget. Further detail must remain reachable
through bounded context, graph/query and history reads. Do not delete a dedicated
read if full required coverage cannot be retained without more input fields,
response complexity or lost data. This slice remains conditional rather than
claiming the three reads are already interchangeable.

Cancellation needs an internal routing change. Today `CancelEvent` compiles a
`canonical_event_cancel` mutation, while `UpdateEvent(status="cancelled")` compiles
`canonical_event_modify`. Provider planning recognizes both as cancellation, but
that alone does not establish equal recurrence, version or historical semantics.
Use the existing `status`/scope fields to select the guarded cancel compiler path;
never just rename a status edit and assume equivalence. Mixed cancellation/edit
requests must preserve every requested effect or return a field-specific error;
they must not silently discard fields.

## Keep distinct operations where a merge increases complexity

- Create and update retain their own required fields. Combining them into upsert
  would introduce conditional targets and requiredness.
- Update and replace/supersede remain distinct: Affiliation, Fact and Relationship
  updates change validity/status, while replacements contain new substantive data.
  Temporal replacement also has its own lineage. A generic action/patch union would
  shift the internal grammar back onto the agent.
- Identity unbinding stays separate. Both it and handle retraction take `idn_`
  references, but they have different effects; the prefix cannot resolve intent.
- Search/list/detail, exact prepared proposals, saved historical revisions and
  attachment evidence retain their different filtering, identity and paging rules.
- Calendar choices, provider coverage/observations and lane routing/audit reads
  are not the same data. Profile discovery also has protocol metadata that general
  overview does not replace. Keep these boundaries without adding a mode router.
- Restricted triage/case/source reads remain separated from canonical editing and
  optional capture. Consolidation must not grant a background credential more power.

Tasks/events already derive support records, task due-time mutations and provider
intent internally. Preserve that behavior: ordinary commands must not require a
sequence of Item, Time, routing or projection CRUD calls. Derive only support
needed for the authorized intent; a one-off calendar choice is not permission to
invent a standing routing policy. Standalone temporal obligations, visibility
projections and routing records remain useful independent capabilities, so their
advanced commands are not counted as removable without an equivalent workflow.

## Measured inventory projections

The synthetic [probe](../evidence/catalogue-simplification/probe.py) inspects the
actual current `PluginMCP`, checks the twelve retraction input shapes, registers
only a schema prototype and projects discovery after wrapper removals. Its callback
rejects execution. It uses synthetic settings in a temporary directory, no `.env`,
database, provider, OAuth flow or personal records.

All surviving input schemas are held byte-for-byte unchanged. Discovery numbers
include descriptions, output schemas, annotations and OAuth metadata; surviving
descriptions are held constant, so final guidance changes must be remeasured.
Numbers are compact UTF-8 JSON, not token counts or deployed inventories.

| Scenario | Tool count | Discovery bytes |
| --- | ---: | ---: |
| Current source | 84 | 119,691 |
| Primary consolidation | 71 | 105,588 |
| Primary plus six P0a tools | 77 | 120,338 |
| Primary plus complete P0 | 84 | 132,185 |
| Primary and conditional consolidation | 67 | 102,437 |
| Both slices plus six P0a tools | 73 | 117,187 |
| Both slices plus complete P0 | 80 | 129,034 |

P0 additions reuse the earlier
[work-schema measurements](../evidence/work-attribution-contracts/metrics.json),
whose digest is pinned in this measurement. They are not yet implemented tools.
The primary slice saves 14,103 discovery bytes; with P0a, discovery is only 647
bytes above today's source inventory. Complete P0 with primary consolidation has
the same tool count as today, while adding actual-work capabilities. These are
planning results, not permanent tool-count quotas. Per-tool and gross-addition
budgets still apply; removals cannot justify oversized remaining contracts.

## Implementation and compatibility

This work can ship independently or alongside P0a; conditional reductions do not
block useful work logging. Before retiring public wrappers:

1. Route consolidated calls through the current native authentication, request/
   invocation attribution, versions, exact preview and atomic ChangeSet services.
   Keep existing scope/local-owner checks, restrictions and domain handlers.
2. Verify every mapped effect on isolated PostgreSQL, including retraction
   dependencies/provider intent and retries. Exercise useful end-to-end journeys;
   avoid a second family of wording/count snapshot tests.
3. Prove rich-read coverage and cancellation behavior separately before counting
   the conditional savings. Preserve the 8 KiB routine context/16 KiB normal result
   budgets and field-specific errors.
4. Preserve old receipts and exact saved drafts. A compatibility alias may stay
   outside ordinary discovery during client refresh, with its original permission
   and idempotency checks. Never relabel historical calls or replay old work as a
   fresh command. Aliases do not substitute for an implemented replacement.
5. Update the 47-effect disposition, context action names, schema/contract metadata,
   generated reference and the same hosted plugin/skill. Remeasure actual discovery
   under identical grants and report gross additions plus net inventory. Existing
   OAuth delegation needs no new scope or owner reapproval.

Inspected source trace: [native input models](../src/docket/schemas/native_commands.py),
[business definitions](../src/docket/schemas/business.py),
[business dispatch](../src/docket/services/business_commands.py),
[task/event compilation](../src/docket/services/routine_commands.py),
[native context](../src/docket/services/native_context.py),
[conflict projection](../src/docket/services/conflicts.py),
[person/organization facets](../src/docket/services/network.py),
[Item facets](../src/docket/services/tracked_context.py) and
[provider planning/validation](../src/docket/services/change_sets.py).

Reproduce design measurements with
`.venv/bin/python evidence/catalogue-simplification/probe.py`. The current review
changes no runtime handler, published schema, permission or adopted SPEC.
