"""Authenticated event subscriptions; callback verification occurs outside transactions."""

import hashlib
import hmac
import http.client
import json
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from docket.agent_auth import AgentPrincipal, require_principal
from docket.config import Settings
from docket.domain.errors import DocketError
from docket.domain.public_refs import deterministic_public_ref
from docket.models import AuditEvent, MCPEventSubscription, OAuthPrincipal
from docket.models.base import utc_now
from docket.providers.webhooks import (
    PinnedWebhookPoster,
    WebhookPoster,
    signed_headers,
    signing_key,
    validate_callback_url,
)
from docket.services.continuity import ContinuityService
from docket.services.notifications import EVENTS, aware, identity_lock


def subscription_identity(
    principal_ref: str, name: str, arguments: dict[str, str], url: str
) -> tuple[str, str]:
    raw = json.dumps(
        [principal_ref, name, arguments, url], sort_keys=True, separators=(",", ":")
    ).encode()
    digest = hashlib.sha256(raw).digest()
    return deterministic_public_ref("sub", digest), digest.hex()


class EventSubscriptionService:
    def __init__(
        self,
        factory: sessionmaker[Session],
        settings: Settings,
        poster: WebhookPoster | None = None,
    ) -> None:
        self.factory, self.settings = factory, settings
        self.poster = poster or PinnedWebhookPoster()

    def _cipher(self) -> AESGCM:
        key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=b"docket-mcp-events-v1"
        ).derive(self.settings.attachment_encryption_key())
        return AESGCM(key)

    def decrypt(self, row: MCPEventSubscription) -> dict[str, Any]:
        value = self._cipher().decrypt(row.nonce, row.configuration, row.ref_id.encode())
        result: dict[str, Any] = json.loads(value)
        return result

    def encrypt(self, row: MCPEventSubscription, configuration: dict[str, Any]) -> None:
        row.nonce = secrets.token_bytes(12)
        raw = json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
        row.configuration = self._cipher().encrypt(row.nonce, raw, row.ref_id.encode())

    def owner(self, session: Session, principal: AgentPrincipal) -> OAuthPrincipal:
        principal.require("events")
        row = session.scalar(
            select(OAuthPrincipal)
            .where(
                OAuthPrincipal.ref_id == principal.principal_ref,
            )
            .with_for_update()
        )
        if (
            principal.integration != "oauth"
            or row is None
            or not row.enabled
            or row.operator_ref != principal.operator_ref
            or row.valid_after != principal.grant_epoch
            or self.settings.plugin_events_scope not in row.scopes
            or self.settings.plugin_read_scope not in row.scopes
        ):
            raise DocketError(code="agent_authority_denied", message="Subscription access denied.")
        return row

    @staticmethod
    def stop(row: MCPEventSubscription, status: str = "stopped") -> None:
        row.status = status
        row.configuration, row.nonce = b"", b""
        row.version += 1

    def subscribe(
        self,
        *,
        name: str,
        arguments: dict[str, str],
        url: str,
        secret: str,
        ttl_ms: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        principal = require_principal("events")
        if name not in EVENTS or cursor is not None or set(arguments) - {"subject_ref"}:
            raise DocketError(code="invalid_subscription", message="Invalid event or filters.")
        try:
            validate_callback_url(url)
            signing_key(secret)
        except (ValueError, UnicodeError) as exc:
            raise DocketError(
                code="invalid_subscription",
                message="Invalid callback settings.",
            ) from exc
        lifetime = min(max(ttl_ms if ttl_ms is not None else 86400000, 60000), 604800000)
        ref, digest = subscription_identity(principal.principal_ref, name, arguments, url)
        with self.factory.begin() as session:
            owner = self.owner(session, principal)
            grant_epoch = owner.valid_after
            existing = session.scalar(
                select(MCPEventSubscription).where(
                    MCPEventSubscription.ref_id == ref,
                )
            )
            cached = False
            if (
                existing is not None
                and existing.status == "active"
                and existing.grant_epoch == grant_epoch
                and aware(existing.verified_at) > utc_now() - timedelta(minutes=5)
            ):
                try:
                    cached = hmac.compare_digest(self.decrypt(existing)["secret"], secret)
                except (InvalidTag, ValueError, KeyError, TypeError):
                    cached = False
            lease = ContinuityService(session).acquire_execution_lease(
                lease_key=f"subscription-verification:{ref}:{uuid.uuid4().hex}",
                lease_kind="outbox_delivery",
                lease_seconds=60,
            )
            completion = lease.completion_token
        try:
            if not cached:
                challenge = secrets.token_urlsafe(32)
                body = json.dumps(
                    {"type": "verification", "challenge": challenge}, separators=(",", ":")
                ).encode()
                response = self.poster.post(
                    url,
                    body,
                    signed_headers(
                        "verification_" + uuid.uuid4().hex,
                        body,
                        secret,
                        ref,
                    ),
                )
                try:
                    echoed = json.loads(response.body).get("challenge")
                    valid = (
                        200 <= response.status < 300
                        and isinstance(echoed, str)
                        and hmac.compare_digest(challenge, echoed)
                    )
                except (ValueError, AttributeError, TypeError):
                    valid = False
                if not valid:
                    raise DocketError(
                        code="callback_verification_failed",
                        message="Callback challenge verification failed.",
                    )
            with self.factory.begin() as session:
                self.owner(session, principal)
                identity_lock(session, "subscription-quota:" + principal.operator_ref)
                identity_lock(session, "subscription:" + digest)
                row = session.scalar(
                    select(MCPEventSubscription)
                    .where(
                        MCPEventSubscription.ref_id == ref,
                    )
                    .with_for_update()
                )
                now = utc_now()
                configuration: dict[str, Any] = {"url": url, "secret": secret}
                if row is None:
                    count = (
                        session.scalar(
                            select(func.count())
                            .select_from(MCPEventSubscription)
                            .where(MCPEventSubscription.status == "active")
                        )
                        or 0
                    )
                    if count >= 64:
                        raise DocketError(
                            code="subscription_limit",
                            message="Subscription limit reached.",
                        )
                    row = MCPEventSubscription(
                        ref_id=ref,
                        identity_hash=digest,
                        principal_ref=principal.principal_ref,
                        grant_epoch=grant_epoch,
                        event_name=name,
                        arguments=arguments,
                        status="active",
                        expires_at=now + timedelta(milliseconds=lifetime),
                        verified_at=now,
                        configuration=b"",
                        nonce=b"",
                    )
                    session.add(row)
                else:
                    if row.status == "active" and row.configuration:
                        try:
                            previous = self.decrypt(row)
                        except (InvalidTag, ValueError, KeyError, TypeError):
                            previous = {}
                        if previous.get("secret") and previous["secret"] != secret:
                            configuration.update(
                                previous_secret=previous["secret"],
                                previous_until=(now + timedelta(seconds=60)).isoformat(),
                            )
                        elif (previous.get("previous_secret") and
                              datetime.fromisoformat(previous["previous_until"]) > now):
                            configuration.update(
                                previous_secret=previous["previous_secret"],
                                previous_until=previous["previous_until"],
                            )
                    row.version += 1
                    row.status, row.grant_epoch = "active", grant_epoch
                    row.expires_at = now + timedelta(milliseconds=lifetime)
                    if not cached:
                        row.verified_at = now
                self.encrypt(row, configuration)
                session.flush()
                self._audit(session, row, "activated")
                return {
                    "id": ref,
                    "refreshBefore": aware(row.expires_at).isoformat(),
                    "cursor": None,
                    "truncated": False,
                }
        except (OSError, ValueError, http.client.HTTPException, InvalidTag) as exc:
            raise DocketError(
                code="callback_verification_failed", message="Callback verification is unavailable."
            ) from exc
        finally:
            with self.factory.begin() as session:
                ContinuityService(session).complete_execution_lease(completion)

    def unsubscribe(self, *, name: str, arguments: dict[str, str], url: str) -> dict[str, Any]:
        principal = require_principal("events")
        ref, _ = subscription_identity(principal.principal_ref, name, arguments, url)
        with self.factory.begin() as session:
            self.owner(session, principal)
            row = session.scalar(
                select(MCPEventSubscription)
                .where(
                    MCPEventSubscription.ref_id == ref,
                )
                .with_for_update()
            )
            if row is not None and row.status != "stopped":
                self.stop(row)
                self._audit(session, row, "stopped")
        return {}

    def _audit(self, session: Session, row: MCPEventSubscription, action: str) -> None:
        session.add(
            AuditEvent(
                event_type=f"plugin.subscription.{action}",
                actor_type="agent",
                actor_id=row.principal_ref,
                primary_ref=row.ref_id,
                affected_refs=[row.ref_id],
                basis_refs=[],
                data={"event_name": row.event_name, "version": row.version},
            )
        )
