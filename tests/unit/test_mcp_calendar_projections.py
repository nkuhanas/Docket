from docket.mcp.server import _calendar_events_summary


def test_calendar_summary_omits_provider_and_duplicate_timing_details() -> None:
    result = _calendar_events_summary(
        {
            "account_ref": "acct_01M1D000000000000000000000",
            "calendar_ids": ["personal@example.com"],
            "range_start": "2026-08-31T07:00:00+00:00",
            "range_end": "2026-09-01T07:00:00+00:00",
            "range_resolution": "explicit",
            "result_view": "occurrences",
            "events": [
                {
                    "provider_event_id": "provider-secret-id",
                    "recurring_event_id": "provider-series-id",
                    "ref": "evt_01M1D000000000000000000001",
                    "lane_ref": "lane_01M1D00000000000000000001",
                    "calendar_id": "personal@example.com",
                    "object_type": "event",
                    "semantic_role": "occurrence",
                    "status": "confirmed",
                    "summary": "Office hours",
                    "location": "Building 14",
                    "is_all_day": False,
                    "start_at": "2026-08-31T17:00:00+00:00",
                    "end_at": "2026-08-31T18:00:00+00:00",
                    "start_local": "2026-08-31T10:00:00-07:00",
                    "end_local": "2026-08-31T11:00:00-07:00",
                    "local_timezone": "America/Los_Angeles",
                    "timezone": "America/Los_Angeles",
                    "event_type": "default",
                    "recurrence_kind": "one_time",
                    "reminder_plan": {
                        "state": "canonical",
                        "lead_seconds": [600],
                    },
                }
            ],
            "count": 1,
            "total_if_known": 1,
            "truncated": False,
            "freshness_by_calendar": {
                "personal@example.com": {
                    "stale": False,
                    "covered": True,
                }
            },
            "refresh_pending": False,
            "refresh_disabled": False,
        }
    )

    item = result["items"][0]
    assert item == {
        "ref": "evt_01M1D000000000000000000001",
        "lane_ref": "lane_01M1D00000000000000000001",
        "calendar_id": "personal@example.com",
        "object_type": "event",
        "semantic_role": "occurrence",
        "status": "confirmed",
        "summary": "Office hours",
        "location": "Building 14",
        "timing": {
            "kind": "timed",
            "start_local": "2026-08-31T10:00:00-07:00",
            "end_local": "2026-08-31T11:00:00-07:00",
            "timezone": "America/Los_Angeles",
        },
    }
    serialized = repr(result)
    assert "provider-secret-id" not in serialized
    assert "provider-series-id" not in serialized
    assert "reminder_plan" not in serialized
    assert "start_at" not in serialized
