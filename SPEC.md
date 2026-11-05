# Docket specification

Version: 1.0
Adopted: November 4, 2026
Implementation baseline: `60c30ce5aea241b78b36fc75e51481b4680e496d`

## 1. Status and change control

This is Docket's single current specification. It consolidates the signed ontology
and Operator amendments, including the adopted [context/local CI delta](deltas/2026-11-05T04-27-33Z-single-spec-context-and-local-ci.md).
Adoption and deployment are distinct. The native command/context release is
implemented and deployed; plugin v2.0.0 is published. The existing ChatGPT catalog
still needs a host refresh; its old cached tools are not acceptance evidence. Actual implementation
and deployment evidence belongs in [the implementation report](docs/native-context-local-ci.md).

Forward changes use dated `deltas/YYYY-MM-DDTHH-MM-SSZ-description.md` records,
identifying the SPEC baseline, changed requirements, rationale, compatibility,
implementation and acceptance. An Operator instruction to implement adopts the
scoped amendment. Reconcile SPEC, its decisions/history and [delta index](deltas/INDEX.md)
together. Readers need not replay every delta to discover current requirements.
Wire models remain machine definitions; AGENTS is an operating guide;
CONTRIBUTING and runbooks describe procedures, not competing specifications.

Track the new SPEC and forward deltas. Older ignored `specs/` and delta sources
remain private, in their existing provenance chain. Do not bulk import, rewrite
or relocate them. Signed `ONT-DELTA-2026-08-27` remains frozen at SHA-256
`3d744f4d021f8a605086152eb76743a7ec5a7ed2c8754694e38c1a891a14b5e1`.
The August 26 document remains historical, outside that authority chain.

## 2. Purpose and capabilities

Docket is a credential-bearing, single-operator personal operations service.
It retains durable authority, evidence, canonical personal context, triage state
and provider-operation intent. PostgreSQL is the sole durable system of record.
Provider APIs, agent output, transport sessions and logs are observations or
projections, never independent canonical authority.

The native OAuth MCP plugin at `docket.you/mcp/` is primary. Connected agents
supply inference and scheduling; local inference can use the same bounded
workflows. Docket requires no particular inference provider or agent cron.
Hermes, Discord and SearXNG are retired interfaces. Hermes/SearXNG source trees,
gateway tooling and project browser-automation artifacts are removed under the
Operator's additional cleanup instruction. Historical evidence remains in Git.

Capabilities include calendars/occurrences; tasks/items; time/temporal projections;
people, organizations and identity; affiliations, facts, relationships and
interactions; preferences, reminders and lane routing; attention cases, daily
briefs, retained notifications and conflict resolution. Native cutover must
account for all 47 baseline canonical mutation variants. An omitted capability
requires explicit reviewed retirement, not accidental discovery loss.

## 3. Authority and evidence

Verify OAuth issuer, audience, approved client, token dates and local owner grant.
`docket.read`, `docket.write`, `docket.triage` and `docket.events` remain separate.
Both verified token and current local owner grant must permit the operation.
Schema changes do not require owner reapproval or a grant-epoch bump. Other
service credentials retain their distinct roles and limits.

The approved native connection has full delegated write authority in live chats,
scheduled runs and event-triggered runs. Interactive role denotes capability,
not proof of human presence. No foreground attestation, separate approval click,
Discord sign-off or ledger ceremony is required. Untrusted content and model
inference cannot grant permission. Restricted triage produces intelligence or
bounded suppression under an existing Preference, never inherited write authority.

Before write execution, admit durable authenticated `req_` attribution and audit.
Every authenticated invocation creates `call_`, including validation, permission,
conflict and service rejection. Canonical mutations carry public `basis_refs`
tracing operator-owned work to its authenticated request. Historical work retains
its genuine utterance roots and original constraints. Public references cross
tool/audit boundaries; internal UUIDs do not.

Transcript, attachment and response capture is optional, separately committed and
honestly agent-reported. Missing archives do not block valid work. Never fabricate
`utt_` evidence for relayed text. Later input does not destroy earlier evidence.
Evidence truth and canonical truth remain distinct. Never persist raw arguments,
results, authorization headers, Gmail bodies or transcripts in invocation logs.

