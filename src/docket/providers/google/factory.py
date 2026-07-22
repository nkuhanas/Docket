from docket.config import Settings
from docket.providers.google.calendar import (
    CalendarProvider,
    CalendarReadProvider,
    GoogleCalendarProvider,
)
from docket.providers.google.fake_calendar import FakeCalendarProvider


def build_calendar_write_provider(settings: Settings) -> CalendarProvider:
    if settings.external_writes_enabled:
        return GoogleCalendarProvider(str(settings.google_oauth_token_file))
    return FakeCalendarProvider()


def build_calendar_read_provider(settings: Settings) -> CalendarReadProvider:
    if settings.calendar_reads_enabled:
        return GoogleCalendarProvider(str(settings.google_oauth_token_file))
    return FakeCalendarProvider()
