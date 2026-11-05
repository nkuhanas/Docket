"""Bounded OAuth verification and explicit, durable single-Operator enrollment."""

import json
import math
import threading
import time
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from docket.agent_auth import AgentPrincipal
from docket.config import Settings
from docket.domain.errors import DocketError
from docket.models import AuditEvent, OAuthPrincipal

OAuthFailureReason = Literal[
    "credential_missing", "unsupported_token", "signing_keys_unavailable",
    "signing_key_unknown", "missing_claim", "invalid_signature", "invalid_issuer",
    "invalid_audience", "expired_token", "invalid_token_time", "invalid_claims",
    "client_not_allowed", "client_identity_missing", "client_identity_conflict",
    "non_user_grant", "principal_not_approved",
    "principal_disabled", "principal_stale", "operator_mismatch", "read_scope_missing",
]
_REQUIRED_CLAIMS = ("iss", "aud", "sub", "exp", "iat", "scope")
_KNOWN_CLAIMS = (*_REQUIRED_CLAIMS, "azp", "client_id")


class OAuthAuthenticationError(DocketError):
    """A generic public error with separate, value-free operational diagnostics."""

    def __init__(self, reason: OAuthFailureReason, *, missing_claim: str | None = None) -> None:
        super().__init__(
            code="plugin_authentication_required", message="Link an approved Docket account."
        )
        self.diagnostic: dict[str, str] = {"reason": reason}
        if missing_claim is not None:
            self.diagnostic["missing_claim"] = (
                missing_claim if missing_claim in _KNOWN_CLAIMS else "other"
            )


