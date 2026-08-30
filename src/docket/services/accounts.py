from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import Settings
from docket.domain.errors import DocketError
from docket.models import ProviderAccount

_GOOGLE_CAPABILITIES = ["gmail", "google_calendar"]


class AccountService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def ensure_configured_google(self, settings: Settings) -> ProviderAccount:
        account = self.session.scalar(
            select(ProviderAccount).where(
                ProviderAccount.provider == "google",
                ProviderAccount.external_account_id == settings.google_account_external_id,
            )
        )
        if account is None:
            account = ProviderAccount(
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

    def list_enabled_google(self) -> list[ProviderAccount]:
        return list(
            self.session.scalars(
                select(ProviderAccount)
                .where(ProviderAccount.provider == "google", ProviderAccount.enabled.is_(True))
                .order_by(ProviderAccount.created_at)
            )
        )

    def require_google_ref(self, account_ref: str) -> ProviderAccount:
        account = self.session.scalar(
            select(ProviderAccount).where(
                ProviderAccount.ref_id == account_ref,
                ProviderAccount.provider == "google",
                ProviderAccount.enabled.is_(True),
            )
        )
        if account is None:
            raise DocketError(
                code="calendar_account_not_available",
                message="The selected Google provider-account reference is not enabled.",
                details={"account_ref": account_ref},
            )
        return account
