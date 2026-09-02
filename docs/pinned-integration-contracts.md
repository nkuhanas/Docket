# Pinned integration contracts

This document records assumptions that are true for the currently pinned
installation but are not guaranteed stable APIs. Revalidate every item before
changing a pin. Passing unit tests against local fakes is not sufficient; use
the candidate container's real classes and one live Discord message.

## Current pin inventory

| Component | Current pin | Reproducibility caveat |
| --- | --- | --- |
| Hermes Agent | tag `v2026.7.20`; deployed and configured image digest `sha256:f7b35053268f532f98955195c909f15a230470fbcbdacaa9fdecb95707dad04a`; OCI revision label `3ef6bbd201263d354fd83ec55b3c306ded2eb72a` | The digest is the runtime pin. `HERMES_SOURCE_COMMIT` mirrors the OCI revision for traceability but still has no enforcement role. |
| discord.py inside Hermes | `2.7.1` | Docket relies on public-thread, embed, view, raw interaction, and archived-history behavior from this bundled version. |
| MCP Python SDK / FastMCP | `mcp==1.28.1`, locked in `uv.lock` | Python package is exact, but transport and schema behavior also depend on the application mount and Hermes adapter. |
| Docket Python base | `python:3.12-slim` | Minor line is pinned, image digest is not; future rebuilds can receive a different base image. The built image carries the exact Git revision in `org.opencontainers.image.revision`. |
| PostgreSQL | `postgres:16.9-bookworm` | Version tag is pinned, image digest is not; persistent-volume semantics survive image replacement. |
| SearXNG | dated tag plus SHA-256 image digest | This is the strongest container pin in the stack; keep both tag and digest when upgrading. |
| Local developer runtime | selected by `uv` on the host | It may differ from Docket's Python 3.12 container and Hermes's internal Python runtime. Container verification remains authoritative. |

Record the deployed image identity before an upgrade:

```bash
sudo docker image inspect nousresearch/hermes-agent:v2026.7.20 \
  --format '{{json .RepoDigests}} {{.Id}}'
sudo docker compose images
```

Do not assume `HERMES_SOURCE_COMMIT` enforces anything at runtime. It is a
traceability marker only.

## Hermes plugin contract

The Docket plugin depends on the user-plugin loader plus
`pre_gateway_dispatch`, `pre_tool_call`, `post_tool_call`, and `post_llm_call`
hooks in Hermes `v2026.7.20`.

Milestone 2.5 also depends on a private outbound seam in that exact image. The
plugin resolves `gateway.run._gateway_runner_ref()`, selects the Discord adapter
from `GatewayRunner.adapters`, schedules work on `GatewayRunner._gateway_loop`,
and uses the adapter's `_client` (`discord.ext.commands.Bot`). Hermes exposes no
documented user-plugin lifecycle or outbound Discord service API in this pin.
There is no Hermes core patch; the read-only user plugin owns a private HTTP
listener and raw `on_interaction` listener instead.

The listener binds to `0.0.0.0:8787` inside the Hermes container but is only
`expose`d on the Compose network. It authenticates `DOCKET_TO_HERMES_TOKEN_FILE`
on every request. Docket's callback uses the independent
`HERMES_TO_DOCKET_TOKEN_FILE`. Neither port nor token is model-visible.

Hermes performs overlapping plugin discovery during this pin's startup. Each
discovery pass imports an isolated plugin module, so module globals alone cannot
prevent a transient second bind. Plugin `0.24.0` starts the private HTTP server
under a background supervisor: an `EADDRINUSE` defers that copy without failing
plugin registration, and it retries if the process that temporarily owned the
port exits. Healthy startup may contain one `startup deferred` line, followed
by one reachable listener and no `Failed to load plugin 'docket-discord'` line.

Pinned outbound assumptions to revalidate:

* `TextChannel.create_thread(..., type=ChannelType.public_thread)` creates the
  explicit standard public thread without a starter message.
* `TextChannel.threads` plus `archived_threads(private=False)` can find the
  exact active or archived daily thread.
* `Thread.edit(archived=False, locked=False)` restores an archived thread.
* `Thread.add_user(discord.Object(id=operator_id))` idempotently joins the
  independently configured operator to an active daily thread. Discord requires
  the bot to have `SEND_MESSAGES_IN_THREADS`; the call returns success when the
  operator is already a member. Docket never accepts a caller-selected member.
* a `View(timeout=None)` sends literal components, while a raw
  `on_interaction` listener continues to receive their custom IDs after a
  gateway restart even though the original View object is gone.
