# Milestone 3.7 verification

Date: 2026-07-24 (America/Los_Angeles)

Milestone 3.7 replaces the normal aggregate-schedule workflow with independent
course lifecycles. The authoritative private specification is
`specs/spec-2.3.md` and remains outside this repository.

## Delivered contract

* A term supplies shared bounds and timezone; it does not own a schedule list.
* Each course/section is one canonical record with stable child meeting IDs.
* `docket_apply_course_intent` derives create, update, cancel, and
  no-op items for one course.
* `drop` persists independently retryable cancellation progress and archives
  only after every active link is confirmed cancelled.
* `docket_restore_record` reactivates the same canonical identity; the next
  approved sync creates fresh provider event IDs for its current meetings.
* Re-importing unchanged data, repeating an equal explicit replacement, and
  reconciling an unchanged course preserve the version and perform no provider
  work. Omitting a course has no effect.
* Explicit meeting date bounds and timezone override the shared term; only
  omitted values inherit term defaults during compilation.
* A bounded course proposal renders its immutable items on the initial card and
  opens directly at the decision controls; overflow retains reviewed paging.
* The configured operator can converse with Hermes inside a Docket-owned daily
  thread; trusted writes and redacted MCP traces bind the actual stored thread,
  while the queue root, foreign threads, other actors, and system channel remain
  denied.
* Standalone timed and all-day events materialize an omitted timezone from
  `DOCKET_TIMEZONE`; an explicit IANA timezone retains precedence.
* The obsolete atomic whole-term store/proposal path is absent from both Docket
  and Hermes; bulk input is agent-side orchestration over independent courses.

Alpha cleanup migration `0012` terminalizes any unresolved obsolete proposal,
cancels its unactivated reminder plans, and drops
`calendar_schedule_snapshots`. Ordinary action, queue, outbox, and audit rows
remain as inert operational history.

Follow-up alpha cleanup migration `0013` removes the disabled pre-unified
reminder-rule rows and their obsolete scheduled notifications, then drops the
source-kind compatibility discriminator. Current approved unified reminder
rules and Calendar links are unchanged; audit rows remain inert history.

## Automated evidence

The integration lifecycle uses the stateful fake Calendar adapter and proves:

1. add two meetings;
2. update one stable series, cancel one, and create one;
3. an equal update that preserves the record version, followed by unchanged
   reconciliation without a card;
4. permanent failure after one drop cancellation;
5. active course state during partial failure;
6. retry of only the remaining cancellation followed by archival;
7. explicit restore and fresh provider identities; and
8. unchanged re-import plus final Calendar no-op.

It also verifies the course card summary, Discord-native term timestamps,
pending-proposal deduplication, and rejection of a conflicting concurrent
lifecycle action.

Validation completed before deployment:

```text
ruff check .  -> passed
mypy          -> passed (64 source files)
pytest -q     -> 197 passed, 1 dependency deprecation warning
skill check   -> Skill is valid
```

The warning is the existing FastAPI/Starlette `httpx` compatibility
deprecation; it is unrelated to this milestone.

## MCP and Hermes contract

The generated Docket surface and active Hermes allowlist contain the same 19
tools. The Milestone 3.7 additions are:

* `docket_restore_record`
* `docket_apply_course_intent`

Hermes plugin `0.15.6` recognizes both in redacted MCP traces. The
`docket-manual-intent` skill now treats bulk input as resumable per-course
orchestration, allocates a distinct intent index to every write/proposal,
requires explicit drop, recognizes materially equal updates before writing,
and follows restore with reconciliation. The skill passed the Skill Creator
validator.

Existing sessions still cache MCP discovery. Run `/reload-mcp` after the
deployment before the live gate.

## Operator-present gate

The operator-present gate uses disposable course `DKT 932 · LIFECYCLE` and
stops after each decision card.

Completed on 2026-07-24:

