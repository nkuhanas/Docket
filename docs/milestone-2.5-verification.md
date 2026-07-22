# Milestone 2.5 verification

This is the reproducible evidence record for the outbound Discord capability
closure on 2026-07-22. It intentionally redacts Discord snowflakes and service
tokens. Canonical IDs remain in PostgreSQL and Discord.

## Runtime identity and extension points

* Hermes tag: `nousresearch/hermes-agent:v2026.7.20`
* deployed image digest:
  `sha256:f7b35053268f532f98955195c909f15a230470fbcbdacaa9fdecb95707dad04a`
* image OCI revision: `3ef6bbd201263d354fd83ec55b3c306ded2eb72a`
* bundled discord.py: `2.7.1`
* Docket plugin: `docket-discord` `0.3.0`
* Hermes core patch: none

The plugin uses the public `pre_gateway_dispatch` hook for provenance/fallback
commands and the pin-private gateway weak reference, adapter map, gateway loop,
and Discord client for outbound operations and raw interaction callbacks. See
`pinned-integration-contracts.md` before changing any pin.

## Automated evidence

The host suite passes:

```text
81 passed, 1 third-party deprecation warning
Ruff: pass
mypy strict: pass (45 source files)
plugin bytecode compile: pass
```

Coverage includes:

* deterministic compact approval-plus-projection tokens within Discord's
  100-character custom-ID limit;
* two ensures converging on one fake public thread;
* archive replay;
* Discord post success followed by a discarded acknowledgement;
* replacement of the fake Hermes adapter while fake Discord state persists;
* recovery of the same thread and message after that replacement;
* rejection of a valid signed token with a forged message ID;
* acceptance of the exact parent/thread/projection/message binding;
* continued ordinary-message fallback and its actor/guild/root-channel gate;
* migration upgrade/downgrade and internal bearer separation.

## Live private-channel evidence

With `DOCKET_EXTERNAL_CALLS_ENABLED=false`, the rebuilt Docket container applied
migration `0004`, Hermes loaded plugin `0.3.0`, and an existing durable
projection event delivered through the real pinned Discord adapter.

Observed redacted transcript:

```text
ensure current date -> thread T (created once, public)
deliver projection -> message M (one embed, no root-channel card)
ensure active date twice -> T, T
archive T -> archived=true
ensure with no known ID -> T, unarchived=true
archive T again -> archived=true
activate T -> archived=false
replay outbox -> delivered, same T, same M
restart Hermes gateway
replay same outbox -> delivered, same T, same M
```

The deployment accepted the full 10,080-minute auto-archive duration. Docket
used the stable footer marker `docket-projection:<projection-uuid>` with separate
full render and component digests. Replays increased only the outbox attempt
counter; the stored Discord thread and message IDs did not change.

## Remaining operator interaction

The recovered historical card represented an already-consumed fake-Calendar
action, so it correctly rendered without live approval controls. The final
button portion of the live gate requires one fresh pending fake-adapter proposal
and the configured operator pressing its Approve or Reject button after a Hermes
restart. Record the redacted callback fields and post-commit ephemeral response
here when that interaction is complete.

Until that click is recorded, the automated exact-context gate is complete and
the live thread/embed/archive/restart gate is complete, but the live persistent
button callback gate remains open. Milestone 3 must not begin.

## Permission deviation

The application currently has Discord Administrator by explicit operator choice
for the staging spike. Therefore this run demonstrates API capability, not the
specification's least-privilege deployment. The intended narrow set remains
View Channel, Send Messages, Create Public Threads, Send Messages in Threads,
Manage Threads, Read Message History, and Embed Links. Removing Administrator
and re-running this transcript is a production-hardening task.