* `Interaction.response.defer(ephemeral=True, thinking=True)` supplies the
  initial response before authenticated approval, local-action, and editable
  proposal callbacks and their follow-up.
* `Interaction.response.defer()` on a component interaction acknowledges
  persistent aggregate-review navigation as a deferred message update.
  Success has no ephemeral follow-up; Docket edits the same bound card through
  its durable projection outbox. Errors may still use an ephemeral response.
* message history and embed footer text are available for stable marker
  recovery after an acknowledgement is lost.
* Discord native timestamp tokens (`<t:UNIX_SECONDS:STYLE>`) survive the
  plugin's markdown/mention escaping inside embed descriptions and field
  values. Docket emits them only from canonical concrete instants; date-only
  all-day events and recurrence definitions retain explicit calendar-date and
  IANA-timezone text so client-local conversion cannot change their semantics.
* due-date daily-thread reminder posts can recover by the stable
  `docket-calendar-reminder:<notification UUID>` footer after verifying the
  configured queue parent and bot-owned thread, without enabling mentions,
  components, arbitrary content, or arbitrary destinations.
* bounded operational lifecycle entries use the separately allowlisted system
  channel and one compact action marker. Later states edit the same bot-owned
  message; raw request/provider payloads and per-item progress never cross this
  seam.
* `pre_tool_call` receives the registered
  `mcp__docket__docket_<tool>` name plus stable task/session, turn, and call
  identifiers before dispatch; `post_tool_call` receives those identities,
  bounded duration, status/error category, and result after dispatch.
  `post_llm_call` fires once after the tool loop for the completed turn.
* `pre_gateway_dispatch` receives the synchronous session store. The plugin
  resolves the authorized chat message to the same session ID Hermes later
  supplies as the tool hook task ID. This is the trusted source-to-trace join.
  Raw tool arguments and results are never retained or forwarded. A compact,
  redacted argument preview of at most 768 UTF-8 bytes is forwarded into the
  operational Discord trace for operator diagnostics; secrets, verbatim text,
  raw bodies, deep nesting, and excess fields/items are replaced or omitted.
  A canonical SHA-256 of the complete received arguments is forwarded
  separately so the authenticated trace can bind to the `call_` created at
  Docket's MCP boundary.
* the plugin sends hook observations to Docket through a bounded background
  queue so trace telemetry does not add one network round trip to each tool's
  critical path. Docket validates monotonicity and projects the one trace
  through its durable outbox; the plugin never posts hook output directly to
  Discord. The trace carries the authenticated gateway turn-start instant, so
  its compact timing projection distinguishes time before the first Docket tool,
  tool execution time, and time outside tools without retaining model prompts or
  tool payloads.

Plugin `0.24.0` retains the phase-one provenance boundary. For every
authenticated operator message on the Docket chat root, Docket queue root, or
a Docket-owned daily thread, `pre_gateway_dispatch` synchronously persists one
verbatim `OperatorUtterance` before rewrite, control handling, model dispatch,
or any mutation-capable tool call. Persistence failure skips the turn. Duplicate
Discord delivery reuses the same `utt_` through the transport request key.

After the tool loop, `post_llm_call` persists the one final assembled assistant
message as `rsp_`; stream chunks are not responses. The Discord adapter's
`on_processing_complete` callback updates delivery state separately, so a
generated response and failed projection remain distinct facts. The plugin's
listener is installed on the adapter instance and must be revalidated whenever
the pinned Hermes gateway lifecycle changes.

Each interactive and triage MCP request creates `call_` after service bearer
authentication but before FastMCP argument validation. Received and normalized
argument hashes, status, timing, bounded result references, and later trusted
Discord trace bindings are retained. Raw arguments, raw results, and the
diagnostic argument preview are not stored in `tool_invocations`; the preview
belongs only to the bounded operational `DiscordMcpTrace` projection.

Before an existing interactive mutation tool executes, the shared MCP
dispatcher resolves its normalized
`discord:{guild}:{channel}:{message}:{intent}` request key to the committed
`:0` `OperatorUtterance`. Actor and source metadata must match that immutable
ledger entry. The dispatcher binds the `utt_` to `call_` before execution and
fails closed with `rejected_authority` when the binding is absent or
inconsistent. Read-only interactive tools and the restricted triage profile do
not acquire mutation authority through this check.

### Gmail sender identity and Preference matching

Gmail ingestion retains the provider `From` value as bounded metadata while
continuing to prohibit raw body persistence. When triage first claims a Gmail
source, Docket parses and normalizes the exact address and materializes an
unbound `email` `IdentityHandle` with the source `src_` as evidence. The display
name remains untrusted provider metadata and never becomes a matching key.

