# Milestone 3.6 persistent review-carousel verification

Date: 2026-07-24 (America/Los_Angeles)

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

An authenticated read of that exact Discord message confirmed one enabled
button labeled **Begin review**, compact custom-ID prefix `dkt:n`, no
Approve/Reject components, and footer bindings for projection version 3 and
projection `20bfb771-26a9-4238-8736-fa154f0915e9`. Docket still had zero
`proposal_review_navigate` commands/audits at that point, so this proves the
deployed summary surface but does not substitute for the operator callback
gate below.

## Operator-present evidence

The configured operator used the existing card before it expired:

1. Summary -> schedule review page 1 committed at
   `2026-07-24T04:28:54Z`, from projection version 3.
2. Review page 1 -> Decision committed at `2026-07-24T04:29:04Z`, from
   projection version 4.

Both commands and `proposal.schedule_review_navigated` audits bind the exact
configured actor, projection, daily thread, message, action revision, source
view/page, and target view/page. The same persistent message was edited; no
ephemeral item response, replacement proposal, approval, operation, or provider
call was created by traversal.

The operator then closed the view without pressing **Back to review**. The
approval expired at `2026-07-24T07:05:49Z`, and daily rollover archived the old
thread and created the next active daily projection. Therefore Summary ->
Review -> Decision is live-proven, while Decision -> Back remains a required
fresh-card UI gate. Expiry and rollover correctly prevent the old control from
being revived.

## Freshness correction discovered during closure

Schedule approvals bind exact `calendar_sync_states.last_success_at`, while the
Calendar worker may advance that generation during a multi-page review. The
expired card exposed that an aggregate proposal had no operator recovery path:
standalone proposals rendered **Refresh**, but schedule Summary/Decision did
not.

The closure adds schedule **Refresh** to Summary and Decision. It performs a
new complete Calendar read, recompiles every immutable item/effect/target/
conflict, creates a replacement revision and approval, recreates reminder plans
per manifest item, and resets the same persistent card to Summary. It does not
relax the exact freshness check. Automated coverage also verifies that Snooze
replacement revisions retain per-item reminder bindings and that tokens from
the pre-refresh revision fail closed.

## Schedule Refresh deployment evidence

The correction is recorded in:

* `b358a58 fix(calendar): refresh aggregate schedule proposals`
* `3237c40 docs(calendar): record schedule refresh recovery`

Verification passed with 191 tests, repository-wide `ruff check`, and strict
`mypy`. The schedule integration test adds a newly synchronized conflicting
event before Refresh and confirms that the replacement preview contains it,
proving full recompilation rather than timestamp-only rebinding. It also proves
old-token rejection, same-message Summary reset, superseded approval and
reminder cancellation, and per-item reminder bindings after Refresh and Snooze.
The pinned plugin test accepts the exact mixed Summary component set.

The committed Docket source was rebuilt and recreated. It became healthy on
migration `0010`, reported a current complete Calendar cache, zero enabled
legacy reminder rules, and `external_writes_enabled=false`. Hermes was restarted
for the mounted skill update, connected to Discord, installed the restart-stable
interaction listener, listed `docket-discord` `0.7.0` as enabled, and discovered
all 20 Docket MCP tools. No live proposal or provider mutation was performed
during deployment verification.

## Completed fresh-card UI gate

The operator created the fresh harmless schedule proposal and completed:

1. Summary -> Review -> Decision;
2. Decision -> Back to review -> Decision;
3. Decision -> Refresh, which superseded revision 1 and reset the same message
   to Summary under revision 2;
4. replacement Summary -> Review -> Decision.

The seven navigation/refresh commands committed from
`2026-07-24T21:49:40Z` through `21:50:22Z`. Every navigation command completed
in 2–3 milliseconds and Refresh's post-sync transaction completed in
8 milliseconds. The final projection is delivered at Decision with one page
reviewed, action revision 2, and a pending approval. External writes remained
disabled and no approval, operation, or provider mutation occurred.

This closes the persistent Summary/Review/Decision, Back, full schedule
Refresh/reset, and replacement-review UI gate. The remaining Calendar mutation
gate is separately opted in.

## Button-latency finding

Although the flow was correct, each durable message edit took 1.764–5.185
seconds after its millisecond transaction, averaging 3.510 seconds. This
matched the inherited five-second projection polling cadence; authentication,
state mutation, and token verification were not the bottleneck.

The correction gives Discord projection delivery a dedicated worker task.
After a trusted approval/local/proposal callback transaction commits, the
request thread sends a best-effort thread-safe wake and the task drains all due
rows immediately. Wakes coalesce, never run before commit, and never make the
request synchronously dependent on Discord. The existing leased five-second
poll remains the lost-wake/restart fallback. Automated coverage proves
cross-thread wake, drain/coalescing, polling fallback, commit ordering, no wake
on rejection, and a harmless wake failure.

## Projection-wake deployment and live evidence

The correction is recorded in:

* `f887295 perf(discord): wake projections after commit`
* `8f34c03 docs(discord): record projection wake contract`

Verification passed with 199 tests, repository-wide `ruff check`, and strict
`mypy`. Docket was rebuilt and recreated on migration `0010`; health reported
the database and worker ready, a current Calendar cache, no enabled legacy
reminder rules, and external writes disabled. Startup logged the dedicated
projection task with the unchanged five-second fallback poll.

The pending revision-2 Decision card survived the restart with no operation.
The operator then exercised Decision -> Back to review -> Decision. The two
navigation transactions completed in 5 and 2 milliseconds; their durable
Discord projection rows delivered in 1.123 and 0.970 seconds respectively,
both on the first attempt. This reduced observed mean button-to-card delivery
from 3.510 seconds to 1.047 seconds. The operator confirmed the interaction was
materially better. The remaining latency is bounded thread verification and
Discord message editing rather than waiting for the database polling cadence.
