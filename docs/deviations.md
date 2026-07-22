# Specification deviations

## 2026-07-22 — Retain Discord Administrator during the staging spike

The Milestone 2.5 specification calls for proving the exact channel permission
set without granting Administrator. The operator explicitly accepted a
high-speed staging shortcut: the existing Hermes/Yuuka application currently
retains server-wide Administrator while the outbound thread, embed, and button
capability is closed.

This does not weaken Docket's application checks. The plugin still allowlists
one guild and queue, accepts only a private service token, derives thread names
and components, forbids arbitrary target channels, and Docket validates the
actual actor, guild, parent, thread, projection, and message before consuming an
approval. It does mean the live spike does **not** prove least-privilege Discord
deployment. Remove Administrator and re-run the capability suite with View
Channel, Send Messages, Create Public Threads, Send Messages in Threads, Manage
Threads, Read Message History, and Embed Links before calling the deployment
production-hardened.

## 2026-07-22 — Use a pinned private Hermes runtime seam for outbound Discord

Hermes `v2026.7.20` exposes `pre_gateway_dispatch` to user plugins but does not
publish a lifecycle hook or public outbound Discord plugin API. The Docket
plugin therefore uses the pinned runtime's module-level gateway weak reference,
`GatewayRunner.adapters`, `_gateway_loop`, and the Discord adapter's `_client`.
It starts a private token-authenticated listener on the Compose network and
schedules bounded Discord operations onto the gateway loop.

No Hermes core file is patched and no second bot identity is introduced. This
is nevertheless a private compatibility seam: any Hermes upgrade must re-run
thread create/find/archive, post/edit, restart recovery, and persistent-button
tests before deployment. Failure to locate the weak reference, live adapter,
event loop, or client fails closed with `discord_runtime_unavailable`.

## 2026-07-21 — Preauthorize the Google Workspace bundle

Operator direction expands initial Google OAuth authorization beyond the
private specification's Calendar-then-Gmail milestone sequence. The default
setup now requests these four scopes in one consent flow:

```text
https://www.googleapis.com/auth/calendar.events
https://www.googleapis.com/auth/documents
https://www.googleapis.com/auth/gmail.modify
https://www.googleapis.com/auth/spreadsheets
```

This is an authorization expansion, not a tool-surface expansion. The token
remains mounted only into Docket. Hermes does not receive it; no Google MCP or
raw provider method becomes model-visible; Sheets and Docs have no Docket
adapter yet; and Gmail send/reply remains disabled. Future provider operations
still require an action-registry entry and the risk/approval policy specified by
the private implementation specification.

The accepted tradeoff is a higher-impact refresh credential now in exchange for
one consent flow and avoiding future reauthorization. Compensating controls are
the ignored credential directory, mode-0600 atomic persistence, read-only
container mount, external calls disabled by default, and narrow Docket-owned
adapters when those features are implemented.

## 2026-07-22 — Persist the last synchronized Calendar snapshot

`calendar_links` includes a `synced_snapshot` JSON value in addition to the
private specification's listed columns. The specified link fields identify the
provider object and record version but cannot reconstruct the exact prior
schedule after the canonical course record changes. Without a snapshot, an
update preview cannot honestly show before/after values and reconciliation
cannot compare the provider result with the last confirmed representation.

The snapshot is a bounded, normalized subset: summary, location, start/end,
recurrence, and Docket correlation. It excludes attendee data, creator email,
HTML links, descriptions, credentials, and arbitrary provider response fields.
It is updated only in the same transaction that confirms operation success.

## 2026-07-21 — Keep an ordinary Discord approval fallback

The private specification's `/docket approve <short-code>` fallback assumes a
registered Discord application command or a gateway path that admits arbitrary
slash-like channel messages. The deployed server has no Docket Discord
application registration, and Hermes `v2026.7.20` applies channel mention
admission before the Docket plugin hook.

The operational syntax is therefore the plain queue message
`docket approve <short-code>` or `docket reject <short-code>`. The queue is an
allowed, free-response, no-thread channel so the adapter delivers that message.
The trusted plugin then drops every non-command queue message, verifies the
exact operator/guild/channel tuple, and calls Docket's authenticated internal
approval endpoint. Approval remains outside the model-visible MCP surface.

The plugin still parses a leading slash when delivered for forward
compatibility. Milestone 2.5 adds persistent message buttons through the pinned
Discord client listener; it does not register a `/docket` application command.
The ordinary message remains the non-model recovery path if a card or component
cannot be used.