An operator-authored `sender_label` `IdentityHandle` is a compact agent-facing
index for one sender. Its exact addresses live in `sender_identity_emails` as
time-scoped associations to `email` handles. One sender label may have up to 25
projected active addresses; one exact address may belong to only one active
sender label. Historical/retracted associations remain available in audit view.
No Person or Organization registration is implied.

Triage sender matching is deterministic:

```text
observed Gmail From address
  -> exact normalized email idn_
  -> active sender_identity_emails association
  -> sender_label idn_
  -> active structured Preference policy
```

A suppression Preference may target the exact email handle or an associated
sender-label handle. It is executable only when
`policy_json.disposition="suppress"`; a bare label or an empty policy cannot
authorize suppression. `docket_get_triage_case` and exact `src_` history expose
the bounded exact source identity and associated sender handle, while exact
`idn_` history exposes the sender's associated-email table. Exact `pref_`
history exposes the stored policy and scope. These projections never include a
Gmail body.

An MCP request rejected by the outer bearer boundary creates only a structured
`log_` with profile and method, never a `call_`; the authorization header and
request body are not read into the log. Phase-one exact-ref, bounded history,
and ordered conversation inspection live behind the distinct trusted internal
service bearer. They are deliberately not added to either agent MCP profile
before the signed tool-contract migration.

The exact final architecture-signoff sentence is recognized only after its
`utt_` commit and creates a ledger-backed `dec_`. That Decision grants
architecture authority only; its implementation authority remains
`gated_by_ONT-INV-0011`. The phase-one runtime does not change approval,
registry, Calendar-lane, AttentionCase, triage-authority, or provider-operation
semantics.

Manifest-bound amendment sign-offs use the same ledger path. A later amendment
may name the existing ledger-backed architecture Decision as its prerequisite;
the earlier case-resolution amendment retains its one-time bootstrap evidence.
An exact sign-off rejected for an ineligible hash or unmet prerequisite remains
a durable `utt_`, creates no authority Decision, and reaches Hermes with a
trusted bounded error result so the Operator receives an explicit failure
instead of only a gateway reaction. Successful sign-off dispatch likewise
includes the already-created `dec_` so Hermes confirms it without replaying the
mutation.

Plugin `0.24.0` renders timed reminder start/end values as Docket-supplied native
Discord timestamps, puts the event subject under the native `Title` field, and
omits a redundant timezone field. All-day reminders instead render fixed
start/end dates plus the Calendar timezone. Projection embeds may omit their
description when a successful terminal title and `Title` field are sufficient.
Timestamp tokens do not grant mention authority and `AllowedMentions.none()`
remains set.

The current Docket deployment runs one Uvicorn process containing both the
trusted callback routes and the projection worker. A successful component
callback can therefore signal that process's dedicated projection task
immediately after commit. This signal is only a latency optimization: the
leased five-second database poll remains authoritative if it is lost, if the
worker is restarting, or if a future multi-process deployment commits in a
different process. Do not remove the poll. A multi-instance deployment that
requires the same immediate latency must replace the local signal with a
database-backed notification or equivalent cross-process wake, while retaining
the outbox lease and polling fallback.

The hook is invoked before ordinary gateway authorization. Therefore the plugin
must perform its own exact actor/guild/channel check and fail closed for control
commands and daily-thread conversation. On an authorized ordinary Docket-chat
or queue-child message it returns:

```python
{"action": "rewrite", "text": rewritten_text}
```

Hermes then replaces the immutable event with `dataclasses.replace` and sends
the rewritten text to the agent. Slash/session commands are intentionally not
rewritten with Docket source context.

Discord channel admission happens inside the pinned Discord adapter before it
constructs the event passed to this hook. Because `require_mention` is enabled,
the dedicated Docket queue must also be a `free_response_channels` and
`no_thread_channels` entry. It remains in `allowed_channels`; in this pinned
adapter that setting prevents Hermes from creating a second response thread and
does not suppress messages already inside a thread. The plugin keeps the queue
root control-only but admits the exact configured operator in a queue child,
then appends that thread ID and the queue parent to trusted Docket provenance.
Docket accepts writes and MCP traces only when the thread is bound in
`discord_daily_threads`. It drops other queue actors, ordinary system-channel
input, and child threads under Docket chat. `/sethome` and generic `/cron`
commands fail closed on Docket surfaces; the Discord toolset omits generic cron
creation, and tool progress is logged rather than posted to chat.
Background-process notifications are disabled, and the prepared Hermes
environment has no Discord home-channel binding. The configured Docket operator
is also the sole generated `DISCORD_ALLOWED_USERS` entry; Compose repeats that
mapping so Hermes' gateway authorization and the plugin's exact actor gate
cannot drift after recreation.

