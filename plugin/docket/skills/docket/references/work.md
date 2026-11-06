# Actual work, timers and visibility

For actual work, use `docket_get_work_context` (optional engagement search or selected
refs), then `docket_log_work_session`. Supply ISO start/end with offsets, or positive
duration_minutes with work_date or a reporting period; incomplete reports are valid.
Unknown attribution can remain attribution_label. Create an engagement only when
useful; payer and compensation terms are optional. Reported billable/paid_work does
not establish eligibility or receipt of money. Event links do not establish attendance.
Use `docket_update_work_session` for corrections or status="retracted"; null clears
optional fields. A supplied duration selects duration-only reporting, while supplied
bounds select interval time. No calendar/provider action follows from logging work.
Use `docket_query_work_sessions` for full-set totals, grouped by engagement,
organization, project, day or week. date_to is exclusive. Known reported quantities,
unique timed coverage, overlaps, unallocated quantities and open estimates remain
separate; these are informal reports without verified pay eligibility. An empty
report does not establish inactivity or complete coverage.

For JSON or CSV, set format and concatenate returned content chunks in order,
following next_cursor until absent. Continuation retains its original work revisions,
period, timezone and calculation basis even after corrections. Use report_cursor to
restart that same basis in another format; a fresh query observes newer revisions.
Exports require read authority and create no claim, submission or provider effect.
Use `docket_start_work_session` to track current work. Pause/resume/stop take the
selected work_ref and its context. Default transition times are retained request
instants, including previews and unchanged retries. A preview starts no timer.
`docket_stop_work_session` accepts an optional reported past end when the operator
supplies it; this remains reported time. `docket_switch_work_session` atomically
stops from_work_ref and starts the new work at one instant. An existing primary
timer is returned rather than silently replaced. Running values are as-of estimates.
Metadata corrections are allowed while running; ordinary bound corrections follow
stopping. Explicit retraction can abandon a timer without inventing an end.
Use `docket_split_work_session` for two or more parts of recorded work. Bounded
parts supply start/end and inherit breaks; duration-only parts supply minutes.
`docket_merge_work_sessions` takes selected work_refs and any conflicting attribution
overrides. Docket conserves quantity and known gaps, retains lineage and supersedes
parents atomically. Refresh affected context; retry an unchanged command unchanged.
Mixed/incomplete reports and duration reports with different dates retain their own
precision and placement inside composite work. Unknown quantities stay unknown;
known_duration_us is only the known subtotal. Work context returns time_components.
Select a component's source_work_ref in each split part, optionally providing bounds
or minutes when dividing known work. Docket derives quantity identity internally.
No claim graph is a client input.


Use `docket_show_work_session` for an explicit Calendar display. Resolve a destination
with calendar context, then get combined context for work_ref and calendar_ref.
The command derives a revision-pinned Event, routing and provider intent. It creates
no extra accounting hours. Its title reports the revision and net hours; the visible
span can include reported breaks. Duration-only, incomplete or composite work cannot
invent a Calendar interval and remains valid without a display.

Work corrections commit immediately and mark an enabled display needs_refresh.
When provider evidence is ready, use the same show command with a new operation_id
to refresh from current work. A blocked refresh does not undo the work correction;
do not log or correct the work again. Snapshot Events are read-only through event
editing tools. Use `docket_retract_record` with the wcal_ to hide the display. After
split/merge or retraction, an old snapshot can be hidden; it cannot refresh from a
superseded/retracted source. Showing successor work is a separate explicit choice.
Follow the original delivery refs through unknown outcomes; never create replacement
displays merely because a provider response was lost.
