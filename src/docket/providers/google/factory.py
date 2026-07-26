from docket.config import Settings
from docket.providers.google.calendar import (
    CalendarProvider,
    CalendarReadProvider,
    GoogleCalendarProvider,
)
from docket.providers.google.disabled_calendar import DisabledCalendarProvider
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.providers.google.fake_gmail import FakeGmailProvider
from docket.providers.google.gmail import (
    GmailMutationProvider,
    GmailReadProvider,
    GoogleGmailProvider,
)


def build_calendar_write_provider(settings: Settings) -> CalendarProvider:
    mode = settings.calendar_write_mode()
    if mode == "google":
        return GoogleCalendarProvider(str(settings.google_oauth_token_file))
    if mode == "fake":
        return FakeCalendarProvider()
    return DisabledCalendarProvider()


def build_calendar_read_provider(settings: Settings) -> CalendarReadProvider:
    if settings.calendar_reads_enabled:
        return GoogleCalendarProvider(str(settings.google_oauth_token_file))
    return FakeCalendarProvider()


def build_gmail_read_provider(settings: Settings) -> GmailReadProvider | None:
    mode = settings.gmail_provider_mode()
    if mode == "google":
        return GoogleGmailProvider(str(settings.google_oauth_token_file))
    if mode == "fake":
        return FakeGmailProvider()
    return None


def build_gmail_mutation_provider(settings: Settings) -> GmailMutationProvider | None:
    if not settings.gmail_writes_enabled:
        return None
    if settings.environment.value == "production":
        return GoogleGmailProvider(str(settings.google_oauth_token_file))
    return FakeGmailProvider()
