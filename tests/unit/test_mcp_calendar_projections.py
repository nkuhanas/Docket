from docket.mcp.server import _calendar_events_summary


def test_calendar_summary_paginates_large_selectors_without_losing_rows() -> None:
    import json

    events = [
        {
            "ref": f"evt_{index:026d}",
            "summary": "A" * 500,
            "location": "B" * 1000,
            "start_local": "2026-09-08T15:00:00",
            "end_local": "2026-09-08T15:50:00",
            "local_timezone": "America/Los_Angeles",
            "mutation_target": {
                "ref": f"evt_{index:026d}",
                "version": 2,
                "scope": {
                    "kind": "occurrence",
                    "identity": {
                        "series_ref": f"evt_{index:026d}",
                        "original_date": "2026-09-08",
                        "original_start_local": "2026-09-08T15:00:00",
                        "timezone": "America/Los_Angeles",
                        "fold": 0,
                    },
                },
            },
        }
        for index in range(25)
    ]
    page = _calendar_events_summary(
        {"events": events, "count": 25, "total_if_known": 100}, offset=25
    )
    assert 0 < page["count"] < 25
    assert len(page["items"]) == page["count"]
    assert page["cursor"] == str(25 + page["count"])
    assert page["truncated"]
    assert len(json.dumps(page, ensure_ascii=False).encode()) <= 14 * 1024


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
