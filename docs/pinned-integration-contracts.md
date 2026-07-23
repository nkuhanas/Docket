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
| Docket Python base | `python:3.12-slim` | Minor line is pinned, image digest is not; future rebuilds can receive a different base image. |
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

The Docket plugin depends on the user-plugin loader and the
`pre_gateway_dispatch` hook in Hermes `v2026.7.20`.

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
prevent a transient second bind. Plugin `0.6.0` starts the private HTTP server
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
* a `View(timeout=None)` sends literal components, while a raw
  `on_interaction` listener continues to receive their custom IDs after a
  gateway restart even though the original View object is gone.
* `Interaction.response.defer(ephemeral=True, thinking=True)` supplies the
  initial response before the authenticated Docket callback and follow-up.
* message history and embed footer text are available for stable marker
  recovery after an acknowledgement is lost.
* due-date daily-thread reminder posts can recover by the stable
  `docket-calendar-reminder:<notification UUID>` footer after verifying the
  configured queue parent and bot-owned thread, without enabling mentions,
  components, arbitrary content, or arbitrary destinations.

The hook is invoked before ordinary gateway authorization. Therefore the plugin
must perform its own exact actor/guild/channel check and fail closed for control
commands. On an authorized ordinary Docket-chat message it returns:

```python
{"action": "rewrite", "text": rewritten_text}
```

Hermes then replaces the immutable event with `dataclasses.replace` and sends
the rewritten text to the agent. Slash/session commands are intentionally not
rewritten with Docket source context.

Discord channel admission happens inside the pinned Discord adapter before it
constructs the event passed to this hook. Because `require_mention` is enabled,
the dedicated Docket queue must also be a `free_response_channels` and
`no_thread_channels` entry. It remains in `allowed_channels`. The plugin treats
the root and every child daily thread as control-only and skips every message
that is not an exact root approval or rejection command, so queue conversation
cannot reach the model. It also drops ordinary system-channel input and child
threads under Docket chat. `/sethome` and generic `/cron` commands fail closed
on Docket surfaces; the Discord toolset omits generic cron creation, and tool
progress is logged rather than posted to chat. Background-process notifications
are disabled, and the prepared Hermes environment has no Discord home-channel
binding. The configured Docket operator is also the sole generated
`DISCORD_ALLOWED_USERS` entry; Compose repeats that mapping so Hermes' gateway
authorization and the plugin's exact actor gate cannot drift after recreation.

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

The model-facing persistence tool is intentionally named `docket_store_record`,
not `docket_create_record`. Its operation stores a source-backed assertion:
create when absent, or match materially equal canonical data and attach current
provenance. A canonical identity with different data returns `record_conflict`
without attaching provenance; replacement remains an explicit update. The
earlier create-oriented name caused the model to use read tools when a canonical
record already existed, while the retired `docket_remember_record` name blurred
natural-language intent with the tool's persistence responsibility.

Calendar proposals are also generated from strict Pydantic input. The model
supplies a stable meeting ID, exact record version, account UUID, and calendar
ID; Docket derives risk, executable schedule, hashes, preview, target versions,
approval references, and operation idempotency. The short code remains durable
for break-glass operations but is removed from the model-facing MCP result,
which instead supplies button-card guidance. No model-visible tool records
approval or directly calls Google.

Calendar lookups add five model-visible tools without exposing a provider
client: bounded cache lookup, redacted sync status, bounded canonical reminder-rule
listing, explicit reminder-rule set, and explicit reminder-rule disable. The list
tool supplies rule UUIDs and current versions after session compaction, avoiding a
past-session search. Their generated schemas cap lookup windows, result counts,
filters, lead times, source context, and optimistic rule versions. Reminder
destinations are absent from the model schema; Docket binds the queue parent and
the due-date daily thread internally.
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
`/reload-mcp` after deployment.

## Google Calendar REST contract

The current adapter uses Calendar API v3 REST endpoints rather than a generated
client. Calendar IDs and event IDs are percent-encoded as opaque path segments.
Create uses `events.insert`; modify-in-place uses `events.patch` with the stored
ETag when available and `sendUpdates=none`.

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
It never combines rolling-window bounds with a provider sync token.
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

Docket validates:

* 17–20 digit Discord snowflake shapes;
* equality among source object ID, metadata message ID, actor ID, and request
  key components;
* exact operator user, guild, and Docket-chat channel against Docket's own
  settings;
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
retain the operation name `docket_remember_record`; the store service accepts
that name only as a replay-compatible predecessor and writes
`docket_store_record` for new commands.

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
