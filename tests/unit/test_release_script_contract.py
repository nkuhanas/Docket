from pathlib import Path


def test_calendar_auth_recovery_has_an_explicit_execution_boundary() -> None:
    script = Path("scripts/docket").read_text(encoding="utf-8")

    assert "calendar-recover-auth status CHANGESET_REF" in script
    assert "calendar-recover-auth requeue-auth-failures CHANGESET_REF --execute" in script
    assert "docket-calendar-recovery" in script



def test_google_oauth_wrapper_auto_selects_remote_mode_without_a_browser() -> None:
    script = Path("scripts/setup-google-oauth.sh").read_text(encoding="utf-8")

    assert "DISPLAY" in script
    assert "WAYLAND_DISPLAY" in script
    assert "BROWSER" in script
    assert 'set -- --remote "$@"' in script
    assert "No SSH tunnel is required" in script
