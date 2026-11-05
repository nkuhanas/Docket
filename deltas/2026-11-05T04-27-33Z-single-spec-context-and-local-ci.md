# Docket: one SPEC, native context, simple commands and local CI

Date: November 4, 2026 (America/Los_Angeles)
Draft timestamp: 2026-11-05 04:27:33 UTC
Status: adopted for SPEC 1.0; implementation in progress
Inspected baseline: `60c30ce5aea241b78b36fc75e51481b4680e496d`

## 1. What changes

Docket should expose personal-operations tools that an ordinary MCP agent can use.
Creating a meeting should require meeting details and a destination, with Docket
handling its own ontology, provenance, versions and provider operations.

This delta makes four coordinated changes:

1. One root `SPEC.md` becomes the current specification. Dated deltas record
   proposed and adopted changes, with decisions and history reconciled into it.
2. Local validation becomes the release authority. GitHub remains source hosting
   and an optional review surface, with no Actions wait before merge or deployment.
3. A small native test suite replaces the unconditional full-suite gate. Retired
   gateway tests and duplicate assertions are pruned rather than carried forever.
4. Bounded context packets and flat business commands replace the generic native
   ChangeSet compiler interface and its large generated instruction catalog.

The approved ChatGPT connection retains full write delegation, including scheduled
and event-triggered runs. Read, write, triage and events remain separately scoped.
There is no new human-presence requirement, approval click or Discord sign-off.

The Operator authorized implementation in the development conversation. Adoption
is recorded in SPEC 1.0 and deltas/INDEX.md; deployment is recorded separately.
The subsequent cleanup instruction also retires the Hermes/SearXNG directories
and project Playwright artifacts. No real meeting creation is authorized by this delta.

## 2. Why the current interface failed

The reported third request used a reasonable meeting-shaped object. The exported
compiler schema instead requires `import_entry_id`, a nested `timing` object with
`kind: timed`, and `lane_ref`. It rejects `entry_id`, `start_at`, `end_at`, the
top-level `timezone`, and `calendar_lane_ref` at that location.

The discriminator correction alone could never fix that mismatch. A synthetic
equivalent reproduced those validation errors. A corrected internal entry passed
Pydantic validation without staging or executing any work.

Measured from the actual native MCP registry at the inspected baseline:

| Surface | Current size or behavior |
| --- | --- |
| `docket_stage_changes` input schema | 109,578 bytes of compact JSON |
| Schema definitions in that input | 113 |
| References and unions in that input | 157 `$ref`, 174 `anyOf`, 14 `oneOf` nodes |
| Stage tool description | 988 bytes |
| Global native server instructions | 25,981 bytes |
| Internal canonical mutation variants | 47 |
| Public native tools | 30, currently treated as a fixed contract count |

The complete definitions exist in the exported schema. The interface still asks
the model to navigate an internal compiler grammar to perform a routine action.
More prose explaining `StageNormalizedEntryUpsert` is insufficient.

Hermes also had a special scoped-schema disclosure bridge. Native ChatGPT should
not need a replacement for that harness-specific mechanism just to create events.

A bounded production metadata query found no authenticated native stage or commit
invocations in its six-hour window. That is consistent with the reported client
validation failure. It does not independently establish the exact payload or
failure location of every reported attempt. No raw arguments or provider content
were queried. This remains a Docket interface defect even when the host rejects
the payload before sending it.

The test inventory contains 102 modules and 732 test functions, expanding to
1,139 cases in the latest full run. At least 13 candidate modules, containing
137 functions before parameterization, directly exercise retired Hermes/Discord
seams. Each still needs a dependency review before removal. The latest full local
pytest run took about 92 seconds, before the separate static and Compose gates.
The workflow then repeats checks in GitHub on the PR and again on merged main.

## 3. One current SPEC and an ordered delta history

Both reference projects track a root `SPEC.md`. PolyPath updates its specification,
architecture decision log, version history and delta index together when adopting
a change. Aphelion's ergonomics delta identifies the previous SPEC version, the
new target version and the exact interface requirements it supersedes.

