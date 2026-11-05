# Native context and local CI implementation

Requirements: [SPEC 1.0](../SPEC.md). Adopted change:
[November 4 delta](../deltas/2026-11-05T04-27-33Z-single-spec-context-and-local-ci.md).

Implementation is in progress; this report does not claim the new command catalog
is deployed. Baseline: `60c30ce5aea241b78b36fc75e51481b4680e496d`.

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