* **Add:** stored version 1 and approved one `calendar_create_event` item for
  stable meeting `lecture`. Google returned success, the recurring link became
  confirmed, and the normal ten-minute unified reminder plan was bound.
* **Change:** stored version 2, approved one in-place lecture update plus one
  new `lab` series, and observed both items succeed on their first attempts.
  The lecture retained its provider identity; the lab received a distinct
  provider identity.
* **Accidental unchanged repeat:** repeating the already-current change exposed
  a gap: `docket_update_record` advanced version 2 to 3 and caused two needless
  provider patches. No destructive change occurred. The service and Hermes
  contract were corrected so an equal full replacement now returns
  `matched_existing`, preserves the version, and leaves reconciliation at
  `no_op`.
* **Post-restore unchanged repeat:** the record-side correction held version 5
  and Hermes issued no update, but reconciliation proposed two updates because
  Google had reordered the semantically equal RRULE properties in its create
  responses. The false-positive card was rejected without creating an
  operation. Reconciliation now canonicalizes recurrence property, multi-value,
  and line order before comparison; the integration regression uses Google's
  observed ordering.
* **Drop:** approved exactly two independent cancellation items. Both Google
  deletions succeeded on their first attempts, and Docket archived the course
  only after the second cancellation completed.
* **Restore:** reactivated the same canonical course identity at version 5 and
  approved exactly two creates. Google returned fresh provider event IDs for
  `lecture` and `lab`; both links were confirmed with the normal ten-minute
  reminder plan.
* **Final unchanged repeat:** Hermes called search, account/profile resolution,
  current-record read, and course reconciliation without storing or updating
  the record. Reconciliation refreshed the current Google state with `GET`
  requests, returned `no_op`, created no action or operation, and performed no
  provider `POST`, `PATCH`, or `DELETE`. The course remained active at version
  5 with both provider identities unchanged.

During the gate, confirm:

* [x] each proposal appears in the current ISO queue thread;
* [x] course review pages show only operationally relevant fields;
* [x] `docket-system` receives redacted MCP traces and rich lifecycle entries;
* [x] partial or uncertain cancellation never archives the course;
* [x] the completed drop archives exactly once;
* [x] restore retains the course record identity and creates fresh Google
  series;
* [x] an unchanged final re-import/reconciliation produces no duplicate
  series.

The Milestone 3.7 operator-present gate is complete. Gmail ingestion remains
deferred behind the calendar-control milestones.

## Post-gate live import correction

The first real eight-course import exposed an approval-freshness coupling that
the single-course gate could not reveal. Each per-course proposal performed a
fresh complete Calendar read, advancing the global `last_success_at`; the first
course card therefore reported stale before any provider write, and approving
one rebuilt card would invalidate its independent siblings.

Course approvals now require a current complete cache but bind only the
course's record version, linked provider identities/ETags, effects, and actual
overlapping conflict set. A newer equivalent refresh or an unrelated course
proposal/write no longer invalidates the card. A changed provider target or a
new overlapping conflict still fails closed and requires review of a rebuilt
preview. Integration coverage proves both sides of that boundary.

## Post-gate standalone recurring lifecycle

Completed on 2026-07-25 against the configured Docket Calendar:

* created a standalone recurring event with the default Los Angeles timezone
  and ten-minute unified reminder;
* updated its recurring time in place;
* cancelled the linked provider series; and
* recreated it with a fresh provider identity.

Every approved operation reached Google once and completed successfully. The
first create card had remained open beyond Calendar freshness, so approval
failed closed and the same card was rebuilt before the successful decision.
That is the intended stale-preview recovery path, not a provider retry.

The smoke also exposed an expired Milestone 3.6 whole-term proposal eligible
for daily carryover. It was terminalized and its unactivated reminder plans
were cancelled. Because Docket is alpha software, the subsequent cleanup
removed that compatibility workflow entirely instead of maintaining a runtime
retirement path.