Docket currently has no root `SPEC.md`. Its operative requirements are spread
across frozen source documents, later amendments, `AGENTS.md` and verification
notes. Consolidate those current requirements into one root `SPEC.md`.

The new document covers purpose, supported capabilities, authority and provenance,
canonical data/workflows, provider lifecycle, context/MCP semantics, privacy,
local validation, deployment/recovery, architecture decisions and change history.
Its first version must honestly identify its baseline and pending implementation;
do not invent a prior SPEC version or treat every current code behavior as an
approved requirement. Include accepted Operator amendments, particularly native
delegation, and explicitly identify superseded Hermes/Discord requirements.

Use this ongoing workflow:

1. A dated delta identifies the SPEC baseline/affected sections, previous and
   proposed requirements, rationale, compatibility, implementation and acceptance.
2. Adoption uses the Operator's current development-conversation authorization.
   An instruction to implement the delta authorizes its scoped SPEC amendment;
   there is no additional ledger ceremony or repeated approval round trip.
3. Reconcile the adopted requirements into `SPEC.md`, its architecture decision
   log and version/change history, and the forward delta index as a coherent
   change. A reader should not have to replay every delta to learn current rules.
4. Keep each delta as a historical change record with clear draft/adopted/status
   information. Record implementation, deployment and actual-host evidence
   separately and honestly; adopting a requirement does not prove it is deployed.

Use timestamped forward filenames such as
`deltas/YYYY-MM-DDTHH-MM-SSZ-short-description.md`. A small tracked forward index
can be `deltas/INDEX.md`, leaving the existing private historical registry intact.
`AGENTS.md` becomes a short operating guide pointing to the relevant SPEC sections.
`CONTRIBUTING.md` describes contributor/release practice; runbooks and verification
notes document procedures and evidence. They do not become competing specifications.
Wire models remain the authoritative machine definitions linked from the SPEC.

This is a forward provenance-policy amendment: the new, non-secret current SPEC
and forward adopted deltas/index are version controlled with their implementation,
as in the reference projects. Update ignore rules deliberately for those files.
Older private `specs/` and delta sources stay private and in their original
provenance chain. Frozen signed artifacts remain byte-for-byte unchanged. Do not
bulk-add private history or copy credentials, operator transcripts or provider
content into the new SPEC. Synthesize current requirements and retain references.

The master SPEC is a development source. MCP receives only short relevant rules
and bounded domain context; the monolithic document is never injected wholesale
into model sessions. Implementation must align the single SPEC and lean runtime
contracts rather than create another family of independent SPEC documents.

## 4. Copy the useful context patterns

Aphelion provides immutable context handles carrying the target and observed
reference revision. Its library context returns supported actions and the facts
needed to edit. Flat tools such as `create_cycle`, `prepare_entry_create` and
`prepare_entry_edit` compile server-side requests. Prepared edits are applied by
their returned handle rather than by reconstructing an internal mutation union.

PolyPath provides an initial planning packet with actual available tools,
conventions, workflow, source coverage and freshness. Retained personal context
is addressed by an owner-bound `contextId`; domain services calculate results
and return actionable next steps. Missing information remains distinct from an
empty result.

Docket should adopt those patterns:

- An initial overview packet supplies the configured timezone, current date,
  capabilities, useful defaults and a short guide to the next relevant tools.
- Domain packets supply the selected calendar, event, task, person or attention
  case, with exact references, observed state and the actions that apply to it.
- The server retains versions, selected targets and execution bookkeeping behind
  opaque handles. Context is data and correlation, never an authorization grant.
- Packets distinguish canonical facts, provider observations, reported context,
  freshness and incomplete coverage. Reading a packet never implies a refresh or
  proves that an appointment is absent from an incompletely observed calendar.
- Reuse suitable context. Refresh affected state when it changes; do not add a
  mandatory reread before every action or expire an unchanged target arbitrarily.

Docket already has triage ContextPackets, but their table requires a TriageRun and
source. General native context must have its own appropriate model or an explicit
schema evolution. Do not fabricate a triage run to obtain an editing context, or
let a triage claim become write permission.

