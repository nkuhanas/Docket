import hmac

from fastapi import Header, HTTPException, status

from docket.config import get_settings


def require_hermes_service(authorization: str | None = Header(default=None)) -> None:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing internal service authorization",
        )
    supplied = authorization.removeprefix("Bearer ").strip()
    expected = get_settings().hermes_to_docket_token()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal service authorization",
        )
