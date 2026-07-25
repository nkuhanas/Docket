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
* Re-importing unchanged data, repeating an equal explicit replacement, and
  reconciling an unchanged course preserve the version and perform no provider
  work. Omitting a course has no effect.
* `docket_store_term_schedule` and `docket_propose_term_schedule` remain
  available only for compatibility with existing durable history.

No schema migration is required. Existing action, operation, operation-item,
record-status, Calendar-link snapshot, audit, and outbox columns already admit
the new closed action types and lifecycle transitions.

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
mypy          -> passed (65 source files)
pytest -q     -> 218 passed, 1 dependency deprecation warning
skill check   -> Skill is valid
```

The warning is the existing FastAPI/Starlette `httpx` compatibility
deprecation; it is unrelated to this milestone.

## MCP and Hermes contract

The generated and allowlisted surface contains 22 tools. The additions are:

* `docket_restore_record`
* `docket_propose_course_reconciliation`

Hermes plugin `0.15.1` recognizes both in redacted MCP traces. The
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
  `no_op`. Automated coverage proves the corrected path; live verification
  resumes after deployment.

Remaining:

```text
drop -> re-add -> unchanged repeat
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
