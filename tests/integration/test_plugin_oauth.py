import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select

from docket.agent_auth import AgentCallContext, agent_call_context
from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.models import AuditEvent, OAuthPrincipal
from docket.services.oauth import OAuthPrincipalService, OAuthVerifier


@pytest.fixture
def oauth(session_factory):
    settings = get_settings().model_copy(update={
        "plugin_enabled": True,
        "plugin_oauth_issuer": "https://identity.example.test/",
        "plugin_oauth_jwks_url": "https://identity.example.test/.well-known/jwks.json",
        "plugin_oauth_client_ids": ["docket-client"],
    })
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="signing-key", alg="RS256", use="sig")
    requests = []

    def keys(request):
        requests.append(request)
        return httpx.Response(200, json={"keys": [jwk]})

    verifier = OAuthVerifier(settings, httpx.Client(transport=httpx.MockTransport(keys)))

    def token(**changes):
        now = int(time.time())
        claims = {
            "iss": settings.plugin_oauth_issuer, "aud": settings.plugin_public_origin,
            "sub": "user-owner", "azp": "docket-client", "iat": now, "exp": now + 3600,
            "scope": "docket.read docket.events write commit",
        }
        claims.update(changes)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "signing-key"})

    return settings, verifier, token, requests


def test_login_requires_explicit_owner_and_never_grants_foreground(session_factory, oauth):
    settings, verifier, token, requests = oauth
    with pytest.raises(DocketError):
        verifier.authenticate(token(), session_factory)
    with session_factory.begin() as session:
        ref = OAuthPrincipalService(session, settings).approve("user-owner", events=True).ref_id
    principal = verifier.authenticate(token(), session_factory)
    assert principal.principal_ref == ref
    assert principal.role == "read_only"
    assert principal.permissions == {"read", "events"}
    assert len(requests) == 1
    for permission in ("stage", "commit", "resolve_conflict", "triage"):
        with pytest.raises(DocketError) as denied:
            principal.require(permission)
        assert denied.value.code == "agent_authority_denied"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1
        assert session.scalar(select(func.count()).select_from(OAuthPrincipal)) == 1


def test_triage_requires_local_approval_and_token_scope_and_never_grants_foreground(
    session_factory, oauth,
):
    settings, verifier, token, _ = oauth
    with session_factory.begin() as session:
        OAuthPrincipalService(session, settings).approve("user-owner", events=True, triage=True)
    assert verifier.authenticate(token(), session_factory).role == "read_only"
    principal = verifier.authenticate(token(scope="docket.read docket.triage"), session_factory)
    assert principal.role == "triage" and principal.permissions == {"read", "triage"}
    for permission in ("stage", "commit", "resolve_conflict", "events"):
        with pytest.raises(DocketError):
            principal.require(permission)
    with session_factory.begin() as session:
        session.scalar(select(OAuthPrincipal)).scopes = [settings.plugin_read_scope]
    assert "triage" not in verifier.authenticate(token(
        scope="docket.read docket.triage",
    ), session_factory).permissions


@pytest.mark.parametrize("claims", [
    {"iss": "https://wrong.example.test/"}, {"aud": "https://aphelion.host"},
    {"sub": "other-user"}, {"azp": "other-client"}, {"exp": 1},
    {"iat": 9999999999}, {"scope": "write"}, {"gty": "client-credentials"},
    {"sub": "machine@clients"}, {"scope": ["docket.read"]},
    {"iat": True},
])
def test_invalid_grants_fail_closed(session_factory, oauth, claims):
    settings, verifier, token, _ = oauth
    with session_factory.begin() as session:
        OAuthPrincipalService(session, settings).approve("user-owner")
    with pytest.raises(DocketError):
        verifier.authenticate(token(**claims), session_factory)


def test_revoke_and_reapprove_preserves_identity_without_reviving_tokens(session_factory, oauth):
    settings, verifier, token, _ = oauth
    with session_factory.begin() as session:
        ref = OAuthPrincipalService(session, settings).approve("user-owner", events=True).ref_id
    old = token()
    verifier.authenticate(old, session_factory)
    with session_factory.begin() as session:
        OAuthPrincipalService(session, settings).revoke(ref)
    with pytest.raises(DocketError):
        verifier.authenticate(old, session_factory)
    with session_factory.begin() as session:
        row = OAuthPrincipalService(session, settings).approve("user-owner")
        assert row.ref_id == ref
    with pytest.raises(DocketError):
        verifier.authenticate(old, session_factory)


def test_issuer_key_fetch_is_bounded_and_never_uses_token_key_urls(oauth):
    settings, verifier, token, requests = oauth
    verifier.verify(token())
    for _ in range(5):
        with pytest.raises(DocketError):
            verifier.verify("malformed")
    assert len(requests) == 1
    assert str(requests[0].url) == settings.plugin_oauth_jwks_url


def test_wrong_algorithm_and_malicious_key_url_are_rejected(oauth):
    _, verifier, token, requests = oauth
    claims = jwt.decode(token(), options={"verify_signature": False})
    malicious = jwt.encode(claims, "synthetic-secret-" * 4, algorithm="HS256", headers={
        "kid": "signing-key", "jku": "http://127.0.0.1/private",
    })
    with pytest.raises(DocketError):
        verifier.verify(malicious)
    assert requests == []


def test_unknown_keys_are_throttled(oauth):
    _, verifier, token, requests = oauth
    verifier.verify(token())
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = jwt.decode(token(), options={"verify_signature": False})
    for index in range(5):
        malicious = jwt.encode(claims, key, algorithm="RS256", headers={"kid": f"other-{index}"})
        with pytest.raises(DocketError):
            verifier.verify(malicious)
    assert len(requests) == 1


def test_principal_identity_is_immutable(session_factory, oauth):
    settings, _, _, _ = oauth
    with session_factory.begin() as session:
        ref = OAuthPrincipalService(session, settings).approve("user-owner").ref_id
    with pytest.raises(ValueError, match="immutable"), session_factory.begin() as session:
        row = session.scalar(select(OAuthPrincipal).where(OAuthPrincipal.ref_id == ref))
        row.subject = "different-user"
    with pytest.raises(ValueError, match="retained"), session_factory.begin() as session:
        row = session.scalar(select(OAuthPrincipal).where(OAuthPrincipal.ref_id == ref))
        session.delete(row)


def test_plugin_principal_cannot_admit_a_request(session_factory, oauth):
    from docket.services.agent_requests import AgentRequestService

    settings, verifier, token, _ = oauth
    with session_factory.begin() as session:
        OAuthPrincipalService(session, settings).approve("user-owner")
    principal = verifier.authenticate(token(), session_factory)
    binding = agent_call_context.set(AgentCallContext(principal, "copied-key", "task", "op"))
    try:
        with pytest.raises(DocketError), session_factory.begin() as session:
            AgentRequestService(session).admit(request_key="copied-key")
    finally:
        agent_call_context.reset(binding)