## 5. The native MCP contract

### Routine commands

Use named business operations with small, strict inputs: create an event, update
an event, cancel an occurrence, create or complete a task, record a fact, change a
preference. The handler calls shared services; domain logic stays in services.

A routine command admits its authenticated `req_`, validates and compiles a draft,
then commits the resolved action through the existing ChangeSet engine. That can
happen inside one MCP call. The model does not manually operate the compiler.
Canonical changes and provider intent still commit atomically in PostgreSQL.

An illustrative calendar flow is:

```text
docket_get_calendar_context(...) -> calendar choices, timezone, bounded observations
docket_create_event(...)         -> committed canonical event and provider delivery state
```

Proposed event input, using handles supplied by the packet:

```json
{
  "context_id": "<returned_calendar_context>",
  "operation_id": "<stable_id_for_this_create_and_its_retries>",
  "title": "Example meeting",
  "start": "2026-11-13T13:00:00-08:00",
  "end": "2026-11-13T14:00:00-08:00",
  "timezone": "America/Los_Angeles",
  "calendar_ref": "<Meetings_calendar_ref_from_context>",
  "location": "https://example.test/meeting",
  "notes": "Reported participants and meeting notes"
}
```

These are proposed public names, not a recipe for the current generic stage tool.
Implementation must test the exact published schema and a real transport call.

Public event inputs accept documented ISO timestamps, including the offset form
above. The server normalizes them to canonical timing. Conflicting offsets/zones,
nonexistent local times and end-before-start produce specific field errors.
An offset can disambiguate a daylight-saving fold. Ordinary input should not need
internal `timing.kind`, `fold`, import IDs, support-record IDs or compiler scope.

`calendar_ref` selects a known destination; the model need not supply provider
account UUIDs or raw Google calendar IDs. Names in notes do not send invitations.
Creating invitations or other additional provider effects requires a supported,
explicit operation rather than inference from a list of participants.

Updates use context-held target versions and existing field-level provider patch
rules. Recurring changes still require the intended occurrence or series scope.
Errors block affected work and retain useful state; they do not demand renewed
authority for a schema correction or an optional archive.

### Optional prepared work

Retain an explicit prepare/read/apply flow for requested previews, larger imports
and genuine multi-object atomic work. Preparation returns a bounded preview and
an opaque immutable change handle. Applying that handle commits that exact
proposal, with current permission and target checks. An unchanged retry returns
the original receipt. A correction creates a retained revision and a new exact
handle while preserving the original request and audit chain.

The nested `work` envelope disappears from the public contract. Docket manages
request/execution identity, internal action unions, assembly scopes and draft
revisions. A single documented `operation_id` remains on direct commands where
needed; applying a prepared handle uses the handle's durable retry identity.

This changes the native concurrency contract from client-managed execution
observation to server-bound exact proposals. It preserves the important property:
an apply never means whichever draft happens to be latest. Existing historical
execution bindings and provenance are not rewritten.

### Context and contract size

The initial instructions should state scope, trust, retry and delivery rules
briefly. Domain guidance belongs in the requested context packet. Tool schemas
come from the actual friendly public models, with a small executable example for
each normal write. The generated Markdown catalog is no longer injected wholesale.
Versions and hashes support maintenance and cache refresh; normal calls need no
model-supplied contract-hash acknowledgement or schema-discovery ceremony.

Initial design budgets are 2 KiB for global instructions, 8 KiB for a normal
context packet and 4 KiB for a routine write schema. They are engineering targets
to verify against real clients, not reasons to discard required facts silently.
Existing 16 KiB normal result limits and explicit pagination remain.

The ordinary write surface has no nested generic mutation union. Validation
reports the offending public field, expected shape and a usable next step.
Client errors should not cite private Python class names as the only explanation.

The current 30-tool count is superseded. Choose clear tools by supported workflow;
do not replace it with another arbitrary count. Map all 47 current internal effect
types to the new domain surface or an explicitly reviewed retirement. Calendar,
tasks/time, people/identity/relationships/facts, preferences, reminders/routing and
attention/conflict handling all require a capability disposition before cutover.
The internal compiler can remain rich while its clients stay simple.