## 4. Canonical workflow and concurrency

Handlers authenticate and validate; services own business rules/transactions.
Resolved intent compiles to an immutable `chg_`. Canonical state and required
provider Operation intents commit in one PostgreSQL transaction; external calls
run afterward. Public commands compile, stage and commit through this shared
engine rather than introducing alternate direct-write paths.

Draft corrections retain immutable revisions within admitted effect/target scope.
Missing values, stale versions, invalid occurrence scope and unresolved domain
conflicts block affected work. Optional archives and fine first-reading semantics
do not introduce global gates. Conflict resolution uses the same guarded service.

Use expected versions, stable idempotency identities and exact public references.
Applying prepared work commits its exact immutable proposal, never whichever
draft is latest. Unchanged retries return the original receipt. Corrections
retain request/audit roots and receive new exact proposals. Current permission
and target checks still apply. Handles/IDs grant no authority. Preserve unfinished
historical requests and bounded recovery of exact saved drafts; never delete or
silently translate unknown intent.

## 5. Native context and commands

Overview context supplies configured timezone/current date, actual capabilities,
defaults and short next-tool guidance. Domain packets supply selected targets,
exact references, observed versions/state and allowed actions. General contexts
are durable owner- and purpose-bound snapshots. Existing triage ContextPackets
retain their run/source semantics; normal editing never invents a triage run.
Reuse suitable context without arbitrary TTL or mandatory reread gates.

Distinguish canonical facts, provider observations and reported context. Include
freshness and incomplete coverage. Reading a packet implies neither provider
refresh nor absence from an incompletely observed calendar. Changed state
invalidates affected optimistic preconditions, not unrelated work.

Expose strict flat business tools for routine operations across supported domains.
Clients supply ordinary business fields and one stable `operation_id` for a command
and its retries. Docket derives request/execution identities, support records,
internal action grammar, compiler scope and exact draft bindings. Normal native
discovery contains neither nested generic mutation unions nor the three-field
`work` envelope. There is no fixed tool-count invariant.

Calendar context returns known destination references. Event creation accepts
`context_id`, `operation_id`, `title`, ISO `start`/`end`, `timezone`, `calendar_ref`
and optional `location`/`notes`. Accept offset timestamps and normalize server-side.
Conflicting zone/offset, nonexistent local time and reversed intervals yield
public-field errors; offsets can disambiguate DST folds. Provider UUIDs, raw
calendar IDs, compiler timing kinds, import/support IDs and fold flags are not
ordinary client requirements. Names in notes do not send invitations; additional
provider effects require explicit supported operations.

Updates use context-held versions and field-level provider patch guards. Recurring
work specifies occurrence/series scope. Errors identify the public field, expected
shape and useful next action, rather than only a Python class or union failure.
Correcting input does not require renewed authority.

Optional prepare/read/apply supports requested previews, imports and atomic
multi-object work. Preparation returns bounded preview and immutable opaque
handle; apply uses the handle's durable retry identity. The internal compiler
and isolated historical recovery can remain without public compiler aliases.
Batching composes owned ready previews for distinct targets. Applied members
recover the parent receipt instead of executing again. Conflict decisions can
atomically include an exact business preview; attention dispositions use observed
case versions/revisions. Bounded saved-work reads preserve exact historical JSON
and authentic roots without silently translating or executing retired drafts.

Global instructions briefly explain authority, trust, retries and delivery.
Domain guidance lives in packets. Never inject this SPEC or a giant generated
catalog. Initial engineering budgets: 2 KiB global instructions, 8 KiB routine
context and 4 KiB routine write schema, measured on actual transport. Normal JSON
output caps at 16 KiB; explicit audit caps at 64 KiB with pagination. Default
lists use 25 items, maximum 100. Omit null optionals, internal UUIDs, duplicate
snapshots and unsolicited verbatim history. Never rely on transport truncation
or silently discard required facts.

## 6. Providers, storage and recovery