Because each daily thread begins without Hermes conversation history, the
pinned gateway otherwise attempts to send its generic `/sethome` reminder on
the first Docket turn. Plugin `0.24.0` suppresses only that exact reminder while
an authenticated Docket provenance turn is active in the same channel. It does
not create a home-channel binding, enable generic cron delivery, or suppress
the reminder on ordinary non-Docket Discord surfaces.

The current deployment does not register a native Docket Discord application
command. Persistent Approve/Reject components on the projected card are the
normal operator surface. The plugin retains this ordinary-message syntax only
for operator-runbook break-glass recovery:

```text
docket approve SHORT-CODE
docket reject SHORT-CODE
```

The hook accepts a leading slash for compatibility if Discord delivers it as an
ordinary message, but model guidance must not suggest either typed form. The
model-facing proposal result omits the short code and identifies the daily
thread card as the approval surface. Projection buttons use message components
and the raw interaction listener described above; they do not imply that a
native slash command was registered.

The real current event shape is:

```text
MessageEvent.text
MessageEvent.message_id
MessageEvent.source -> SessionSource

SessionSource.platform   -> Platform enum; use .value, not str(enum)
SessionSource.user_id    -> Discord user snowflake
SessionSource.chat_id    -> effective Discord channel snowflake
SessionSource.guild_id   -> Discord guild snowflake
SessionSource.scope_id   -> canonical alias mirrored with guild_id
SessionSource.message_id -> triggering Discord message snowflake
```

The message ID has appeared on both `MessageEvent` and `SessionSource`; the
plugin accepts either. Never infer a Discord ID from a server/channel name.

The pin-specific implementation points inspected during the Milestone 1 spike
are inside the Hermes container:

```text
/opt/hermes/gateway/run.py
/opt/hermes/gateway/session.py
/opt/hermes/gateway/platforms/base.py
/opt/hermes/plugins/platforms/discord/adapter.py
/opt/hermes/hermes_cli/plugins.py
/opt/hermes/tools/mcp_tool.py
```

These paths are diagnostic anchors, not imported Docket APIs. Their movement or
absence on upgrade is a reason to re-spike, not to blindly patch around it.

### Plugin discovery assumptions

The active container expects:

```text
/opt/data/plugins/docket-discord/plugin.yaml
/opt/data/plugins/docket-discord/__init__.py
plugins.enabled: [docket-discord]
```

`plugin.yaml` declares the hook and required environment. The directory is a
read-only bind mount from `hermes/plugin/docket_discord`. The manual Docket
skill is separately mounted into `/opt/data/skills/docket-manual-intent` so it
appears in ordinary skill discovery.

That separate skill mount is also read-only. If Hermes invokes `skill_manage`
against it, the atomic temporary-file write fails with `EROFS`; this is expected
and does not mean plugin discovery or Docket persistence failed. Repository
edits, followed by a Hermes restart, are the authoritative update path for this
skill.

Use this as the first discovery check:

```bash
sudo docker compose exec -T hermes \
  hermes plugins list --plain --no-bundled
```

An enabled listing proves discovery, not that an actual event satisfied the
plugin's exact context gate. Run it only after the gateway has finished starting;
the pinned CLI imports the plugin, and registration binds port 8787. Running this
probe concurrently with gateway startup can contend for that private listener.

## Active configuration versus templates

There are two Hermes configurations with different roles:

* `.runtime/hermes/config.yaml` is the active, ignored, persistent runtime.
* `hermes/config.example.yaml` is the checked-in template and pin record.

Changing only the template does not change the running agent. Changing only the
active file creates configuration drift that will recur on the next bootstrap.
Intentional tool/config changes should update both.

`scripts/prepare-hermes-home.sh` preserves an existing active `config.yaml` but
rewrites the active `.env`. It is a bootstrap helper, not a general config
synchronizer. This distinction matters after a tool rename: the checked-in
template can be correct while the running allowlist remains stale.

Production deployment therefore runs the isolated triage installer after
Hermes reconnects. The installer rewrites the active `docket-triage` profile
from `hermes/triage-config.example.yaml`, refreshes its skill and launcher, and
must discover exactly four tools before deployment succeeds. A successful root
Hermes restart alone does not prove that the isolated profile is current.

Hermes also has `.runtime/hermes/.env`, while Compose injects Docket integration
values from the project `.env` through the service `environment` block. Compose
environment values override same-name values from `env_file`.

