# Semantic acceptance matrix

This matrix maps the private semantic handoff's acceptance requirements to
authoritative repository evidence. A green repository test proves deterministic
domain and projection behavior; it does not substitute for the explicitly
listed operator-present Discord/Gmail observations.

## Explicit authority and ascertainment

| Requirement | Automated evidence | Current evidence |
| --- | --- | --- |
| Complete known event executes without approval | `test_explicit_create_update_entity_rebind_and_cancel_need_no_approval` | Passed and deployed |
| Explicit update executes without approval | Same end-to-end test | Passed and deployed |
| Explicit cancellation executes without approval | Same end-to-end test | Passed and deployed |
| Explicit organization/context correction persists | Same end-to-end test plus `test_entity_registry_resolves_aliases_and_preserves_ambiguity` | Passed and deployed |
| Unknown entity is unresolved/provisional, never a permanent `unknown` fact | `test_inferred_unknown_entity_is_explicitly_provisional` | Passed and deployed |
| Unknown required organization pauses and resumes after registration | `test_required_inferred_entity_pauses_then_resumes_after_registration` | Passed and deployed |
| Ambiguous mention requires selection | `test_entity_registry_resolves_aliases_and_preserves_ambiguity` | Passed and deployed |
| Registration and aliases improve later matching | `test_explicit_registration_resolves_an_existing_unknown_mention` and entity registry test | Passed and deployed |
| Correction/merge preserves relationships and future resolution | Entity registry test | Passed and deployed |
| No seed list is required | Entity service has no seed path; manual-intent contract forbids seeding | Passed static audit; conversational observation pending |

## Inferred Gmail semantics and correlation

| Requirement | Automated evidence | Current evidence |
| --- | --- | --- |
| Gmail extraction persists typed candidates, not housekeeping | `test_triage_persists_typed_event_candidate_without_housekeeping_or_card` | Passed and deployed |
| Empty extraction and duplicate thread are idempotent | `test_empty_extraction_and_duplicate_thread_candidate_are_idempotent` | Passed and deployed |
| Application receipt is awareness/suppressed with no individual card | Morning/night brief integration tests and `test_passive_gmail_notification_renders_without_local_controls` | Passed and deployed |
| Complete invitation creates one inferred, version-bound proposal | `test_complete_inferred_event_becomes_one_version_bound_proposal` | Passed and deployed |
| Exact Calendar match is a no-op | `test_existing_provider_event_is_noop_then_cancellation_needs_no_replacement` | Passed and deployed |
| Update/cancellation converge with a pending create | `test_update_and_cancellation_reconcile_one_pending_create_formulation` | Passed and deployed |
| New evidence supersedes the current edited proposal, not a historical revision | `test_new_evidence_supersedes_current_edited_revision_and_aggregate_card` | Passed and deployed |
| Repeat cancellation and materially unchanged update are no-ops | Semantic supersession integration tests | Passed and deployed |
| Independent Google edit is recorded as divergence, not overwritten | `test_independent_provider_edit_is_recorded_as_divergence_not_canonical_drift` | Passed and deployed |
| Related non-event follow-ups consolidate without collapsing unrelated same-title items | Night brief test with explicit `topic_key` correlation | Passed and deployed |

## Decisions, conflicts, and execution

| Requirement | Automated evidence | Current evidence |
| --- | --- | --- |
| One inferred approval adopts canonical state and executes once | `test_complete_inferred_event_becomes_one_version_bound_proposal` | Passed and deployed |
| Explicit no-conflict create executes directly | Explicit standalone lifecycle test | Passed and deployed |
| Explicit/inferred conflicts remain advisory decision context | Standalone conflict tests and inferred formulation service path | Passed and deployed; inferred live observation pending |
| Keep both preserves both events | `test_non_destructive_conflict_choices_preserve_the_selected_events[keep_both]` | Passed and deployed |
| Existing event wins performs no proposed-event write | Same parametrized test for `keep_existing` | Passed and deployed |
| Proposed event wins compiles explicit cancel/create operations | `test_explicit_conflict_resolution_new_wins_runs_a_durable_bundle` | Passed and deployed |
| Partial provider execution remains durable and honest | Same test's permanent-failure case | Passed and deployed |

## Projection convergence

| Requirement | Automated evidence | Current evidence |
| --- | --- | --- |
| Carried-forward click targets the exact visible projection | `test_carried_forward_approval_refreshes_the_clicked_card_and_repairs_duplicates` | Passed and deployed |
| Accepted/duplicate decisions remove controls and execute once | Same test | Passed and deployed |
| Lost Discord edit acknowledgement retries idempotently | Same test with `discard_next_projection_ack` | Passed and deployed |
| Restart reconciliation repairs stale interactive projections | Same test's `enqueue_stale_projection_repairs` assertions | Passed and deployed |
| Material semantic change invalidates the old approval and aggregate view | Semantic supersession tests | Passed and deployed |
| Exhausted projection failure preserves canonical state and alerts system channel | `test_exhausted_projection_reports_one_durable_system_alert` | Passed and deployed |

## Daily cadence

| Requirement | Automated evidence | Current evidence |
| --- | --- | --- |
| Overnight sources emit no normal per-email stream | `test_overnight_event_is_silent_until_idempotent_morning_brief` | Passed and deployed |
| Several overnight sources yield exactly one morning message | Same test | Passed and deployed |
| Morning message navigates separate canonical decisions in place | Same test | Passed and deployed |
| Overnight noise stays suppressed | Typed-candidate and brief filtering tests | Passed and deployed |
| One morning/night brief per local date across retries/restarts | Morning/night brief tests and unique database constraints | Passed and deployed |
| Waking actionable work may surface immediately | Night brief integration test creates daytime action cards | Passed and deployed |
| Routine daytime awareness stays quiet until closeout | Same test | Passed and deployed |
| Night brief reports outcomes, unresolved work, and awareness without replay | `test_night_brief_consolidates_daytime_action_and_awareness` | Passed and deployed |
| Delayed classification crosses every published boundary without loss | `test_delayed_candidate_advances_past_every_published_window` | Passed and deployed |

## Live/runtime evidence still required

The isolated live triage profile is deployment-validated against exactly four
tools. A stale legacy submission allowlist was repaired on 2026-08-26, and a
controlled pass proved semantic candidates can again be persisted. The
remaining operator-present evidence is intentionally narrow:

1. A routine real receipt produces no actionable card.
2. A real invitation produces one inferred proposal; a changed follow-up
   replaces it rather than creating a second active decision.
3. A complete natural-language Discord event command executes directly.
4. An unknown organization triggers one concise registration question and the
   original command resumes after the answer.
5. One real morning and night boundary each produces no more than its single
   cohesive brief message.

Do not mark the semantic handoff complete until those observations are recorded
or equivalent authoritative live evidence exists.
