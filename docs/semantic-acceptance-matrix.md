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
| No seed list is required | Entity service has no seed path; manual-intent contract forbids seeding | Passed static audit and live organic registration |

## Inferred Gmail semantics and correlation

| Requirement | Automated evidence | Current evidence |
| --- | --- | --- |
| Gmail extraction persists typed candidates, not housekeeping | `test_triage_persists_typed_event_candidate_without_housekeeping_or_card` | Passed and deployed |
| Untrusted legacy classification cannot formulate Gmail housekeeping | `test_untrusted_content_cannot_propose_a_gmail_action` | Passed and deployed in `6811106` |
| Empty extraction and duplicate thread are idempotent | `test_empty_extraction_and_duplicate_thread_candidate_are_idempotent` | Passed and deployed |
| Application receipt is awareness/suppressed with no individual card | Morning/night brief integration tests and `test_passive_gmail_notification_renders_without_local_controls` | Passed and deployed |
| Complete invitation creates one inferred, version-bound proposal | `test_complete_inferred_event_becomes_one_version_bound_proposal` | Passed and deployed |
| Optional provisional sender/location classification does not block a complete formulation | `test_complete_inferred_event_becomes_one_version_bound_proposal` | Passed and deployed in `ebedb1a` |
| Exact Calendar match is a no-op | `test_existing_provider_event_is_noop_then_cancellation_needs_no_replacement` | Passed and deployed |
| Update/cancellation converge with a pending create | `test_update_and_cancellation_reconcile_one_pending_create_formulation` | Passed and deployed |
| New evidence supersedes the current edited proposal, not a historical revision | `test_new_evidence_supersedes_current_edited_revision_and_aggregate_card` | Passed and deployed |
| Repeat cancellation and materially unchanged update are no-ops | Semantic supersession integration tests | Passed and deployed |
| Independent Google edit is recorded as divergence, not overwritten | `test_independent_provider_edit_is_recorded_as_divergence_not_canonical_drift` | Passed and deployed |
| Related non-event follow-ups consolidate without collapsing unrelated same-title items | Night brief test with explicit `topic_key` correlation | Passed and deployed |
| Independent same-title event creates do not rebind by title alone | `test_same_title_different_time_create_does_not_rebind_existing_event` | Passed and deployed in `6811106` |
| Classifier internals and source identifiers stay out of user cards | `test_complete_inferred_event_becomes_one_version_bound_proposal` renderer assertions | Passed and deployed in `6811106` |

## Decisions, conflicts, and execution

| Requirement | Automated evidence | Current evidence |
| --- | --- | --- |
| One inferred approval adopts canonical state and executes once | `test_complete_inferred_event_becomes_one_version_bound_proposal` | Passed and deployed |
| Explicit no-conflict create executes directly | Explicit standalone lifecycle test | Passed and deployed |
| Explicit/inferred conflicts remain advisory decision context | Standalone conflict tests, `test_course_approval_tolerates_unrelated_snapshot_refresh`, and `test_inferred_event_integrates_calendar_conflicts_into_one_proposal` | Automated standalone and course paths passed and deployed in `f8acdef`; inferred live observation pending |
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
| Reconciliation clears terminal approval-control bindings even when the external card is already current | Same carried-forward projection test | Passed and live residual count is zero in `6811106` |
| Material semantic change invalidates the old approval and aggregate view | Semantic supersession tests | Passed and deployed |
| Exhausted projection failure preserves canonical state and alerts system channel | `test_exhausted_projection_reports_one_durable_system_alert` | Passed and deployed |
| Decision-to-card convergence latency is recorded once at the clicked projection acknowledgement | Carried-forward projection test asserts one `approval.projection_converged` audit event | Passed and deployed in `6811106`; first post-deploy click pending |

## Daily cadence

| Requirement | Automated evidence | Current evidence |
| --- | --- | --- |
| Overnight actionable cards project immediately while routine awareness stays consolidated | `test_overnight_attention_projects_immediately_and_brief_remains_idempotent` | Passed |
| Several overnight sources yield exactly one morning message | Same test | Passed and deployed |
| Morning message navigates separate canonical decisions in place | Same test | Passed and deployed |
| Overnight noise stays suppressed | Typed-candidate and brief filtering tests | Passed and deployed |
| One morning/night brief per local date across retries/restarts | Morning/night brief tests and unique database constraints | Passed and deployed |
| Actionable work projects immediately at every hour | Overnight and night-brief integration tests | Passed |
| Routine daytime awareness stays quiet until closeout | Same test | Passed and deployed |
| Night brief reports outcomes, unresolved work, and awareness without replay | `test_night_brief_consolidates_daytime_action_and_awareness` | Passed and deployed |
| Delayed classification crosses every published boundary without loss | `test_delayed_candidate_advances_past_every_published_window` | Passed and deployed |

## Live/runtime evidence

The isolated live triage profile is deployment-validated against exactly four
tools. A stale legacy submission allowlist was repaired on 2026-08-26, and a
controlled pass proved semantic candidates can again be persisted. The
following content-free production queries provide authoritative live
acceptance evidence:

* Between `2026-08-26T23:23:45Z` and `2026-08-26T23:24:32Z`, four candidates
  matching receipt/application-confirmation terminology were durably resolved
  as `information`; all four have no queue item and therefore no actionable
  card.
* The published `morning` brief for 2026-08-26 and `night` brief for 2026-08-25
  each have exactly one Discord projection and one external Discord message.
* On 2026-08-26, a complete natural-language Discord create command with an
  available time slot executed under `explicit_user` authority without an
  approval. Its queue presentation was suppressed, the provider link was
  confirmed, and its ten-minute Google popup and Docket reminder plan were
  activated. The action and provider operation succeeded, both system-log
  projections were delivered, and every associated MCP trace reached the
  system channel without an error. A preceding command for an occupied slot
  correctly produced conflict-resolution context instead of executing.
* On 2026-08-26, an event naming an unregistered organization paused for one
  ascertainment choice. Selecting registration created an active organization
  under `explicit_user` authority and resumed the original Discord intent with
  its next request index. The resumed event preserved that organization as its
  organizer, executed without a second approval, and reached active canonical
  state with one provider link. Both system-log projections and every related
  MCP trace were delivered without an error.
* On 2026-08-26, a fresh Gmail invitation processed after `ebedb1a` retained a
  first-time sender as an optional provisional person and produced exactly one
  inferred action with one pending approval and no clarification. Two rapid
  same-thread follow-ups then arrived: the accidental material repeat resolved
  without a decision, while the actual time change superseded the original
  queue item, action, and approval. The surviving proposal retained the same
  canonical event identity at version 2 with the corrected timing, and exactly
  one pending approval remained. This observation exposed and closed the final
  cadence gap: brief-window membership no longer defers its interactive card.

The semantic handoff's operator-present gates are complete. The deferred
invitation decision remains an ordinary operational item to approve or reject
from its carried-forward morning card, not an additional implementation gate.