## Container and mount lifecycle assumptions

Docket and Hermes do not consume source/config changes the same way:

* Docket source is copied into a locally built image. Rebuilding is mandatory.
* Hermes plugin and skill files are bind-mounted, but hook/module registration
  and active config are process-scoped. Restart Hermes after edits.
* A Docker restart does not update container environment variables. Recreate
  services after root `.env` changes.
* Changing `DOCKET_CREDENTIALS_DIR` changes the host side of a mount and also
  requires recreation.
* PostgreSQL consumes `POSTGRES_PASSWORD` only during first initialization of a
  volume; later environment changes do not rotate the database role.

These distinctions caused real false diagnoses during the initial live spike.
Always name the changed layer before choosing rebuild, restart, or recreate.

The production credential helper sets Docket's UID/GID to the invoking host
identity so mode-`0600` files remain readable. Hermes has its own UID/GID pin.
This currently assumes compatible numeric ownership for the files Hermes must
read; moving the stack to a host with a different user mapping requires an
ownership or narrowly scoped ACL plan, not broader file modes.

The Google credential mount is read-only. The Calendar adapter loads the
persisted refresh token and refreshes short-lived access tokens in memory; it
does not rewrite the mounted file. A future flow that relies on refresh-token
rotation or credential replacement must add an explicit host-side persistence
handoff rather than making the mount writable.

The private SearXNG URL is resolved through the Compose service name. It works
only from a container on the Docket network; no host port is intentionally
published. The network is not marked Docker-internal because SearXNG needs
outbound web access.

## MCP transport and schema contract

Docket mounts a stateless FastMCP server at `/mcp/`. With the current SDK and
application mount, `/mcp` redirects to `/mcp/` with HTTP 307. Some MCP client
paths do not follow that redirect during protocol setup, so use the trailing
slash everywhere.

FastMCP publishes each tool as separate `description` and `inputSchema` fields.
The schema is generated at runtime from Python type annotations and Pydantic
models. Hermes `v2026.7.20` converts these into model-visible function
definitions in `/opt/hermes/tools/mcp_tool.py` and normalizes nullable unions,
object types, and local JSON Schema references.

Consequences:

* The skill should explain when and why to use a tool, not duplicate its field
  schema.
* The Python signature and Pydantic models are the schema source of truth.
* `hermes mcp test` does not display the full schema.
* A generated-schema regression test is required after signature changes.
* A provider/Hermes upgrade can alter schema normalization even when Docket's
  generated schema is unchanged.
* Existing Discord sessions cache the discovered tool surface. After changing
  tools, schemas, or the allowlist, `/reload-mcp` is required in the active
  session even when `hermes mcp test docket` already reports the new server
  contract.

### Scoped ChangeSet schema disclosure

The clean `docket_commit_changeset` input is a fully discriminated union across
all canonical mutation variants. Its complete normalized schema is too large to
place in every model turn or return through Hermes' ordinary inline
`tool_describe` budget. The pinned Docket Discord plugin therefore extends only
the progressive `tool_describe` bridge with:

```text
mutation_types[]
```

For `docket_commit_changeset`, this field is mandatory. The plugin derives a
reference-closed schema containing the common turn envelope and only the exact
requested mutation variants, and binds the result to both the complete and
scoped schema SHA-256 values. It never hand-maintains a second mutation schema.
Other tools retain upstream Hermes describe behavior.

Before a constructed ChangeSet crosses the MCP transport, the plugin validates
it against the complete normalized schema in Hermes' live registry. A malformed
construction is blocked locally and does not create a Docket `call_` or affect
MCP server connectivity health. Docket repeats Pydantic validation at its
authenticated boundary; such a rejection is returned as `ok=false` over a
completed MCP request/response, not as an MCP transport error. This distinction
prevents schema mistakes from opening Hermes' server circuit breaker.

The adapter lives at:

```text
hermes/plugin/docket_discord/schema_disclosure.py
```

It is deliberately pin-specific. A Hermes upgrade must re-run the scoped-schema,
bridge-shape, invalid-argument, and Compose smoke fixtures before changing the
image digest.

The model-facing persistence tool is intentionally named `docket_store_record`,
not `docket_create_record`. Its operation stores a source-backed assertion:
create when absent, or match materially equal canonical data and attach current
provenance. A canonical identity with different data returns `record_conflict`
without attaching provenance; replacement remains an explicit update. The
earlier create-oriented name caused the model to use read tools when a canonical
record already existed, while the retired `docket_remember_record` name blurred
natural-language intent with the tool's persistence responsibility.

