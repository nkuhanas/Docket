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

## Live persistent-button evidence

After the recorded Hermes restart, the configured operator created a fresh
test-only `DKT 998 BUTTON` proposal and pressed its Reject component. Docket's
authenticated callback committed at `2026-07-22T07:36:58Z`. The redacted
database comparison proved:

```text
response actor == approval authorized actor       true
response guild == stored daily-thread guild       true
response channel == stored Discord thread         true
response parent == configured root queue          true
response projection == delivered projection       true
response message == stored projected card         true
unique Discord interaction ID persisted           true
```

The callback returned HTTP 200 with no Hermes interaction exception. In the
same transaction, the approval and action became `rejected`, the queue item
became `completed` with `approval_rejected`, an `approval.rejected` audit event
captured the immutable parameter and preview hashes, and no operation was
created. The refresh outbox event then delivered successfully to the same
message, advanced the projection to version 2, removed the active
`control_projection_id`, and rendered an empty component set. Both initial and
refresh projection events were delivered once.

This closes the Milestone 2.5 gate end to end. Milestone 3 may begin when
selected as the next coding assignment.

## Permission deviation

The application currently has Discord Administrator by explicit operator choice
for high-speed staging. The server is treated as a reconstructible projection,
so least-privilege permission reduction is not a condition of this gate. The
application-level target, token, provenance, and approval checks remain active.
If the deployment model later changes, the narrower permission set can be
revisited as hardening rather than retroactively weakening this capability
result.
