"""JWT bearer authentication — the hub as an OAuth resource server (Story 5.2).

Per ADR 0001 the hub validates inbound access tokens itself rather than trusting a
fronting proxy. Tokens here are signed with a throwaway key and the JWKS is served
through an httpx MockTransport, so these tests need no IdP and no network.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Request

from mcp_hub.auth import Principal
from mcp_hub.auth.jwt_bearer import JWKSCache, JWTBearerStrategy

ISSUER = "https://idp.example.com/realms/mcp-hub"
AUDIENCE = "k5n-mcp-hub"
JWKS_URI = "https://idp.example.com/realms/mcp-hub/protocol/openid-connect/certs"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    entry = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    entry.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return entry


def _token(
    key: rsa.RSAPrivateKey,
    *,
    kid: str = "key-1",
    subject: str = "alice",
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    expires_in: int = 300,
    not_before_offset: int = 0,
    algorithm: str = "RS256",
    **claims: Any,
) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": subject,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + expires_in,
        "nbf": now + not_before_offset,
        **claims,
    }
    return jwt.encode(payload, key, algorithm=algorithm, headers={"kid": kid})


class JWKSServer:
    """A stand-in JWKS endpoint that counts how often it is fetched."""

    def __init__(self, keys: dict[str, rsa.RSAPrivateKey]) -> None:
        self.keys = keys
        self.fetch_count = 0

    def client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            self.fetch_count += 1
            return httpx.Response(
                200, json={"keys": [_jwk(k, kid) for kid, k in self.keys.items()]}
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def bearer_request(token: str) -> MagicMock:
    request = MagicMock(spec=Request)
    request.headers = {"Authorization": f"Bearer {token}"}
    return request


def strategy_for(server: JWKSServer, **overrides: Any) -> JWTBearerStrategy:
    kwargs: dict[str, Any] = {
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "jwks_uri": JWKS_URI,
        "client": server.client(),
    }
    kwargs.update(overrides)
    return JWTBearerStrategy(**kwargs)


@pytest.fixture
def signing_key() -> rsa.RSAPrivateKey:
    return _rsa_key()


@pytest.fixture
def jwks(signing_key: rsa.RSAPrivateKey) -> JWKSServer:
    return JWKSServer({"key-1": signing_key})


class TestValidTokens:
    @pytest.mark.asyncio
    async def test_valid_token_yields_a_principal(self, signing_key, jwks) -> None:
        result = await strategy_for(jwks).authenticate(bearer_request(_token(signing_key)))

        assert isinstance(result, Principal)
        assert result.subject == "alice"
        assert result.issuer == ISSUER
        assert result.is_anonymous is False

    @pytest.mark.asyncio
    async def test_principal_retains_the_raw_token_for_exchange(self, signing_key, jwks) -> None:
        # Epic 6 sends this verbatim as RFC 8693 `subject_token`.
        token = _token(signing_key)

        result = await strategy_for(jwks).authenticate(bearer_request(token))

        assert result is not None
        assert result.token == token
        assert result.can_act_as_obo_subject() is True

    @pytest.mark.asyncio
    async def test_space_delimited_scope_claim_is_parsed(self, signing_key, jwks) -> None:
        token = _token(signing_key, scope="mcp:read mcp:invoke")

        result = await strategy_for(jwks).authenticate(bearer_request(token))

        assert result is not None
        assert result.scopes == frozenset({"mcp:read", "mcp:invoke"})

    @pytest.mark.asyncio
    async def test_scp_list_claim_is_parsed(self, signing_key, jwks) -> None:
        token = _token(signing_key, scp=["mcp:read", "mcp:invoke"])

        result = await strategy_for(jwks).authenticate(bearer_request(token))

        assert result is not None
        assert result.scopes == frozenset({"mcp:read", "mcp:invoke"})

    @pytest.mark.asyncio
    async def test_audience_may_be_a_list_containing_the_expected_value(
        self, signing_key, jwks
    ) -> None:
        token = _token(signing_key, audience=["other-service", AUDIENCE])

        assert await strategy_for(jwks).authenticate(bearer_request(token)) is not None


class TestRejection:
    @pytest.mark.asyncio
    async def test_expired_token_is_rejected(self, signing_key, jwks) -> None:
        token = _token(signing_key, expires_in=-30)

        assert await strategy_for(jwks).authenticate(bearer_request(token)) is None

    @pytest.mark.asyncio
    async def test_not_yet_valid_token_is_rejected(self, signing_key, jwks) -> None:
        token = _token(signing_key, not_before_offset=3600)

        assert await strategy_for(jwks).authenticate(bearer_request(token)) is None

    @pytest.mark.asyncio
    async def test_wrong_audience_is_rejected(self, signing_key, jwks) -> None:
        # The core no-passthrough rule: a token minted for another service must not
        # be accepted here just because it is well-formed and signed by our IdP.
        token = _token(signing_key, audience="some-other-service")

        assert await strategy_for(jwks).authenticate(bearer_request(token)) is None

    @pytest.mark.asyncio
    async def test_wrong_issuer_is_rejected(self, signing_key, jwks) -> None:
        token = _token(signing_key, issuer="https://evil.example.com/")

        assert await strategy_for(jwks).authenticate(bearer_request(token)) is None

    @pytest.mark.asyncio
    async def test_signature_from_an_unknown_key_is_rejected(self, jwks) -> None:
        token = _token(_rsa_key())  # signed by a key the JWKS never advertises

        assert await strategy_for(jwks).authenticate(bearer_request(token)) is None

    @pytest.mark.asyncio
    async def test_alg_none_is_rejected(self, jwks) -> None:
        token = jwt.encode(
            {"sub": "alice", "iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 300},
            key="",
            algorithm="none",
            headers={"kid": "key-1"},
        )

        assert await strategy_for(jwks).authenticate(bearer_request(token)) is None

    @pytest.mark.asyncio
    async def test_algorithm_confusion_hs256_signed_with_public_key_is_rejected(
        self, signing_key, jwks
    ) -> None:
        # The classic attack: re-sign with HS256 using the *public* key as the HMAC
        # secret, betting the verifier trusts the token's own `alg` header. Built by
        # hand because PyJWT refuses to encode it -- which is a defense on the signing
        # side and says nothing about whether *our* verifier is safe.
        public_pem = signing_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "key-1"}).encode())
        payload = _b64url(
            json.dumps(
                {
                    "sub": "attacker",
                    "iss": ISSUER,
                    "aud": AUDIENCE,
                    "exp": int(time.time()) + 300,
                }
            ).encode()
        )
        signing_input = f"{header}.{payload}".encode()
        signature = hmac.new(public_pem, signing_input, hashlib.sha256).digest()
        token = f"{header}.{payload}.{_b64url(signature)}"

        assert await strategy_for(jwks).authenticate(bearer_request(token)) is None

    @pytest.mark.asyncio
    async def test_symmetric_algorithms_are_refused_at_construction(self, jwks) -> None:
        # A JWKS publishes public keys; an HMAC "key" there would be world-readable.
        with pytest.raises(ValueError, match="asymmetric"):
            strategy_for(jwks, algorithms=["HS256"])

    @pytest.mark.asyncio
    async def test_missing_authorization_header_is_rejected(self, jwks) -> None:
        request = MagicMock(spec=Request)
        request.headers = {}

        assert await strategy_for(jwks).authenticate(request) is None

    @pytest.mark.asyncio
    async def test_non_bearer_scheme_is_rejected(self, jwks) -> None:
        request = MagicMock(spec=Request)
        request.headers = {"Authorization": "Basic dXNlcjpwYXNz"}

        assert await strategy_for(jwks).authenticate(request) is None

    @pytest.mark.asyncio
    async def test_garbage_token_is_rejected(self, jwks) -> None:
        assert await strategy_for(jwks).authenticate(bearer_request("not-a-jwt")) is None

    @pytest.mark.asyncio
    async def test_token_without_subject_is_rejected(self, signing_key, jwks) -> None:
        # No `sub` means no identity to act on behalf of.
        token = _token(signing_key, subject="")

        assert await strategy_for(jwks).authenticate(bearer_request(token)) is None


class TestRequiredScopes:
    @pytest.mark.asyncio
    async def test_token_missing_a_required_scope_is_rejected(self, signing_key, jwks) -> None:
        token = _token(signing_key, scope="mcp:read")
        strategy = strategy_for(jwks, required_scopes=frozenset({"mcp:invoke"}))

        assert await strategy.authenticate(bearer_request(token)) is None

    @pytest.mark.asyncio
    async def test_token_with_all_required_scopes_is_accepted(self, signing_key, jwks) -> None:
        token = _token(signing_key, scope="mcp:read mcp:invoke")
        strategy = strategy_for(jwks, required_scopes=frozenset({"mcp:invoke"}))

        assert await strategy.authenticate(bearer_request(token)) is not None


class TestJWKSCaching:
    @pytest.mark.asyncio
    async def test_jwks_is_fetched_once_and_reused(self, signing_key, jwks) -> None:
        strategy = strategy_for(jwks)

        for _ in range(3):
            assert await strategy.authenticate(bearer_request(_token(signing_key))) is not None

        assert jwks.fetch_count == 1

    @pytest.mark.asyncio
    async def test_unknown_kid_triggers_a_refresh_and_then_succeeds(self) -> None:
        # Key rotation: the IdP starts signing with key-2, which we have never seen.
        original, rotated = _rsa_key(), _rsa_key()
        server = JWKSServer({"key-1": original})
        strategy = strategy_for(server, min_refresh_interval_seconds=0.0)

        assert await strategy.authenticate(bearer_request(_token(original))) is not None
        assert server.fetch_count == 1

        server.keys["key-2"] = rotated
        result = await strategy.authenticate(bearer_request(_token(rotated, kid="key-2")))

        assert result is not None
        assert server.fetch_count == 2

    @pytest.mark.asyncio
    async def test_unknown_kid_refresh_is_rate_limited(self, signing_key) -> None:
        # An unknown-kid flood must not turn the hub into a JWKS request amplifier.
        server = JWKSServer({"key-1": signing_key})
        strategy = strategy_for(server, min_refresh_interval_seconds=3600.0)

        await strategy.authenticate(bearer_request(_token(signing_key)))
        assert server.fetch_count == 1

        for _ in range(10):
            bogus = _token(_rsa_key(), kid="never-seen")
            assert await strategy.authenticate(bearer_request(bogus)) is None

        assert server.fetch_count == 2

    @pytest.mark.asyncio
    async def test_jwks_endpoint_failure_rejects_rather_than_raising(self, signing_key) -> None:
        def failing(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="upstream down")

        strategy = JWTBearerStrategy(
            issuer=ISSUER,
            audience=AUDIENCE,
            jwks_uri=JWKS_URI,
            client=httpx.AsyncClient(transport=httpx.MockTransport(failing)),
        )

        assert await strategy.authenticate(bearer_request(_token(signing_key))) is None


class TestSSRFSafety:
    def test_default_jwks_client_pins_and_refuses_redirects(self) -> None:
        # JWKS is an outbound fetch like any other in this codebase (CLAUDE.md).
        from mcp_hub.utils import SafePinnedTransport

        cache = JWKSCache(JWKS_URI)
        client = cache.build_client()

        assert client.follow_redirects is False
        assert isinstance(client._transport, SafePinnedTransport)

    @pytest.mark.asyncio
    async def test_jwks_is_not_fetched_at_construction(self, jwks) -> None:
        # ADR 0001: JWKS is lazy so a down IdP cannot stop the hub from starting.
        strategy_for(jwks)

        assert jwks.fetch_count == 0


class TestSubjectExpiry:
    @pytest.mark.asyncio
    async def test_principal_carries_the_token_expiry(self, signing_key, jwks) -> None:
        # mcp.obo_cache clamps an exchanged token's lifetime to this.
        result = await strategy_for(jwks).authenticate(
            bearer_request(_token(signing_key, expires_in=120))
        )

        assert result is not None
        assert result.expires_at is not None
        remaining = (result.expires_at - datetime.now(timezone.utc)).total_seconds()
        assert 100 < remaining <= 120


IDENTITY_ASSERTION_HEADER = "X-MCP-Identity-Assertion"


def _id_token(key, *, subject: str = "alice", audience: str = "mcp-client", **claims):
    """An OIDC ID Token. Its audience is the *client* that requested it, not the hub,
    so the hub cannot validate `aud` the way it does for an access token."""
    return _token(key, subject=subject, audience=audience, **claims)


def request_with_assertion(access_token: str, id_token: str) -> MagicMock:
    request = MagicMock(spec=Request)
    request.headers = {
        "Authorization": f"Bearer {access_token}",
        IDENTITY_ASSERTION_HEADER: id_token,
    }
    return request


class TestIdentityAssertion:
    """Story 8.3 / ADR 0006: the caller may supply an ID Token for EMA's leg 1."""

    @pytest.mark.asyncio
    async def test_a_valid_assertion_is_carried_on_the_principal(self, signing_key, jwks) -> None:
        access = _token(signing_key)
        assertion = _id_token(signing_key)

        result = await strategy_for(jwks).authenticate(request_with_assertion(access, assertion))

        assert result is not None
        assert result.id_token == assertion

    @pytest.mark.asyncio
    async def test_absent_assertion_is_not_an_error(self, signing_key, jwks) -> None:
        # Most callers will never send one; EMA servers fail closed later, per ADR 0006.
        result = await strategy_for(jwks).authenticate(bearer_request(_token(signing_key)))

        assert result is not None
        assert result.id_token == ""

    @pytest.mark.asyncio
    async def test_an_assertion_for_a_different_subject_is_refused(self, signing_key, jwks) -> None:
        # The attack this closes: alice authenticates to the hub with her own access
        # token but attaches bob's ID Token. Leg 1 would then mint an ID-JAG for bob,
        # and the downstream server would attribute alice's call to bob. The IdP
        # cannot catch this -- it only ever sees a validly-signed token for bob.
        access = _token(signing_key, subject="alice")
        someone_elses = _id_token(signing_key, subject="bob")

        result = await strategy_for(jwks).authenticate(
            request_with_assertion(access, someone_elses)
        )

        assert result is not None, "the access token itself is still valid"
        assert result.subject == "alice"
        assert result.id_token == "", "the mismatched assertion must be dropped"

    @pytest.mark.asyncio
    async def test_an_unsigned_or_forged_assertion_is_dropped(self, signing_key, jwks) -> None:
        access = _token(signing_key)
        forged = _id_token(_rsa_key())  # signed by a key the JWKS never advertises

        result = await strategy_for(jwks).authenticate(request_with_assertion(access, forged))

        assert result is not None
        assert result.id_token == ""

    @pytest.mark.asyncio
    async def test_an_expired_assertion_is_dropped(self, signing_key, jwks) -> None:
        access = _token(signing_key)
        stale = _id_token(signing_key, expires_in=-30)

        result = await strategy_for(jwks).authenticate(request_with_assertion(access, stale))

        assert result is not None
        assert result.id_token == ""

    @pytest.mark.asyncio
    async def test_an_assertion_from_another_issuer_is_dropped(self, signing_key, jwks) -> None:
        access = _token(signing_key)
        foreign = _id_token(signing_key, issuer="https://evil.example.com/")

        result = await strategy_for(jwks).authenticate(request_with_assertion(access, foreign))

        assert result is not None
        assert result.id_token == ""

    @pytest.mark.asyncio
    async def test_garbage_in_the_header_is_dropped(self, signing_key, jwks) -> None:
        result = await strategy_for(jwks).authenticate(
            request_with_assertion(_token(signing_key), "not-a-jwt")
        )

        assert result is not None
        assert result.id_token == ""

    def test_principal_repr_redacts_the_assertion(self) -> None:
        principal = Principal(subject="alice", token="a.b.c", id_token="secret.id.token")

        assert "secret.id.token" not in repr(principal)
