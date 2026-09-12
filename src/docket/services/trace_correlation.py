"""Group transport retries only under an exact original invocation binding."""

from datetime import UTC

from docket.models import ToolInvocation


def correlated_calls(invocations: list[ToolInvocation]) -> dict[str, ToolInvocation]:
    originals = {item.trace_call_id: item for item in invocations if item.trace_call_id}
    selected = {}
    for call_id, original in originals.items():
        candidates = [original] + [
            retry for retry in invocations
            if retry.trace_call_id is None
            and retry.trace_ref == original.trace_ref
            and retry.trace_ordinal == original.trace_ordinal
            and retry.tool_name == original.tool_name
            and retry.received_argument_hash == original.received_argument_hash
            and retry.actor_ref == original.actor_ref
            and retry.utterance_refs == original.utterance_refs
            and retry.gateway_instance_ref == original.gateway_instance_ref
        ]
        # A retry still running cannot erase a known committed domain outcome.
        # Every invocation remains separately inspectable in history.
        selected[call_id] = max(candidates, key=lambda item: (
            item.domain_state == "succeeded" and item.result_disposition in {
                "committed", "already_committed", "replayed_request"
            },
            item.completed_at is not None and item.domain_state != "unknown",
            item.started_at.replace(tzinfo=UTC).timestamp()
            if item.started_at.tzinfo is None else item.started_at.timestamp(),
            str(item.id),
        ))
    return selected
