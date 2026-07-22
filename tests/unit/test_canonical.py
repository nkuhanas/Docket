import uuid

import pytest

from docket.domain.canonical import canonical_record_key, normalize_identity, sha256_json


def test_term_identity_is_stable() -> None:
    assert canonical_record_key(
        "term", {"institution": "Cal Poly", "term_name": "Fall 2026"}
    ) == canonical_record_key("term", {"institution": "  CAL--POLY ", "term_name": "Fall   2026"})


def test_course_identity_uses_stable_record_id() -> None:
    term_id = uuid.uuid4()
    assert (
        canonical_record_key(
            "course",
            {"term_record_id": term_id, "course_code": "CSC 101", "section": None},
        )
        == f"course:{term_id}:csc-101:none"
    )


def test_empty_identity_is_rejected() -> None:
    with pytest.raises(ValueError):
        normalize_identity("--")


def test_json_hash_is_order_independent() -> None:
    assert sha256_json({"a": 1, "b": 2}) == sha256_json({"b": 2, "a": 1})
