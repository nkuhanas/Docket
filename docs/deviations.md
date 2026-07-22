# Specification deviations

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

## 2026-07-21 — Use an ordinary Discord approval message

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
compatibility. Native slash commands or buttons remain deferred until Docket
has an explicitly registered Discord application interaction surface.
