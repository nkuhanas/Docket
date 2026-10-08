import pytest

from docket.agent_auth import authenticate_agent
from docket.config import get_settings
from docket.domain.errors import DocketError


def test_distinct_credentials_enforce_profile_and_shared_credentials_fail(tmp_path):
    interactive = tmp_path / "interactive"
    triage = tmp_path / "triage"
    interactive.write_text("synthetic-interactive-secret")
    triage.write_text("synthetic-triage-secret")
    settings = get_settings().model_copy(
        update={
            "docket_to_hermes_token_file": interactive,
            "docket_triage_token_file": triage,
        }
    )
    assert (
        authenticate_agent(
            "synthetic-interactive-secret", role="interactive", settings=settings
        ).role
        == "interactive"
    )
    assert (
        authenticate_agent("synthetic-triage-secret", role="triage", settings=settings).role
        == "triage"
    )
    with pytest.raises(DocketError):
        authenticate_agent("synthetic-triage-secret", role="interactive", settings=settings)
    triage.write_text("synthetic-interactive-secret")
    with pytest.raises(DocketError):
        authenticate_agent("synthetic-interactive-secret", role="interactive", settings=settings)
