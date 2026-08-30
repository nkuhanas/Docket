from __future__ import annotations

from datetime import date, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from docket.domain.public_refs import (
    PUBLIC_REF_PREFIX_BY_TYPE,
    PUBLIC_REF_PREFIXES,
    PUBLIC_REF_TYPES,
    new_public_ref,
    parse_public_ref,
    prefix_for_type,
    public_ref_type,
)
from docket.models import BriefEntry, CaseItem, Conflict, ProviderAccount, SemanticRequestAttempt
from docket.schemas.tracked_context import (
    DateTemporalValue,
    ItemInput,
    TaskInput,
    TemporalBindingInput,
    TemporalValue,
)


def _ref(prefix: str) -> str:
    return new_public_ref(prefix)


def test_clean_public_reference_registry_has_one_meaning_per_prefix() -> None:
    assert len(PUBLIC_REF_TYPES) == 44
    assert len(PUBLIC_REF_PREFIX_BY_TYPE) == 44
    assert frozenset(PUBLIC_REF_TYPES) == PUBLIC_REF_PREFIXES
    assert prefix_for_type("Item") == "item"
    assert prefix_for_type("CaseItem") == "citem"
    assert prefix_for_type("BriefEntry") == "bentry"
    assert prefix_for_type("ProviderAccount") == "acct"
    assert prefix_for_type("Conflict") == "conf"
    assert prefix_for_type("SemanticRequestAttempt") == "sattempt"
    assert public_ref_type(_ref("time")) == "TemporalBinding"


@pytest.mark.parametrize("prefix", ["itm", "cnf", "satt", "lease", "dproj", "prompt"])
def test_discarded_public_reference_prefixes_are_unknown(prefix: str) -> None:
    with pytest.raises(ValueError, match="Unsupported public-reference prefix"):
        new_public_ref(prefix)
    with pytest.raises(ValueError, match="Invalid Docket public reference"):
        parse_public_ref(f"{prefix}_01M18DYEYJVVJ7TW5VQQBCA6NC")


def test_existing_model_generators_emit_clean_reassigned_prefixes() -> None:
    assert ProviderAccount.__table__.c.ref_id.default.arg(None).startswith("acct_")
    assert CaseItem.__table__.c.ref_id.default.arg(None).startswith("citem_")
    assert BriefEntry.__table__.c.ref_id.default.arg(None).startswith("bentry_")
    assert Conflict.__table__.c.ref_id.default.arg(None).startswith("conf_")
    assert SemanticRequestAttempt.__table__.c.ref_id.default.arg(None).startswith("sattempt_")


def test_item_kind_is_descriptive_and_metadata_is_byte_bounded() -> None:
    item = ItemInput(
        title="Midterm Exam",
        kind="academic.exam",
        context_entity_refs=[_ref("ent")],
        metadata_json={"source_cell": "week-4-friday"},
        source_refs=[_ref("src")],
    )
    assert item.kind == "academic.exam"

    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        ItemInput(title="Midterm", kind="Academic Exam")
    with pytest.raises(ValidationError, match="16 KiB"):
        ItemInput(title="Midterm", metadata_json={"value": "é" * 9000})


def test_date_precision_remains_date_precision() -> None:
    binding = TemporalBindingInput(
        subject_ref=_ref("item"),
        role="due_by",
        temporal_value={
            "kind": "date",
            "date": date(2026, 10, 3),
            "timezone": "America/Los_Angeles",
        },
    )
    assert isinstance(binding.temporal_value, DateTemporalValue)
    assert binding.temporal_value.date == date(2026, 10, 3)
    assert "local_datetime" not in binding.temporal_value.model_dump()


def test_temporal_role_requires_the_correct_value_shape() -> None:
    with pytest.raises(ValidationError, match="window requires an interval"):
        TemporalBindingInput(
            subject_ref=_ref("item"),
            role="window",
            temporal_value={
                "kind": "date",
                "date": "2026-09-01",
                "timezone": "America/Los_Angeles",
            },
        )
    with pytest.raises(ValidationError, match="point temporal roles"):
        TemporalBindingInput(
            subject_ref=_ref("item"),
            role="expected_at",
            temporal_value={
                "kind": "date_interval",
                "start_date": "2026-09-01",
                "end_date": "2026-09-30",
                "end_inclusive": True,
                "timezone": "America/Los_Angeles",
            },
        )


def test_datetime_rejects_nonexistent_and_unresolved_ambiguous_local_time() -> None:
    adapter = TypeAdapter(TemporalValue)
    with pytest.raises(ValidationError, match="nonexistent daylight-saving"):
        adapter.validate_python(
            {
                "kind": "datetime",
                "local_datetime": datetime(2026, 3, 8, 2, 30),
                "timezone": "America/Los_Angeles",
            }
        )
    with pytest.raises(ValidationError, match="ambiguous daylight-saving"):
        adapter.validate_python(
            {
                "kind": "datetime",
                "local_datetime": datetime(2026, 11, 1, 1, 30),
                "timezone": "America/Los_Angeles",
            }
        )


def test_task_requires_exactly_one_item_and_exact_completion_time() -> None:
    with pytest.raises(ValidationError, match="one item ref or change id"):
        TaskInput(title="Complete Problem Set 4")
    with pytest.raises(ValidationError, match="completed_at"):
        TaskInput(item_ref=_ref("item"), title="Complete Problem Set 4", task_state="completed")
    task = TaskInput(
        item_change_id="create-problem-set-4",
        title="Complete Problem Set 4",
        task_state="completed",
        completed_at=datetime(2026, 9, 25, 18, 30),
    )
    assert task.item_change_id == "create-problem-set-4"