Calendar proposals are also generated from strict Pydantic input. The model
supplies an exact course record/version or standalone event specification,
account UUID, one stable configured Calendar lane, and the opaque Calendar ID
bound to that lane; Docket derives stable logical item identities,
risk, executable effects, hashes, preview, target versions, approval references,
and operation idempotency. The short code remains durable for break-glass
operations but is removed from the model-facing MCP result, which instead
supplies button-card guidance. No model-visible tool records approval or
directly calls Google.

The lane registry is a Docket-owned control plane, not a free-form event tag.
`academic`, `work`, `organizations`, `personal`, and `unsorted` are seeded
defaults; explicitly authorized custom lowercase slugs extend the vocabulary.
The preexisting configured Calendar is bound to non-deletable `unsorted`
without moving historical events. Provider creation is correlated by a stable
Docket lane marker, while rename/color changes reuse the same Calendar ID.
Event migration and empty-lane deletion are separate explicit-operator tools.
They queue execution directly and never generate authorization proposals;
ambiguous targets are clarified before the call. Neither operation is implied by
presentation changes. Event routing precedence is explicit operator direction,
entity default, bounded inference, then `unsorted`; a mismatch between the
classified lane and target ID fails closed.

Calendar lookups and control do not expose a provider client. Bounded cache
lookup, redacted sync status, profile reads/writes, canonical reminder-rule
listing, standalone proposals, per-course reconciliation, and restore all use
generated typed schemas. Reminder mutations exist only inside an approved
Calendar proposal; there is no model-visible direct rule-write/disable tool.
Existing-event mutations distinguish one occurrence or non-recurring event
(`target_scope=event`) from a whole Docket-owned recurring series
(`target_scope=series`). Series scope accepts only the master
`recurring_event_id` returned by the bounded lookup; occurrence/master
substitution fails closed. The master identity and ETag live on the exact
Docket `calendar_links` row rather than an expanded occurrence cache row.
Cancellation and reminder-only approvals require a current, non-stale complete
cache plus the exact bound event/master ETag, but do not require the cache's
`last_success_at` to remain byte-for-byte unchanged. A later harmless complete
refresh therefore cannot invalidate an earlier independent card. Create and
event-content update approvals retain the exact target and conflict
dependencies used by their preview. Per-course reconciliation binds its record
version, linked provider identities/ETags, effects, and actual overlapping
conflict set.
The rule list supplies current canonical identities for diagnosis after session
compaction, avoiding a past-session search. Reminder destinations are fixed:
Docket binds Google popup plus the due-date queue thread internally.
For local-day Calendar questions, that same lookup accepts only the closed
`today`/`tomorrow` vocabulary. Docket samples its request clock once, resolves
both local midnights in `DOCKET_TIMEZONE`, and returns the resolved date,
timezone, and `as_of` instant. This is deliberately part of the existing read
tool rather than a generic model-visible clock: Hermes must not invoke a terminal
to manufacture lookup bounds or convert result timestamps. Each timed event
retains its UTC `start_at`/`end_at` pair and adds `start_local`/`end_local` plus
the configured `local_timezone`, including the correct offset across DST.
Explicit timezone-aware start/end pairs and the no-range rolling seven-day
default remain separate modes; mixed relative and explicit ranges are rejected.
A direct current/today/tomorrow list or find uses `require_fresh`: the normal
five-minute synchronization interval can leave a healthy, covered cache behind
a provider event created seconds earlier. `prefer_cache` remains correct only
when that bounded lag is acceptable.
The active and template allowlists are synchronized by
`scripts/prepare-hermes-home.sh`, but an existing Hermes session still requires
`/reload-mcp` after deployment. The MCP server registry and Hermes allowlist
must contain the same complete public tool registry. The Discord and server
MCP-trace allowlists must match that registry as well, so adding a tool cannot
silently turn its execution into a black box.

`docket_apply_course_intent` accepts one active course UUID/version,
`sync|drop`, configured account/active academic lane, optional unified reminder
plan, and trusted request context. `drop` additionally requires a reason. The generated
schema must keep these fields explicit. A fully synchronized course returns a
no-op; an approved proposal compiles one parent operation and independent
durable items. Drop archives only after every active link is confirmed
cancelled. `docket_restore_record` is a separate optimistic local transition;
it never contacts Google.

