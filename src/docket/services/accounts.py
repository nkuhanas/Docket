from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import Settings
from docket.models import Account

_GOOGLE_CAPABILITIES = ["gmail", "google_calendar"]


class AccountService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def ensure_configured_google(self, settings: Settings) -> Account:
        account = self.session.scalar(
            select(Account).where(
                Account.provider == "google",
                Account.external_account_id == settings.google_account_external_id,
            )
        )
        if account is None:
            account = Account(
                provider="google",
                external_account_id=settings.google_account_external_id,
                display_name="Configured Google account",
                capabilities=_GOOGLE_CAPABILITIES,
                credential_ref=str(settings.google_oauth_token_file),
                enabled=True,
            )
            self.session.add(account)
            self.session.flush()
        else:
            account.capabilities = _GOOGLE_CAPABILITIES
            account.credential_ref = str(settings.google_oauth_token_file)
        return account

    def list_enabled_google(self) -> list[Account]:
        return list(
            self.session.scalars(
                select(Account)
                .where(Account.provider == "google", Account.enabled.is_(True))
                .order_by(Account.created_at)
            )
        )
