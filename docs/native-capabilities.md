# Native capability disposition

Generated from current canonical definitions and their native public routes.
All 61 canonical mutation variants have an advertised route.

| Internal effect | Native business tool |
| --- | --- |
| `entity_create` | `docket_create_entity` |
| `entity_modify` | `docket_update_entity` |
| `entity_retract` | `docket_retract_record` |
| `identity_handle_create` | `docket_create_identity_handle` |
| `identity_handle_modify` | `docket_update_identity_handle` |
| `identity_binding_bind` | `docket_bind_identity_binding` |
| `identity_binding_unbind` | `docket_unbind_identity_binding` |
| `identity_handle_retract` | `docket_retract_record` |
| `affiliation_create` | `docket_create_affiliation` |
| `affiliation_update` | `docket_update_affiliation` |
| `affiliation_supersede` | `docket_replace_affiliation` |
| `affiliation_retract` | `docket_retract_record` |
| `relationship_create` | `docket_create_relationship` |
| `relationship_update` | `docket_update_relationship` |
| `relationship_supersede` | `docket_replace_relationship` |
| `relationship_retract` | `docket_retract_record` |
| `fact_create` | `docket_create_fact` |
| `fact_update` | `docket_update_fact` |
| `fact_supersede` | `docket_replace_fact` |
| `fact_retract` | `docket_retract_record` |
| `interaction_create` | `docket_create_interaction` |
| `preference_create` | `docket_create_preference` |
| `preference_modify` | `docket_update_preference` |
| `preference_retract` | `docket_retract_record` |
| `calendar_lane_create` | `docket_create_calendar_lane` |
| `calendar_lane_modify` | `docket_update_calendar_lane` |
| `calendar_lane_retract` | `docket_retract_record` |
| `lane_routing_decision_create` | `docket_create_lane_routing_decision` |
| `item_create` | `docket_create_item` |
| `item_modify` | `docket_update_item` |
| `item_retract` | `docket_retract_record` |
| `temporal_binding_create` | `docket_create_temporal_binding` |
| `temporal_binding_modify` | `docket_update_temporal_binding` |
| `temporal_binding_supersede` | `docket_replace_temporal_binding` |
| `temporal_binding_retract` | `docket_retract_record` |
| `task_retract` | `docket_retract_record` |
| `temporal_calendar_projection_create` | `docket_create_temporal_calendar_projection` |
| `temporal_calendar_projection_modify` | `docket_update_temporal_calendar_projection` |
| `temporal_calendar_projection_retract` | `docket_retract_record` |
| `reminder_plan_create` | `docket_create_reminder_plan` |
| `reminder_plan_modify` | `docket_update_reminder_plan` |
| `reminder_plan_retract` | `docket_retract_record` |
| `canonical_event_create` | `docket_create_event` |
| `canonical_event_modify` | `docket_update_event` |
| `canonical_event_cancel` | `docket_cancel_event` |
| `task_create` | `docket_create_task` |
| `task_modify` | `docket_update_task` |
| `engagement_create` | `docket_create_engagement` |
| `timesheet_prepare` | `docket_prepare_timesheet` |
| `timesheet_transition` | `docket_update_timesheet` |
| `timesheet_refresh` | `docket_refresh_timesheet` |
| `engagement_modify` | `docket_update_engagement` |
| `engagement_pay_terms_set` | `docket_set_pay_terms` |
| `engagement_reporting_rules_set` | `docket_set_reporting_rules` |
| `work_session_create` | `docket_log_work_session` |
| `work_session_modify` | `docket_update_work_session` |
| `work_session_split` | `docket_split_work_session` |
| `work_session_merge` | `docket_merge_work_sessions` |
| `work_timer_start` | `docket_start_work_session` |
| `work_timer_transition` | `docket_pause_work_session` |
| `work_timer_switch` | `docket_switch_work_session` |

Shared retraction dispatches the existing typed handler from the target reference.
Identity unbinding and Calendar cancellation retain their distinct domain semantics.
Compatibility aliases retain original authority/retry handling outside discovery.

Timer transitions also have named resume/stop tools; switching is one atomic effect.

Conflict and attention decisions retain observed versions and exact proposals.
Capture is optional. Batching remains one atomic canonical/provider transaction.
Saved work retains exact historical revisions and authentic roots.
