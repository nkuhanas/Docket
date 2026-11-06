# Native command examples

These examples illustrate the actual published flat schemas. Substitute references
returned by this connection; the placeholder handles are not usable credentials.
No compiler discriminator, internal IDs or `work` envelope is supplied.

Read `docket_get_overview`, `docket_get_calendar_context` for destinations, or
`docket_get_context(refs=[...])` for selected targets and observed versions.
Context observations state their coverage; an empty sample is not proof of absence.

Create an event with `docket_create_event`:

```json
{"context_id":"<calendar-context>","operation_id":"meeting-1","title":"Example meeting","start":"2026-11-13T13:00:00-08:00","end":"2026-11-13T14:00:00-08:00","timezone":"America/Los_Angeles","calendar_ref":"<Meetings lane>","location":"https://example.test/meeting","notes":"Reported participants"}
```

Update only supplied fields with `docket_update_event`:

```json
{"context_id":"<event-context>","operation_id":"meeting-1-edit","event_ref":"<event>","title":"Updated meeting","location":null}
```

Cancel with `docket_cancel_event`:

```json
{"context_id":"<current-event-context>","operation_id":"meeting-1-cancel","event_ref":"<event>"}
```

Recurring edits/cancellation require `scope="series"` or `scope="occurrence"`.
An occurrence supplies its original `occurrence_start` ISO timestamp or all-day
`occurrence_date`; the current moved time is not the occurrence identity.
All-day creation uses `all_day=true`, ISO dates, and an exclusive `end` date.
`recurrence` uses named frequency/interval/weekdays/count/until fields in the schema.
Entity/item/time references can be linked from a selected object context containing
the destination and those objects. Names in notes do not send invitations.

Create a task with `docket_create_task`:

```json
{"context_id":"<overview-context>","operation_id":"agenda-1","title":"Prepare agenda","priority":"high","due":"2026-11-12","timezone":"America/Los_Angeles"}
```

Update with `docket_update_task`:

```json
{"context_id":"<task-context>","operation_id":"agenda-edit","task_ref":"<task>","notes":"Include project updates","priority":"normal"}
```

Complete with `docket_update_task`:

```json
{"context_id":"<current-task-context>","operation_id":"agenda-done","task_ref":"<task>","state":"completed"}
```

The same unchanged command and operation_id recovers its receipt. Different intent
uses a new ID. Set `preview=true` on a business command when a preview is wanted;
`docket_apply_change(change_id=...)` commits that exact retained proposal. A corrected
preview supplies a new operation_id plus replaces_change_id from the old proposal.
`docket_prepare_batch(operation_id=..., change_ids=[...])` combines ready previews
for distinct targets into one atomic change; apply the returned handle. The batch
and its members cannot create duplicate effects on retry.

A canonical receipt is separate from provider delivery. Follow its delivery/history
reference for queued or uncertain work. Never recreate a committed event to repair
delivery. Optional `docket_record_context` capture is independent of these commands.

Other named domain tools use ordinary fields from their actual MCP schemas. See
[the complete capability disposition](native-capabilities.md). Historical saved
requests/revisions remain available through bounded owner-scoped recovery reads.

Work recording is implemented in the current source; its release is still pending.
For “log three hours of billable Example engineering yesterday,” read
`docket_get_work_context(search="Example")`, then use the configured date/timezone
and a known engagement_ref if available. An unresolved arrangement may remain a label:

```json
{"context_id":"<work-context>","operation_id":"example-work-1","description":"Engineering investigation","attribution_label":"Example","work_date":"2026-11-04","duration_minutes":180,"reported_compensation_class":"billable"}
```

This records performed work without creating a calendar event or assuming a payer,
rate or payment. Unknown quantity is valid: omit duration and bounds. Exact work
uses ISO start/end and optional breaks, each with start/end. Work context supplies
current versions for `docket_update_work_session`; corrections append history.
Supply only changed fields, use null to clear optional fields, or status="retracted"
for a logical retraction. Earlier revisions remain readable through history.
Query the reported week with `docket_query_work_sessions`:

```json
{"date_from":"2026-11-02","date_to":"2026-11-09","group_by":"engagement"}
```

Totals cover the entire matching set, independently of the bounded items/groups
pages. They distinguish known quantities, unknown quantity, unique timed coverage,
overlap, unallocated time and open estimates. Unknown attribution has its own bucket.
Daily/weekly views never spread an imprecise period evenly across days. Intervals
that match the period but have no active time remain visible with zero active time.

For a CSV export of that exact report, use its report_cursor:

```json
{"cursor":"<report-cursor>","format":"csv"}
```

Concatenate content chunks, passing next_cursor back until it is absent. JSON
exports use format="json" and the same continuation behavior. Chunks may end
inside a long record; concatenation preserves every UTF-8 character and field.
CSV contains total/group/work rows with stable references and reporting basis.
report_cursor restarts the same retained basis; a fresh query sees later corrections.
Read authority is sufficient. An export is neither a submitted timesheet nor a
financial claim or provider operation. Split/merge and finance remain later milestones.

Current work uses a primary timer:

```json
{"context_id":"<work-context>","operation_id":"validation-start","description":"Hardware qualification","attribution_label":"Example"}
```

Call `docket_start_work_session` with that input. Docket retains the request's
instant/timezone; optional preview/apply and unchanged retries use that same instant.
A preview starts no timer. Work context returns the active timer, versions and
available pause/resume/stop actions. Those commands take context_id, operation_id
and work_ref. Stop optionally records an outcome. A reported past end timestamp
can stop a forgotten timer without first recording an invented current end.

Switch work atomically with `docket_switch_work_session`:

```json
{"context_id":"<current-work-context>","operation_id":"validation-switch","from_work_ref":"<primary-work>","description":"Second activity","previous_outcome":"Qualification recorded"}
```

The old end and new start are one retained instant. Starting while a timer is
already running or paused returns that timer and context guidance. Pauses exclude
time; restarts/disconnects do not end a timer. Running durations are as-of estimates.
Metadata remains editable while active. Explicit status="retracted" abandons a
timer and preserves its unknown end and history. Canonical receipts retain compact
work states/times from the commit; unchanged retries recover those exact receipts.