## 6. Local CI and release authority

Replace the mandatory GitHub status query in `scripts/docket predeploy` with an
automatic local validation result bound to the source being released and the
tested image. Run checks once for that content; an unchanged tree after a rebase
or merge does not require running them again under a different commit SHA.

The local result records the source-tree digest, lockfile/check-policy identity,
selected test groups, outcomes and tested image digest. Generate it automatically
from a stable snapshot that excludes ignored private provenance and runtime data.
It is local execution evidence, not a production ledger approval or a credential.
No extra acceptance phrase, hand-edited attestation or signing infrastructure is
required. Changed source or a missing required check invalidates the result.

`scripts/docket check` becomes the fast native gate. A named extended command runs
the additional groups needed for a storage, migration, provider or release change.
The supported deployment consumes that result and the tested artifact instead of
querying GitHub Actions or rebuilding different content after validation.

Retire automatic full-suite Actions runs for PRs and main. GitHub may continue to
host commits and PRs; merge/deployment eligibility comes from local checks. Any
optional remote diagnostic run is non-gating and does not duplicate every release.

The inspected effective branch rules contain deletion, non-fast-forward and
linear-history protections, with no required-status-check rule. The ordinary
branch-protection endpoint was unavailable. Recheck at implementation time and
change only CI requirements that actually exist; preserve the history protections.
The current wait is principally in scripts, workflow triggers and agent policy.

Update `AGENTS.md`, `CONTRIBUTING.md`, workflow files, the deploy script and active
runbooks together. This delta proposes an explicit replacement for their GitHub
CI requirements. The transition itself is validated locally rather than waiting
for the remote gate being retired to authorize its replacement.

Keep clean source, synchronized release refs, authentication, durable drain,
idle/in-flight checks, pre-migration backup, schema checks and postdeploy health.
Destructive reset/restore and unrelated provider actions remain separately scoped.

## 7. Fewer tests, chosen for current behavior

Implementation must produce a before/after inventory with actual runtimes and
the distinct failure each retained group catches. Delete or consolidate tests
for retired behavior and duplicate assertions. Moving all old checks into one
giant test, or only relabeling the existing suite, does not meet this delta.

| Changed area | Required local evidence |
| --- | --- |
| Native business/MCP behavior | Small client/schema tests plus affected native workflow on isolated PostgreSQL |
| Authentication/permissions | Signed token and local-grant checks, revocation, owner isolation, restricted credentials and untrusted-input rejection |
| Provider execution | Atomic intent, retry identity, partial delivery and unknown-after-transmission reconciliation |
| Durable models/migrations | Affected PostgreSQL constraints/locking and the relevant migration/recovery rehearsal |
| Release scripts | Local-result binding, failure handling, drain/backup and tested-image deployment checks |
| Docs, skill or presentation only | Relevant syntax, links/package consistency and examples; no unconditional database rebuild |

The default native gate should initially aim for roughly 100–200 meaningful
cases, with the retained set justified by behavior rather than a numerical quota.
Keep focused edge cases that catch different failures. Extended groups run for
relevant changes; the historical full suite is not a permanent release tax.

Prioritize removal of Hermes native-input preparation, Discord gateway hooks,
trace callback choreography, retired schema-disclosure bridging and their
test-only scaffolding. Port any still-relevant domain/authority assertion to the
native boundary before deleting its only coverage. Historical evidence remains
in Git and frozen source artifacts; it need not execute in every release.

Consolidate repeated exact tool counts, instruction wording and duplicated schema
snapshots into a small generated-consistency check. Spend contract coverage on
what the host actually receives and whether a normal agent-shaped command works.
Use disposable test databases and synthetic provider credentials throughout.

Measure a warm routine local gate against a one-minute engineering target. Larger
PostgreSQL/migration work has its own measured budget. Timing is a performance
target, not a new arbitrary authority gate, and numbers must be reported honestly.

## 8. Implementation order and cutover

