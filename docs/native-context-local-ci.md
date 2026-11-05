# Native context and local CI implementation

Requirements: [SPEC 1.0](../SPEC.md). Adopted change:
[November 4 delta](../deltas/2026-11-05T04-27-33Z-single-spec-context-and-local-ci.md).

The complete native catalog is deployed, plugin v2.0.0 is published and live
native-tool use is verified. Baseline: `60c30ce5aea241b78b36fc75e51481b4680e496d`.

The implementation retains all-day/recurring events and event context links, and
composes owned previews into one atomic batch. Immutable consumption records
prevent a member from executing twice; original request/draft ownership remains
unchanged. Catalog activation and PostgreSQL rehearsal passed as recorded below.

| Measurement | Baseline | Implemented result |
| --- | --- | --- |
| Test modules / expanded cases | 102 / 1,139 | 91 modules / 982 retained cases (157 fewer cases net) |
| Full pytest | About 92 seconds | 179 native cases passed in 15.74 seconds; static checks passed |
| Stage schema | 109,578 bytes / 113 definitions | 2,650-byte create / 3,352-byte update; all write schemas ≤3,508 bytes |
| Global MCP instructions | 25,981 bytes | 1,643 bytes |
| Complete warm local gate | Unconditional full suite plus static checks | 17.44 seconds for 179 cases, Ruff and strict mypy |
| Canonical mutation variants | 47 | All 47 retained; [mapping](native-capabilities.md) |

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


## Candidate catalog cutover

The candidate has 84 tools (54 writes, 27 reads and three restricted triage tools).
This is measured inventory, not a fixed count invariant. Generic stage/review/commit
and the public three-field work envelope leave native discovery. Exact apply handles,
flat event/task commands, named domain operations, conflict/clarification, optional
reported capture and owner-scoped saved-work recovery replace that choreography.
The global runtime guidance is separate from the generated reference catalog.
[Routine examples](native-command-examples.md) use the actual flat contracts.

The pre-cutover production metadata inventory found zero native authenticated
requests and zero native drafts, including zero unfinished drafts. It queried only
state/count metadata, without raw arguments, content or credentials. Historical
service-backed state remains retained; no rows or authentic roots were rewritten.

Forward migrations `20261104d2e3` and `20261104e3f4` retain immutable batch consumption
and bounded workflow receipts. Downgrades refuse to discard populated tables.
The candidate smoke uses signed OAuth over real MCP/PostgreSQL: synthetic event
create/retry/update/cancel, uncertain fake-provider reconciliation, tasks, exact
preview correction races, atomic batching, optional capture and current revocation.
This remains synthetic acceptance, not a real calendar operation or host UI test.


## Final activation — November 5, 2026, 06:22 UTC

[PR 57](https://github.com/nkuhanas/Docket/pull/57) merged by rebase to
`2ce9617c9328c3b6ca9637b3caa5989f76125e45`. It reused validated content tree
`9dcc9d0e5b8fe72bac1f869f6af0deec00f1ad2a` and the exact tested image:
`sha256:b3919a01a31ec0a823b1732888966af8aff40bfa0123cf190c718116803ef013`.
No GitHub Actions wait or post-validation rebuild occurred.

Clean-archive validation selected native/storage/release groups: 339 cases passed
in 36.67 seconds, plus Ruff and strict mypy. Overall checks/build/PostgreSQL smoke
and migration round trips took 79.73 seconds. Native transport passed synthetic
create/retry/update/cancel, tasks/completion, uncertain fake-provider reconciliation,
concurrent corrections (one wins, one stale), exact apply, atomic batches/member
retry, optional encrypted capture, workflow receipts and revocation. Seven focused
PostgreSQL locking/continuity checks also passed. The full retained inventory is
91 modules / 982 cases, 157 fewer expanded cases than baseline. The routine native
gate is 179 meaningful cases, measured at 15.74 seconds before static checks.

Supported predeploy/deploy passed with durable drain, idle checks and backup
`backups/docket-20261105T062218Z-2ce9617c9328.dump`. Previous image is retained as
`docket-docket:rollback-20261105T062216Z`. Production schema is `20261104e3f4`;
Docket/PostgreSQL health and native OAuth readiness passed. The three stopped
Hermes/Discord-ingress/SearXNG containers were removed without force or volume
removal. Their images, persistent volumes and private historical runtime evidence
remain recoverable. Aphelion API/worker and local Ollama remained running.

Deployed registry inspection measured 84 tools / 54 writes, 1,643-byte global
instructions and maximum 3,508-byte write schema. Generic compiler tools, public
work envelopes and retired internal callback mounts are absent. The OAuth client
binding is unchanged. Public HTTPS discovery returns 200 with the expected issuer
and four scopes; anonymous MCP returns 401 with its OAuth challenge; internal
callback ingress returns 404. Public DNS succeeds through DoH; the node's previously
known local resolver issue remains separate from application health.

The existing [Docket plugin](https://chatgpt.com/plugins/plugin_asdk_app_6aebd82ba1108191a77747be377965f8)
was updated through Plugin Creator to v2.0.0, release
`pluginrel_6aec20d4dd888191a2178affc82de5b8`. Five-file read-back exactly matched
the candidate archive. App identity, connection binding, presentation/defaultPrompt,
scope and private audience were preserved. No owner reapproval or grant-epoch bump.
A genuine connected-host profile read passed against the existing approved owner.

At activation, this host session still exposed a cached 24-tool inventory and old
instructions. That was recorded as pending acceptance until live new-tool use could
be verified. Release testing used independent signed MCP/PostgreSQL clients and
synthetic provider execution. The plugin source update used the Plugin Creator
update-plugin workflow. The later host acceptance below closes this pending step.

These documentation receipt changes are recorded after activation; they do not
alter the tested runtime image or its exact source-tree evidence. Image rollback
alone does not reverse migrations. Use forward repair or the matching backup and
migration-specific recovery; destructive production restore remains separately scoped.


## Live host acceptance — November 5, 2026

The Operator confirmed that the deployment is functional. A read-only production
query independently verified these authenticated native invocations under
`docket-tools-2026-11-04-v51`:

| Tool | Successful invocations observed | Recorded disposition |
| --- | --- | --- |
| `docket_get_overview` | 3 | Successful read |
| `docket_get_calendar_context` | 1 | Successful read |
| `docket_create_event` | 1 | `committed` |

The query selected only tool names, outcomes, contract versions and aggregate
counts. No arguments, retained text, event content or OAuth material were queried.
The verification itself created no canonical/provider effects. These live calls
verify the new native surface beyond the older cached definition snapshots. The
runtime still matches the tested image/tree above and is healthy. This Codex
session retains an older 30-tool snapshot; the working connection and its v51
calls provide the live acceptance evidence.

The complete warm `scripts/docket check` also passed in 17.44 seconds wall time:
179 cases, Ruff and strict mypy. This measures the whole command, supplementing
the earlier pytest-only timing. All implementation, local-release, cleanup and
host acceptance requirements for this delta are verified; no operator step remains.
These final documentation changes require link/package checks, with no runtime
redeployment or migration rehearsal.