The Discord plugin understands editable proposal-control token fields by
compact numeric codes shared with Docket. Adding a token field requires
changing both maps. Codes currently cover priority, reminder preset, refresh,
edit, and snooze. Bounded batch review uses a separate compact navigation
token bound to revision, projection, projection version, source/target
view/page, actor, and expiry. Its final approval uses a separate decision token
bound to the delivered projection version. A server-only code or token version
produces a valid-looking card that the pinned plugin rejects, so the
adversarial plugin contract and one live persistent-navigation/decision smoke
are mandatory after changes.

Healthy batch Summary contains one `review_navigation` **Begin review**
button. Healthy Decision combines two approval buttons, one
`review_navigation` **Back to review**, and a blue **Snooze until tomorrow**
button on the same primary row as **Approve** and **Reject**. Review
pages contain navigation only. After an approval fails Docket's target-version
check, the same card contains a reject-only approval control plus one
`proposal_action` **Rebuild preview** button. The renderer accepts that exact
mixed set even though ordinary approval cards use an Approve/Reject pair.
Rebuild verifies the immutable course or legacy schedule binding, recompiles
every item against the newly complete Calendar generation, creates a replacement
revision/approval and per-item reminder plans, and resets the same projection
to Summary. Restart Hermes after changing this component contract, then
exercise the stale-card renderer and a real callback.

Approval refreshes are bound to the exact delivered projection that accepted
the interaction. Later operation refreshes without an explicit target select
the newest existing projection, never the queue item's original received date.
Once Docket accepts a decision the plugin immediately removes that message's
controls while the durable canonical renderer converges. A duplicate click on
the still-visible card is an idempotent repair request: it returns the already
recorded decision and targets the same projection instead of attempting a
second operation.

Standalone Priority and Reminder selects apply immediately by creating a new
immutable proposal revision. **Edit details** is a separate bounded modal for
title, location, operator tags, and custom reminder leads; it is not an apply
button for either select.

## Google Calendar REST contract

The current adapter uses Calendar API v3 REST endpoints rather than a generated
client. Calendar IDs and event IDs are percent-encoded as opaque path segments.
Create uses `events.insert`; modify-in-place uses `events.patch` with the stored
ETag when available and `sendUpdates=none`.

Lane provisioning uses `calendars.insert` or `calendars.patch` plus
`calendarList.patch` with `colorRgbFormat=true`. The token therefore needs
`calendar.events`, `calendar.calendars`, and `calendar.calendarlist`. Only the
bounded lane mutation tool can reach those administration methods.

