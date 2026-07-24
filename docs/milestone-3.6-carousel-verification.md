# Milestone 3.6 persistent review-carousel verification

Date: 2026-07-23 (America/Los_Angeles)

This closure replaces the schedule card's action-menu/ephemeral review with one
durable Discord message:

`Summary -> Review pages -> Decision`

Approve and Reject exist only on the final decision view. Navigation updates
stored projection state and requests an idempotent edit of the same message.
Successful navigation intentionally has no ephemeral response.

## Automated evidence

The implementation is recorded in:

* `27e4d0a feat(discord): persist aggregate review carousel`
* `1bc8336 chore(discord): bump carousel bridge version`

Verification passed:

* full suite: 190 tests;
* `ruff check .`;
* strict `mypy`;
* migration `0010` on a fresh SQLite database through
  upgrade -> downgrade to `0009` -> upgrade;
* one-page and five-page persistent traversal;
* same-message and monotonically versioned projection edits;
* retry-after-lost-ack command replay;
* stale/forged navigation rejection;
* pre-carousel schedule approval-token rejection;
* projection-version-bound decision approval;
* persistent failure-page review; and
* pinned Hermes component parsing and non-ephemeral navigation acknowledgement.

The unrelated fixed-date local-control test was clock-frozen after the current
date crossed its synthetic token expiry.

## Deployment evidence

The Docket image was rebuilt from the committed source and recreated with
`DOCKET_EXTERNAL_WRITES_ENABLED=false`. Startup migrated PostgreSQL from `0009`
to `0010`; Docket became healthy. Hermes was restarted and reported:

```text
enabled      user     0.7.0    docket-discord
```

`hermes mcp test docket` connected and discovered the expected 20 tools.

The pre-existing pending schedule projection
`20bfb771-26a9-4238-8736-fa154f0915e9` was refreshed through durable outbox
event `497873d3-55d7-4cc3-9881-e594f7078786`. It delivered in one attempt,
retained Discord message `1529747285901049939`, advanced projection version
2 -> 3, bound its current action revision, and entered `summary` with
`reviewed_through_page=0`. No proposal, approval, thread, message, operation,
or provider event was created by the repair.

## Operator-present gate

On that existing card:

1. confirm it now shows **Begin review** and no Approve/Reject;
2. press **Begin review** and confirm the same message displays all three
   immutable schedule items with **Back to summary** and
   **Continue to decision**;
3. press **Continue to decision** and confirm the same message displays
   Approve, Reject, **Back to review**, and **Snooze until tomorrow**;
4. press **Back to review** once and confirm the persistent item page returns;
5. do not approve or reject this pre-existing proposal.

After this UI-only gate, create a fresh harmless schedule proposal for the
separate Calendar-write gate. External writes must remain disabled until that
fresh proposal and its exact expected provider effects have been reviewed.
