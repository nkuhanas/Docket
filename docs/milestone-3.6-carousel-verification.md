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

## Remaining live gate

After deployment, create a fresh harmless schedule proposal with at least one
recurring series and one exception:

1. traverse Summary -> Review -> Decision;
2. press **Back to review**, then **Continue to decision** again;
3. press **Refresh** and confirm the same message returns to Summary under a
   new revision with no Approve/Reject;
4. verify an old pre-refresh navigation/decision control is stale;
5. traverse the replacement revision to Decision.

Keep external writes disabled for this UI/freshness gate. A separately opted-in
Calendar mutation smoke may enable writes only after the replacement preview
and its exact provider effects have been reviewed.
