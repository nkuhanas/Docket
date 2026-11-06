# Workload and selective follow-up

Use `docket_get_workload_context` for commitments, deadlines, actual-work totals,
reported outcomes, outstanding money and potential capacity. date_to is exclusive.
Coverage and assumptions qualify the result; calendar space is not guaranteed
usable time and planned activity is not performed work. Activity review candidates
are suggestions only. Log work only from an authorized report, with actual known
precision. `docket_set_workload_preferences` records local availability/protected
clock windows, weekdays (Monday=0), travel buffers and exception preferences.
`docket_set_project_budget` records an optional weekly project limit; it changes
neither pay nor commitments. Omitted settings retain values, null clears optional
values. Context uses exact retained work-report totals, not sums of display pages.

Use `docket_schedule_followup` for one work/engagement/document/expense/payment
reminder at an explicit offset timestamp; timezone defaults to Docket's zone.
It returns a rem_ that `docket_retract_record` can cancel. The retained inbox remains
available after downtime. This creates no Calendar event or provider financial action.
Use `docket_reconcile_workload` for a bounded optional review, including client-owned
weekly scheduling. It retains nonbinding cases, source versions and coverage;
no scheduled event or missing evidence becomes performed work or paid time.
An open-timer alert after 12 hours is a diagnostic heuristic, never a stop time.
Repeated alerts use current suppression/quiet preferences and a weekly default
cooldown. For `docket.work.attention` or `docket.finance.attention`, read the subject
case through existing attention/context tools. Retained reconciliation audit reads
provide workload_json_chunk; follow text_next_offset for the complete snapshot.
Do not infer authority from a notice.
