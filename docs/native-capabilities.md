# Native capability disposition

Evidence for SPEC 1.0; generated from the actual public domain definitions.
All 47 baseline canonical mutation variants are retained.

| Internal effect | Native business tool |
| --- | --- |
| `entity_create` | `docket_create_entity` |
| `entity_modify` | `docket_update_entity` |
| `entity_retract` | `docket_retract_entity` |
| `identity_handle_create` | `docket_create_identity_handle` |
| `identity_handle_modify` | `docket_update_identity_handle` |
| `identity_binding_bind` | `docket_bind_identity_binding` |
| `identity_binding_unbind` | `docket_unbind_identity_binding` |
| `identity_handle_retract` | `docket_retract_identity_handle` |
| `affiliation_create` | `docket_create_affiliation` |
| `affiliation_update` | `docket_update_affiliation` |
| `affiliation_supersede` | `docket_replace_affiliation` |
| `affiliation_retract` | `docket_retract_affiliation` |
| `relationship_create` | `docket_create_relationship` |
| `relationship_update` | `docket_update_relationship` |
| `relationship_supersede` | `docket_replace_relationship` |
| `relationship_retract` | `docket_retract_relationship` |
| `fact_create` | `docket_create_fact` |
| `fact_update` | `docket_update_fact` |
| `fact_supersede` | `docket_replace_fact` |
| `fact_retract` | `docket_retract_fact` |
| `interaction_create` | `docket_create_interaction` |
| `preference_create` | `docket_create_preference` |
| `preference_modify` | `docket_update_preference` |
| `preference_retract` | `docket_retract_preference` |
| `calendar_lane_create` | `docket_create_calendar_lane` |
| `calendar_lane_modify` | `docket_update_calendar_lane` |
| `calendar_lane_retract` | `docket_retract_calendar_lane` |
| `lane_routing_decision_create` | `docket_create_lane_routing_decision` |
| `item_create` | `docket_create_item` |
| `item_modify` | `docket_update_item` |
| `item_retract` | `docket_retract_item` |
| `temporal_binding_create` | `docket_create_temporal_binding` |
| `temporal_binding_modify` | `docket_update_temporal_binding` |
| `temporal_binding_supersede` | `docket_replace_temporal_binding` |
| `temporal_binding_retract` | `docket_retract_temporal_binding` |
| `task_retract` | `docket_retract_task` |
| `temporal_calendar_projection_create` | `docket_create_temporal_calendar_projection` |
| `temporal_calendar_projection_modify` | `docket_update_temporal_calendar_projection` |
| `temporal_calendar_projection_retract` | `docket_retract_temporal_calendar_projection` |
| `reminder_plan_create` | `docket_create_reminder_plan` |
| `reminder_plan_modify` | `docket_update_reminder_plan` |
| `reminder_plan_retract` | `docket_retract_reminder_plan` |
| `canonical_event_create` | `docket_create_event` |
| `canonical_event_modify` | `docket_update_event` |
| `canonical_event_cancel` | `docket_cancel_event` |
| `task_create` | `docket_create_task` |
| `task_modify` | `docket_update_task / docket_complete_task` |

Conflict decisions use `docket_resolve_conflict`, including optional exact prepared
effects through the shared guarded service. Clarification choices remain non-executable.
Attention dispositions use `docket_resolve_attention_case` with context-held case
version/revision. This resolution operation is separate from the 47-variant
canonical mutation union. Capture is optional. Prepared batching retains one atomic canonical/provider transaction.
Historical drafts remain available through bounded owner-scoped saved-work reads.
No current capability was silently retired during catalog cutover.
