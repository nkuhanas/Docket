# Native context and local CI implementation

Requirements: [SPEC 1.0](../SPEC.md). Adopted change:
[November 4 delta](../deltas/2026-11-05T04-27-33Z-single-spec-context-and-local-ci.md).

Implementation is in progress; this report does not claim the new command catalog
is deployed. Baseline: `60c30ce5aea241b78b36fc75e51481b4680e496d`.

The next slice retains all-day/recurring events and event context links, and
composes owned previews into one atomic batch. Immutable consumption records
prevent a member from executing twice; original request/draft ownership remains
unchanged. This slice awaits catalog activation and PostgreSQL rehearsal.

| Measurement | Baseline | Implemented result |
| --- | --- | --- |
| Test modules / expanded cases | 102 / 1,139 | 90 modules / 963 retained cases (176 fewer cases net) |
| Full pytest | About 92 seconds | 160 native cases passed in 10.74 seconds; static checks passed |
| Stage schema | 109,578 bytes / 113 definitions | Pending command cutover |
| Global MCP instructions | 25,981 bytes | Pending command cutover |
| Canonical mutation variants | 47 | Capability disposition pending |

Retired gateway tests exercise Hermes hooks, Discord ingress, native image
preparation and schema disclosure. Shared domain behavior remains covered by
native OAuth, request/provenance, provider and concurrency groups. Local evidence
is automatic execution evidence, not operator approval. Synthetic business
acceptance never uses production provider accounts.

## Foundation verification

Removed 13 retired gateway modules and their scaffolding. Consolidated obsolete
release string snapshots into behavioral lifecycle tests; retained service-side
invocation signature checks independently of the removed gateway signer. The
native group checks signed OAuth/local grants, delegated writes, inbox/triage,
notifications, webhook safety and provider recovery. Release checks exercise
backup, drain/lease continuity, missing/stale evidence and image/source identity.
Native release failure injection verifies drain, backup, migration and health
failures never report a successful activation or discard the barrier unsafely.

The project Playwright directory contained local browser snapshots, not a package
dependency or UI suite; it was removed. Real local credentials and historical
runtime/backup data were preserved. The historical destructive clean-cutover CLI
was removed with the retired gateway release path; backup/restore verification
and signed historical artifacts remain available.

## First calendar slice

Commit `ec0773c` establishes SPEC/local release validation and removes the retired
source. Its clean archive passed 197 selected cases and static analysis, built
`sha256:14fff6d65798e893b5a2dce2fc6169c998cd05d32a294c2bfb91793d98c96a36`
and passed native PostgreSQL transport in 53.98 seconds overall. This is local
validation, not a production deployment receipt.

The next slice adds `docket_get_overview`, `docket_get_calendar_context` and
`docket_create_event`. Compact discovered input schemas measured 101, 249 and
1,024 bytes respectively. Calendar context carries owner/purpose-bound observed
versions, incomplete coverage and no arbitrary TTL. The flat event command
normalizes ISO offsets and compiles/stages/commits through existing services,
deriving Item/Time/Event support and one provider intent. Unchanged retries return
the original receipt; changed input under the same operation identity rejects.

New general context and exact proposal rows are separate from triage ContextPacket.
Forward migration `20261104c1d2` preserves prior data and adds immutable tables;
downgrade refuses to discard retained snapshots/proposals. Recovery of a deployed
migration uses the matching backup and migration-specific procedure, not an image
rollback or deletion of rows. The old public compiler remains available during
this implementation slice; complete capability parity, shorter instructions,
prepared-work discovery and registered-plugin refresh remain pending cutover.


## Foundation activation

[PR 56](https://github.com/nkuhanas/Docket/pull/56) merged by rebase to
`51649e38f8a6fdb90a451c526fd12c58ca754b00`. The content-identical merge reused local
receipt/tree `18255901b6218bb0787bb1b01b1abbe75d4d387f`, without Actions or a rebuild.
The clean archive passed 330 selected cases, static checks, native MCP/PostgreSQL,
migration round trips and seven focused PostgreSQL checks in 66.4 seconds.

Production activated tested image
`sha256:6290d57fc6c4d604e36e0c6189956d608eefb07a82a15f80575e5835ec442d9c`
after durable drain and backup
`backups/docket-20261105T045707Z-51649e38f8a6.dump`. Previous image is retained as
`docket-docket:rollback-20261105T045705Z`. Schema is `20261104c1d2`;
health/schema/OAuth readiness passed. Authentication grants were not reapproved.
This activation exposes the first three context/event tools alongside the old
compiler. The registered plugin refresh and full catalog cutover remain pending.

The next branch implements the named domain tools, event/task edits and exact
prepare/read/apply. Coverage includes sparse patches, current target versions,
unknown-after-write reconciliation and correction handles sharing one authentic
request with immutable revisions. No production business effects are part of
these synthetic checks.