class OAuthVerifier:
    """Never follow token-supplied key URLs, or persist/log token payloads."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.Client(timeout=10, follow_redirects=False, trust_env=False)
        self.lock = threading.Lock()
        self.keys: dict[str, jwt.PyJWK] = {}
        self.expires = 0.0
        self.next_fetch = 0.0

    def _key(self, kid: str) -> jwt.PyJWK:
        with self.lock:
            now = time.monotonic()
            if (kid not in self.keys or now >= self.expires) and now >= self.next_fetch:
                self.next_fetch = now + 10
                try:
                    if not self.settings.plugin_oauth_jwks_url:
                        raise ValueError("Issuer is not configured")
                    with self.client.stream("GET", self.settings.plugin_oauth_jwks_url) as response:
                        response.raise_for_status()
                        data = bytearray()
                        for part in response.iter_bytes():
                            data.extend(part)
                            if len(data) > 65536:
                                raise ValueError("JWKS exceeds limit")
                    document = json.loads(data)
                    if not isinstance(document, dict):
                        raise ValueError("Invalid JWKS document")
                    entries = document.get("keys")
                    if not isinstance(entries, list) or not 1 <= len(entries) <= 32:
                        raise ValueError("Invalid JWKS")
                    keys: dict[str, jwt.PyJWK] = {}
                    for entry in entries:
                        if not isinstance(entry, dict) or entry.get("use", "sig") != "sig":
                            continue
                        key_id = entry.get("kid")
                        if not isinstance(key_id, str) or not 1 <= len(key_id) <= 256:
                            continue
                        if key_id in keys:
                            raise ValueError("Duplicate signing key")
                        key = jwt.PyJWK.from_dict(entry)
                        if key.algorithm_name == "RS256":
                            keys[key_id] = key
                    self.keys, self.expires = keys, now + 300
                except (httpx.HTTPError, ValueError, TypeError, KeyError, jwt.PyJWTError) as exc:
                    raise OAuthAuthenticationError("signing_keys_unavailable") from exc
            selected = self.keys.get(kid)
            if selected is None or now >= self.expires:
                raise OAuthAuthenticationError("signing_key_unknown")
            return selected

    def verify(self, token: str) -> dict[str, Any]:
        try:
            if not self.settings.plugin_enabled or not 1 <= len(token) <= 16384:
                raise OAuthAuthenticationError("unsupported_token")
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if (
                not isinstance(kid, str) or not 1 <= len(kid) <= 256
                or header.get("alg") != "RS256" or "jku" in header or "x5u" in header
            ):
                raise OAuthAuthenticationError("unsupported_token")
            claims: dict[str, Any] = jwt.decode(
                token, self._key(kid).key, algorithms=["RS256"],
                issuer=self.settings.plugin_oauth_issuer,
                audience=self.settings.plugin_public_origin.rstrip("/"),
                options={"require": list(_REQUIRED_CLAIMS)},
            )
            if (
                not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"]) <= 512
                or not isinstance(claims["scope"], str)
                or any(type(claims[k]) not in (int, float) or not math.isfinite(claims[k])
                       for k in ("iat", "exp"))
                or claims["exp"] <= claims["iat"]
            ):
                raise ValueError("Invalid delegated grant")
            # Auth0's native profile uses azp; RFC 9068 uses client_id. Both
            # values, when present, are signed and must identify the same client.
            clients = [claims[name] for name in ("azp", "client_id") if name in claims]
            if not clients:
                raise OAuthAuthenticationError("client_identity_missing")
            if any(not isinstance(value, str) or not 1 <= len(value) <= 256 for value in clients):
                raise OAuthAuthenticationError("invalid_claims")
            if any(value != clients[0] for value in clients[1:]):
                raise OAuthAuthenticationError("client_identity_conflict")
            if clients[0] not in self.settings.plugin_oauth_client_ids:
                raise OAuthAuthenticationError("client_not_allowed")
            if claims.get("gty") == "client-credentials" or claims["sub"].endswith("@clients"):
                raise OAuthAuthenticationError("non_user_grant")
            return claims
        except jwt.MissingRequiredClaimError as exc:
            raise OAuthAuthenticationError("missing_claim", missing_claim=exc.claim) from exc
        except jwt.InvalidSignatureError as exc:
            raise OAuthAuthenticationError("invalid_signature") from exc
        except jwt.InvalidIssuerError as exc:
            raise OAuthAuthenticationError("invalid_issuer") from exc
        except jwt.InvalidAudienceError as exc:
            raise OAuthAuthenticationError("invalid_audience") from exc
        except jwt.ExpiredSignatureError as exc:
            raise OAuthAuthenticationError("expired_token") from exc
        except (jwt.ImmatureSignatureError, jwt.InvalidIssuedAtError) as exc:
            raise OAuthAuthenticationError("invalid_token_time") from exc
        except (ValueError, TypeError, KeyError, OverflowError, jwt.PyJWTError) as exc:
            raise OAuthAuthenticationError("invalid_claims") from exc

    def authenticate(self, token: str, factory: sessionmaker[Session]) -> AgentPrincipal:
        claims = self.verify(token)
        with factory() as session:
            row = session.scalar(select(OAuthPrincipal).where(
                OAuthPrincipal.issuer == claims["iss"], OAuthPrincipal.subject == claims["sub"],
            ))
            if row is None:
                raise OAuthAuthenticationError("principal_not_approved")
            if not row.enabled:
                raise OAuthAuthenticationError("principal_disabled")
            if claims["iat"] < row.valid_after:
                raise OAuthAuthenticationError("principal_stale")
            if row.operator_ref != self.settings.canonical_operator_ref:
                raise OAuthAuthenticationError("operator_mismatch")
            scopes = set(row.scopes) & set(claims["scope"].split())
            if self.settings.plugin_read_scope not in scopes:
                raise OAuthAuthenticationError("read_scope_missing")
            permissions = {"read"}
            if self.settings.plugin_events_scope in scopes:
                permissions.add("events")
            if self.settings.plugin_triage_scope in scopes:
                permissions.add("triage")
            # A connected OAuth account proves no foreground task identity.
            return AgentPrincipal(
                principal_ref=row.ref_id, operator_ref=row.operator_ref,
                role="triage" if "triage" in permissions else "read_only",
                permissions=frozenset(permissions), integration="oauth",
                expires_at=datetime.fromtimestamp(claims["exp"], UTC),
                grant_epoch=row.valid_after,
            )


class OAuthPrincipalService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session, self.settings = session, settings

    def approve(
        self, subject: str, *, events: bool = False, triage: bool = False,
    ) -> OAuthPrincipal:
        issuer = self.settings.plugin_oauth_issuer
        if not issuer or not subject.strip() or len(subject) > 512 or subject.endswith("@clients"):
            raise ValueError("A configured issuer and user subject are required")
        row = self.session.scalar(select(OAuthPrincipal).where(
            OAuthPrincipal.issuer == issuer, OAuthPrincipal.subject == subject,
        ).with_for_update())
        scopes = [self.settings.plugin_read_scope]
        if events:
            scopes.append(self.settings.plugin_events_scope)
        if triage:
            scopes.append(self.settings.plugin_triage_scope)
        if row is None:
            row = OAuthPrincipal(
                issuer=issuer, subject=subject, operator_ref=self.settings.canonical_operator_ref,
                scopes=scopes,
            )
            self.session.add(row)
        else:
            if row.operator_ref != self.settings.canonical_operator_ref:
                raise ValueError("An existing principal cannot be reassigned")
            row.enabled, row.scopes = True, scopes
            row.valid_after = int(time.time()) + 1
        self.session.flush()
        self._audit(row, "approved")
        return row

    def revoke(self, ref: str) -> OAuthPrincipal:
        row = self.session.scalar(select(OAuthPrincipal).where(
            OAuthPrincipal.ref_id == ref,
            OAuthPrincipal.operator_ref == self.settings.canonical_operator_ref,
        ).with_for_update())
        if row is None:
            raise ValueError("Approved principal not found")
        row.enabled = False
        self._audit(row, "revoked")
        return row

    def _audit(self, row: OAuthPrincipal, disposition: str) -> None:
        self.session.add(AuditEvent(
            event_type=f"oauth.principal.{disposition}", actor_type="operator",
            actor_id=self.settings.canonical_operator_ref, primary_ref=row.ref_id,
            affected_refs=[row.ref_id], basis_refs=[],
            data={"permissions": row.scopes, "valid_after": row.valid_after},
        ))