Google OAuth credentials are protected local material. Calendar/Gmail ingestion
is bounded evidence capture. Adapters project committed intent, never canonical
truth. Report queued, successful, failed and uncertain delivery honestly.
Unknown-after-transmission outcomes reconcile before retry; never blindly resend
or invent success. Disabled production gates pause/fail closed. A canonical
commit does not prove provider delivery.

Use SQLAlchemy models/services and ordered forward Alembic migrations for durable
changes. PostgreSQL is authoritative for locking, constraints, triggers and JSON;
SQLite provides fast behavior checks only. Preserve append-only/immutable rows.
A migration may suspend a trigger only around narrowly scoped reversible work.
Never edit production rows to pass checks or add compatibility domain backfills.

Historical clean-ledger export/reset rules retain their original authority; this
amendment authorizes no destructive reset/restore. Image rollback cannot reverse
a migration. Retain a verified backup, prior image and migration-specific recovery.

## 7. Local validation and release

Local validation is release authority. GitHub hosts source/review; Actions does
not gate merge/deploy or automatically duplicate releases. Preserve history
protections. No manual attestation, acceptance phrase, signing infrastructure or
production ledger approval is required for local execution evidence.

The fast native gate runs a compact justified suite, Ruff and strict mypy.
Extended provider, storage/migration and release groups run for affected changes.
Delete retired gateway scaffolding and duplicate assertions; merely relabeling
all tests or hiding them in giant loops is insufficient. Initial targets are
roughly 100–200 native cases and a one-minute warm gate, measured honestly as
engineering targets. Distinct safety failures matter more than a numerical quota.
Checks never use production `.env` or credentials.

Release validation uses a stable tracked-source snapshot excluding ignored private
provenance/runtime data. Automatic evidence binds source-tree digest, lock/check
policy identity, required groups/outcomes and tested immutable image digest.
Changed source, missing checks or a different image invalidates it. Identical
content after rebase/merge may reuse evidence. Deploy the tested image without
rebuilding it. Documentation-only iteration does not require database rehearsal.

Releases require clean synchronized `main`, production configuration and valid
local evidence. Preserve durable drain, pre-barrier execution completion, idle
checks, pre-migration backup, schema upgrade and postdeploy health/authentication.
Queued operations/reconciliation survive restart and do not block drain; timeout
leaves active work intact.

Explicit development instructions authorize local implementation. Standing release
authority covers related branches/PRs, locally validated merges/deployments,
required ingress and approved-owner configuration. Unrelated provider effects,
sending messages, destructive reset/restore and new authority grants remain
outside that release authorization.

## 8. Acceptance

Verify actual schemas and real MCP transport on disposable PostgreSQL: context
then first-valid-attempt flat event creation; retries without duplicate event or
provider intent; update/cancel and task workflows; unknown provider delivery;
stale/timezone errors; exact prepared proposal; permission/revocation/owner
isolation. Preserve delegated automation and restricted service credentials.
Cutover accounts for 47 effects and unfinished drafts, updates the same registered
plugin/skill and reports schema/instruction/context sizes, removed tests and
measured runtimes. No OAuth reapproval is planned. Local evidence must reject
stale/missing checks and reuse content-identical merge evidence without Actions.

## 9. Architecture decisions

| ADR | Decision | Reason |
| --- | --- | --- |
| 001 | One tracked SPEC plus dated delta/index adoption | Current rules remain readable without replaying scattered amendments. |
| 002 | Source-bound local validation and tested-image releases | Remove duplicate remote waits while retaining release safety. |
| 003 | Owner-bound context and flat business commands | Agents supply intent rather than operate an internal compiler grammar. |
| 004 | Full native delegated write authority and optional archives | Authority comes from verified grants, not presence/capture ceremonies. |
| 005 | Remove Hermes/SearXNG trees and browser artifacts | Plugin and Google integrations own the supported user path. |

## 10. Version history

| Version | Change |
| --- | --- |
| 1.0 | First current SPEC synthesized from signed ontology and Operator amendments; adopts November 4 context/local-CI delta and retirement cleanup. No invented prior version. Implemented and deployed; hosted catalog refresh pending. |