1. Establish the single SPEC and forward delta workflow together with local
   validation/release evidence. Remove the remote status wait and automatic
   duplicate Actions runs, and reconcile the governing instructions.
2. Inventory and prune tests while preserving a compact native safety baseline.
   Split the native PostgreSQL smoke from legacy gateway and exhaustive rehearsal.
3. Implement the overview/calendar context and a flat event-create command as the
   first complete vertical slice. Use the shared canonical/provider services.
4. Complete event modification/cancellation, tasks and the remaining capability
   matrix. Add optional prepared work where the workflow benefits from it.
5. Cut over the native catalog and plugin instructions together. Remove generic
   compiler operations from normal public discovery and the giant injected catalog.

Use forward migrations for general tool context and prepared handles where
needed. Keep triage contexts purpose-separated. Preserve authenticated principals,
existing scopes, the registered App binding and the user's write delegation.
There is no planned OAuth reapproval or grant-epoch bump for an interface change.

Before retiring the old public contract, inventory unfinished native requests and
drafts. Preserve them and their genuine roots. Provide bounded recovery of their
exact saved state; do not delete them, invent new evidence or silently translate
unknown content. The internal engine and explicitly isolated recovery path can
outlive the public compiler interface. No legacy alias is needed in the new catalog.

Refresh the existing hosted tool registration and bundled skill after deployment.
Treat this as a native contract version change; cached old tool definitions do
not prove the new surface is active. Retain the prior image and migration-specific
recovery instructions. No production rows are edited to make acceptance pass.

## 9. Acceptance

- An independent MCP client can read calendar context and create the synthetic
  meeting above with the exact published flat schema, on its first valid attempt.
- The server derives canonical support records and one immutable ChangeSet with
  required provider intent. Retrying the same operation does not create another
  event or provider operation.
- A stale target, conflicting timezone or invalid occurrence produces a useful
  public-field error. Correcting it does not require fabricated utterances,
  attachment capture, compiler vocabulary or a fresh grant of authority.
- Normal create/update/cancel and task workflows work through real MCP transport
  and PostgreSQL with synthetic provider execution, including uncertain delivery.
- Previewed work applies only its exact prepared snapshot; context or handle
  possession never bypasses current OAuth/local-owner permission checks.
- Full delegated ChatGPT writes work from chat and automation. Read-only/triage
  credentials remain restricted. Quoted source instructions grant no permission.
- A source-bound local result authorizes the tested release without a GitHub
  Actions run. Missing/stale required evidence rejects; a content-identical merge
  reuses valid evidence. Deployment still drains, backs up and checks health.
- The root SPEC states current adopted requirements, including this delta, with
  its version, architecture decisions and delta index aligned. Historical private
  sources are preserved; MCP does not receive the whole master specification.
- Report test cases removed/consolidated, remaining native coverage, timings,
  schema/instruction sizes and capability parity. A smaller interface and a shorter
  test count without a working event workflow are insufficient.

## 10. Inspected sources and drafting verification

Docket: `src/docket/plugin/server.py`, `src/docket/plugin/work.py`,
`src/docket/schemas/assembly.py`, `src/docket/schemas/calendar.py`,
`src/docket/models/intelligence.py`, `src/docket/tool_contracts.py`, `scripts/docket`,
`.github/workflows/ci.yml`, and the routed operations/plugin/contract documentation.

Aphelion: `apps/server/src/aphelion_server/services/contexts.py`,
`services/facade.py`, `services/library.py`, `api/library_tools.py`, `api/mcp.py`
and `scripts/build_release.py`, plus root `SPEC.md` and contributor/agent guides,
under `/srv/workspaces/aphelion`.

PolyPath: `contracts/context.js`, `contracts/context.md`, `src/data/context.js`
and `src/documents/context-service.js`, plus root `SPEC.md`, contributor/agent guides
and the recorded delta workflow, under `/srv/workspaces/polypath-mcp`.

Drafting used static test inventory, actual generated native schema measurements,
synthetic input validation, read-only branch-rule inspection and bounded invocation
metadata. No full suite was rerun for this private Markdown draft. No application
code, live grant, GitHub setting, plugin release or calendar state changed.
