from docket.providers.google.fake_gmail import FakeGmailProvider
from docket.providers.google.gmail import GmailReadProvider

_gmail_read_provider: GmailReadProvider = FakeGmailProvider()


def configure_gmail_read_provider(provider: GmailReadProvider) -> None:
    global _gmail_read_provider
    _gmail_read_provider = provider


def get_gmail_read_provider() -> GmailReadProvider:
    return _gmail_read_provider
