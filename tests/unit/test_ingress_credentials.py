from docket.ingress_credentials import _APPEND_TABLES, _READ_TABLES
from docket.models import Base


def test_ingress_role_inventory_matches_clean_schema() -> None:
    table_names = set(Base.metadata.tables)

    assert set(_READ_TABLES) <= table_names
    assert set(_APPEND_TABLES) <= set(_READ_TABLES)
    assert "operator_projections" in _READ_TABLES
    assert "semantic_prompt_projections" not in _READ_TABLES