Every write stores `docket_correlation=<operation UUID>` in
`extendedProperties.private`. Reconciliation calls `events.list` with the
`privateExtendedProperty` constraint, `singleEvents=false`, and a bounded result
count. This behavior follows Google's documented
[private extended-property search contract](https://developers.google.com/workspace/calendar/api/guides/extended-properties).
Changing Calendar API behavior, OAuth libraries, recurrence
serialization, or HTTP transport requires rerunning the zero/one/multiple-match
and unknown-outcome tests before a live write.

The normalized link snapshot deliberately retains only summary, location,
start/end, recurrence, and the Docket correlation. Google response fields such
as creator email and HTML link never enter the link, operation result, audit, or
Discord projection.

The Calendar read adapter uses `events.list` with explicit `timeMin`, `timeMax`,
`singleEvents=true`, `showDeleted=true`, bounded page/event counts, and full
pagination. Its Google partial-response selector requests only page tokens,
calendar timezone, and the event identity/status/summary/location/time/recurrence/
ETag/update fields admitted by the cache; descriptions, attendees, conferencing,
organizers, attachments, and arbitrary extended properties are not requested.
Because that expanded read does not return recurring masters, each complete
generation also exact-reads every active Docket-linked recurring master. The
master snapshot and ETag promote in the same transaction as its occurrences.
This second walk is capped by
`DOCKET_CALENDAR_LINKED_SERIES_MAX_READS` (250 by default); exceeding it fails
the generation closed instead of issuing an unbounded request fan-out.
The pre-read link ETag and provider correlation form the compare guard, so a
concurrent approved mutation wins over an older synchronization result. A
missing/cancelled master closes the link and disables its canonical reminder
rules; a provider error fails the whole generation and retains the prior one.
It never combines rolling-window bounds with a provider sync token.
Per-course approvals bind their record version, linked provider identities and
ETags, and the conflicts that overlap that course. They require a current
complete cache but do not bind equality of its global refresh timestamp, so an
unrelated course proposal or write cannot make every sibling card stale.
Only a complete in-memory page walk enters the database promotion transaction;
any timeout, malformed page, repeated identity/token, authorization failure, or
bound exhaustion leaves the prior generation intact and reports it stale.

`DOCKET_CALENDAR_READS_ENABLED` selects this real read adapter independently of
`DOCKET_EXTERNAL_WRITES_ENABLED`, which selects the mutation/reconciliation
adapter. Both default false. This split is a least-privilege boundary: enabling
read synchronization cannot cause an approved or pending write operation to be
sent to Google.

The worker commits an attempt with no provider marker, then commits a
`call-started:<lease UUID>` marker immediately before network I/O. On lease
recovery, no marker permits the same operation to return to pending; a marker
requires reconciliation. A crash between writing the marker and the HTTP call
therefore takes the conservative reconciliation path and may cost a read, but
cannot justify a blind duplicate write.

## Source provenance boundary

The plugin appends this structured relationship:

```text
source_type = discord_message
source_object_id = metadata.message_id
actor_id = metadata.user_id
request_key = discord:{guild_id}:{channel_id}:{message_id}:{intent_index}
```

For chat-root messages, `metadata.parent_channel_id` is omitted so historical
command hashes remain stable. For daily-thread messages, `channel_id` is the
actual thread and `parent_channel_id` is the configured queue.

Docket validates:

* 17–20 digit Discord snowflake shapes;
* equality among source object ID, metadata message ID, actor ID, and request
  key components;
* exact operator user and guild, plus either the configured Docket-chat root or
  an active/archived Docket daily thread bound to the configured queue;
* the record-type-specific identity and data schema.

This is defense in depth over the Hermes bearer token, but it is not
cryptographic proof that a Discord message exists. A party that steals the
Hermes bearer and knows the configured snowflakes could fabricate a syntactically
consistent message ID. Current containment assumes the bearer is readable only
inside the intended containers and credential mount.

If this trust assumption becomes unacceptable, the next hardening step is a
short-lived or single-use signed provenance envelope created by the trusted
plugin and verified by Docket, or a private plugin-to-Docket assertion endpoint
that keeps source construction outside the model-visible tool call. Do not
misdescribe the current field validation as a signature.

The appended context is visible to the model and stored in the Hermes session
transcript. Use redacted, scoped exports and treat them as operationally
sensitive even though secrets should not be present.

## Record matching and idempotency semantics

`docket_store_record` has two related but distinct deduplication paths:

* Same request key and same complete payload: return the prior result as
  `replayed_request` without new durable rows.
* New Discord request key resolving to the same canonical identity and
  materially equal normalized data: return `matched_existing` and attach a new
  source/audit event without incrementing the record version.

A new request with the same canonical identity but different normalized data
returns `record_conflict` and attaches no provenance. Historical command rows
with the retired `docket_remember_record` operation remain immutable evidence,
but they are not replay-compatible with `docket_store_record`. Reusing one of
their request keys through the current tool returns `idempotency_conflict`.

After `record_conflict`, fetching the canonical data and resubmitting it under a
new request key is forbidden. It would make a current source appear to support
fields learned only from Docket. A replacement requires the explicit update
path and operator intent.

Reusing a request key with any different hashed input is an idempotency
conflict. For that reason, live replay tests should reuse captured tool
arguments exactly instead of manually rebuilding them.

Historical malformed provenance is retained as evidence. Normalization may
repair the canonical record and add an audit event, but must not forge a Discord
source retroactively.

## Upgrade checklist

Before changing Hermes, MCP, Python, PostgreSQL, or SearXNG pins:

1. Record current image IDs/digests, package lock, and the source commit marker.
2. Read the candidate release notes and inspect the candidate container rather
   than assuming file/class compatibility.
3. Confirm plugin manifest discovery and `pre_gateway_dispatch` registration.
4. Recheck the real `MessageEvent` and `SessionSource` field types, especially
   platform enum handling and Discord guild/channel/message placement.
5. Recheck that the hook still runs before authorization and that rewrite/skip
   return values retain their meanings.
6. Confirm the active Discord adapter stamps user, guild/scope, channel, and
   message IDs on ordinary messages.
7. Confirm `/mcp/` transport initialization with the pinned client.
8. Inspect the full Docket tool schema before and after Hermes normalization.
9. Run all unit, integration, and adversarial tests.
10. Run a disposable fake-credential Compose smoke; never point the smoke
    script at production credentials.
11. Run one real operator Discord remember flow and verify command, source,
    audit, retrieval, and exact replay.
12. Verify unauthenticated 401 and authenticated forged-source rejection.
13. Only then update the documented pin and deployed production stack.

Do not perform a broad upgrade and debug all contracts simultaneously. Change
one pin class at a time and retain the prior image so rollback does not depend
on a mutable remote tag.
