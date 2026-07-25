# Milestone 3.7 verification

Date: 2026-07-24 (America/Los_Angeles)

Milestone 3.7 replaces the normal aggregate-schedule workflow with independent
course lifecycles. The authoritative private specification is `spec-2.3.md` and
remains outside this repository.

## Delivered contract

* A term supplies shared bounds and timezone; it does not own a schedule list.
* Each course/section is one canonical record with stable child meeting IDs.
* `docket_propose_course_reconciliation` derives create, update, cancel, and
  no-op items for one course.
* `drop` persists independently retryable cancellation progress and archives
  only after every active link is confirmed cancelled.
* `docket_restore_record` reactivates the same canonical identity; the next
  approved sync creates fresh provider event IDs for its current meetings.
* Re-importing unchanged data and reconciling an unchanged course are no-ops.
  Omitting a course has no effect.
* `docket_store_term_schedule` and `docket_propose_term_schedule` remain
  available only for compatibility with existing durable history.

No schema migration is required. Existing action, operation, operation-item,
record-status, Calendar-link snapshot, audit, and outbox columns already admit
the new closed action types and lifecycle transitions.

## Automated evidence

The integration lifecycle uses the stateful fake Calendar adapter and proves:

1. add two meetings;
2. update one stable series, cancel one, and create one;
3. unchanged reconciliation without a card;
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
mypy          -> passed (65 source files)
pytest -q     -> 216 passed, 1 dependency deprecation warning
skill check   -> Skill is valid
```

The warning is the existing FastAPI/Starlette `httpx` compatibility
deprecation; it is unrelated to this milestone.

## MCP and Hermes contract

The generated and allowlisted surface contains 22 tools. The additions are:

* `docket_restore_record`
* `docket_propose_course_reconciliation`

Hermes plugin `0.15.0` recognizes both in redacted MCP traces. The
`docket-manual-intent` skill now treats bulk input as resumable per-course
orchestration, allocates a distinct intent index to every write/proposal,
requires explicit drop, and follows restore with reconciliation. The skill
passed the Skill Creator validator.

Existing sessions still cache MCP discovery. Run `/reload-mcp` after the
deployment before the live gate.

## Operator-present gate

Pending operator execution. Use one disposable course and stop after each
decision card when requested:

```text
add -> change one meeting/add one/remove one -> drop -> re-add
```

During the gate, confirm:

* each proposal appears in the current ISO queue thread;
* course review pages show only operationally relevant fields;
* `docket-system` receives redacted MCP traces and rich lifecycle entries;
* partial or uncertain cancellation never archives the course;
* the completed drop archives exactly once;
* restore retains the course record identity and creates fresh Google series;
* an unchanged final re-import/reconciliation produces no duplicate series.

Do not proceed to Gmail ingestion until this gate is recorded here.
