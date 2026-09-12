from __future__ import annotations

import hashlib
import json

from docket.models import ChangeSetRevision
from docket.services.changeset_diff import bounded_details, bounded_sample, draft_diff


def _revision(*, entries=(), actions=(), ownership=()):
    return ChangeSetRevision(
        normalized_entries_json=list(entries),
        staged_actions_json=list(actions),
        compiled_action_ownership_json=list(ownership),
        compiler_manifest_json={},
    )


def test_diff_reports_exact_values_scope_and_absent_versus_null():
    original = {
        "change_id": "edit",
        "mutation_type": "canonical_event_modify",
        "object_ref": "evt_example",
        "scope": {"kind": "occurrence"},
        "payload": {"title": "Old title", "location": None, "metadata_json": {"flag": False}},
        "basis_refs": ["utt_old"],
    }
    replacement = {
        **original,
        "scope": {"kind": "entire_series"},
        "payload": {"title": "Correct topic", "metadata_json": {"flag": 0}},
        "basis_refs": ["utt_new"],
    }
    rows, counts = draft_diff(
        _revision(actions=[original]),
        _revision(actions=[replacement]),
        mutation_types=[],
        entry_types=[],
    )
    assert counts == {"added": 0, "removed": 0, "modified": 1}
    by_path = {tuple(row["field_path"]): row for row in rows}
    assert set(by_path) == {
        ("payload", "title"),
        ("payload", "location"),
        ("payload", "metadata_json", "flag"),
        ("scope", "kind"),
    }
    assert by_path[("payload", "title")]["before"] == "Old title"
    assert by_path[("payload", "title")]["after"] == "Correct topic"
    assert by_path[("scope", "kind")]["after"] == "entire_series"
    assert by_path[("payload", "location")]["before"] is None
    assert by_path[("payload", "location")]["before_present"] is True
    assert by_path[("payload", "location")]["after_present"] is False
    assert "after" not in by_path[("payload", "location")]


def test_entry_removal_and_type_replacement_keep_ownership_and_filter_semantics():
    old = _revision(
        entries=[
            {"import_entry_id": "one", "entry_type": "scheduled_occurrence_entry", "title": "Quiz"},
            {"import_entry_id": "two", "entry_type": "scheduled_occurrence_entry", "title": "Exam"},
        ],
        actions=[{"change_id": "one.event", "create_spec": {"title": "Quiz"}}],
        ownership=[{"change_ids": ["one.event"]}],
    )
    new = _revision(
        entries=[
            {
                "import_entry_id": "one",
                "entry_type": "schedule_exception_entry",
                "item": {"title": "No class"},
                "exception_disposition": "no_occurrence",
                "compiler_version": 99,
                "statement_ref": "stm_hidden",
            }
        ]
    )
    rows, counts = draft_diff(
        old,
        new,
        mutation_types=[],
        entry_types=["scheduled_occurrence_entry"],
    )
    assert counts == {"added": 0, "removed": 1, "modified": 1}
    assert all(row["subject_kind"] == "entry" for row in rows)
    assert any(row.get("after") == "no_occurrence" for row in rows)
    assert any(row["change"] == "removed" and row.get("before") == "Exam" for row in rows)
    assert all(row["field_path"] not in (["statement_ref"], ["compiler_version"]) for row in rows)


def test_added_entry_exposes_title_time_lane_without_compiler_boilerplate():
    rows, counts = draft_diff(
        None,
        _revision(
            entries=[
                {
                    "import_entry_id": "fair",
                    "entry_type": "scheduled_occurrence_entry",
                    "title": "Career fair",
                    "lane_ref": "lane_meetings",
                    "timing": {
                        "start_local": "2026-09-16T10:00:00",
                        "timezone": "America/Los_Angeles",
                    },
                }
            ]
        ),
        mutation_types=[],
        entry_types=[],
    )
    assert counts == {"added": 1, "removed": 0, "modified": 0}
    values = {tuple(row["field_path"]): row["after"] for row in rows}
    assert values[("title",)] == "Career fair"
    assert values[("timing", "start_local")] == "2026-09-16T10:00:00"
    assert values[("lane_ref",)] == "lane_meetings"
    assert all(not row["before_present"] for row in rows)


def test_large_detail_has_lossless_bounded_unicode_fragments_and_exact_offsets():
    detail = {"field_path": ["description"], "before": 'é🙂"\n' * 3_000, "after": "corrected"}
    encoded = json.dumps(detail, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    rows = bounded_details([detail, {"small": True}])
    assert rows[-1] == {"small": True}
    restored = bytearray()
    for row in rows[:-1]:
        fragment = row["detail_fragment"]
        assert fragment["byte_offset"] == len(restored)
        assert fragment["total_bytes"] == len(encoded)
        assert fragment["sha256"] == hashlib.sha256(encoded).hexdigest()
        assert len(json.dumps(row, ensure_ascii=False).encode()) < 5_000
        restored.extend(fragment["text"].encode())
    assert restored == encoded
    assert json.loads(restored) == detail
    assert bounded_sample([detail]) == []


def test_unchanged_compiler_bookkeeping_does_not_create_a_semantic_diff():
    original = {
        "import_entry_id": "one",
        "entry_type": "scheduled_occurrence_entry",
        "title": "Quiz",
        "compiler_version": 1,
        "compilation_errors": [{"code": "old_error"}],
    }
    updated = {**original, "compiler_version": 2, "compilation_errors": []}
    rows, counts = draft_diff(
        _revision(entries=[original]),
        _revision(entries=[updated]),
        mutation_types=[],
        entry_types=[],
    )
    assert rows == []
    assert counts == {"added": 0, "removed": 0, "modified": 0}
