"""Enterprise-Managed Authorization: the two-leg ID-JAG flow (Stories 8.1, 8.2).

Leg 1 exchanges the caller's identity assertion at the enterprise IdP for an ID-JAG.
Leg 2 redeems that ID-JAG at the *downstream server's own* authorization server for
an access token. The second leg is what Epic 6's single-leg exchange cannot do.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mcp_hub.mcp.id_jag import (
    ID_JAG_GRANT_PROFILE,
    ID_JAG_TOKEN_TYPE,
    JWT_BEARER_GRANT_TYPE,
    TOKEN_EXCHANGE_GRANT_TYPE,
    IDJagError,
    IDJagRequest,
    exchange_for_access_token,
    supports_id_jag_profile,
)

IDP_TOKEN_URL = "https://idp.example.com/token"
RESOURCE_AS_ISSUER = "https://backend.example.com/oauth"
RESOURCE_AS_TOKEN_URL = "https://backend.example.com/oauth/token"
RESOURCE_ID = "https://files.example.com/mcp"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_id_jag(*, resource: str = RESOURCE_ID, expires_in: int = 300, **claims: Any) -> str:
    now = int(time.time())
    payload = {
        "jti": "assertion-1",
        "iss": "https://idp.example.com",
        "sub": "alice",
        "aud": RESOURCE_AS_ISSUER,
        "resource": resource,
        "client_id": "k5n-mcp-hub",
        "iat": now,
        "exp": now + expires_in,
        **claims,
    }
    return jwt.encode(payload, _KEY, algorithm="RS256")


class TwoLegRecorder:
    """Stands in for both authorization servers, recording each leg's form."""

    def __init__(
        self,
        *,
        leg1: httpx.Response | None = None,
        leg2: httpx.Response | None = None,
    ) -> None:
        self.leg1_response = leg1 or httpx.Response(
            200,
            json={
                "access_token": make_id_jag(),
                "issued_token_type": ID_JAG_TOKEN_TYPE,
                "expires_in": 300,
            },
        )
        self.leg2_response = leg2 or httpx.Response(
            200, json={"access_token": "downstream-access-token", "expires_in": 300}
        )
        self.leg1: dict[str, str] = {}
        self.leg2: dict[str, str] = {}

    def client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if str(request.url) == IDP_TOKEN_URL:
                self.leg1 = form
                return self.leg1_response
            self.leg2 = form
            return self.leg2_response

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def request_for(**overrides: Any) -> IDJagRequest:
    fields: dict[str, Any] = {
        "idp_token_url": IDP_TOKEN_URL,
        "resource_as_token_url": RESOURCE_AS_TOKEN_URL,
        "resource_as_issuer": RESOURCE_AS_ISSUER,
        "resource_id": RESOURCE_ID,
        "client_id": "k5n-mcp-hub",
        "client_secret": "hub-secret",
        "subject_token": "alice-id-token",
    }
    fields.update(overrides)
    return IDJagRequest(**fields)


class TestGrantProfileDiscovery:
    """Story 8.1."""

    def test_detects_the_profile_in_authorization_server_metadata(self) -> None:
        metadata = {
            "issuer": RESOURCE_AS_ISSUER,
            "authorization_grant_profiles_supported": [ID_JAG_GRANT_PROFILE],
        }

        assert supports_id_jag_profile(metadata) is True

    def test_absent_profile_is_false(self) -> None:
        assert supports_id_jag_profile({"issuer": RESOURCE_AS_ISSUER}) is False

    def test_missing_metadata_is_false(self) -> None:
        assert supports_id_jag_profile(None) is False

    def test_a_non_list_value_does_not_raise(self) -> None:
        assert supports_id_jag_profile({"authorization_grant_profiles_supported": "nope"}) is False


class TestLegOne:
    @pytest.mark.asyncio
    async def test_requests_an_id_jag_from_the_enterprise_idp(self) -> None:
        recorder = TwoLegRecorder()

        await exchange_for_access_token(request_for(), client=recorder.client())

        assert recorder.leg1["grant_type"] == TOKEN_EXCHANGE_GRANT_TYPE
        assert recorder.leg1["requested_token_type"] == ID_JAG_TOKEN_TYPE
        assert recorder.leg1["subject_token"] == "alice-id-token"

    @pytest.mark.asyncio
    async def test_audience_is_the_resource_authorization_servers_issuer(self) -> None:
        # Spec: MUST be the issuer identifier of the Resource Authorization Server --
        # not the MCP server's URL, which is the `resource` parameter's job.
        recorder = TwoLegRecorder()

        await exchange_for_access_token(request_for(), client=recorder.client())

        assert recorder.leg1["audience"] == RESOURCE_AS_ISSUER
        assert recorder.leg1["resource"] == RESOURCE_ID

    @pytest.mark.asyncio
    async def test_subject_token_type_is_configurable(self) -> None:
        # ADR 0006: some issuers (Keycloak among them) refuse an ID Token here.
        recorder = TwoLegRecorder()

        await exchange_for_access_token(
            request_for(subject_token_type="urn:ietf:params:oauth:token-type:access_token"),
            client=recorder.client(),
        )

        assert recorder.leg1["subject_token_type"].endswith("access_token")

    @pytest.mark.asyncio
    async def test_a_wrong_issued_token_type_is_refused(self) -> None:
        # Forwarding whatever came back would defeat the point of asking for an ID-JAG.
        recorder = TwoLegRecorder(
            leg1=httpx.Response(
                200,
                json={
                    "access_token": make_id_jag(),
                    "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
                },
            )
        )

        with pytest.raises(IDJagError, match="issued_token_type"):
            await exchange_for_access_token(request_for(), client=recorder.client())


class TestResourceClaimIsVerified:
    @pytest.mark.asyncio
    async def test_an_id_jag_for_another_resource_is_refused(self) -> None:
        # Confused deputy: an assertion minted for a different MCP server must never
        # be redeemed against this one, even though the AS might accept it.
        recorder = TwoLegRecorder(
            leg1=httpx.Response(
                200,
                json={
                    "access_token": make_id_jag(resource="https://other.example.com/mcp"),
                    "issued_token_type": ID_JAG_TOKEN_TYPE,
                },
            )
        )

        with pytest.raises(IDJagError, match="resource"):
            await exchange_for_access_token(request_for(), client=recorder.client())

        assert recorder.leg2 == {}, "leg 2 must not run after a resource mismatch"

    @pytest.mark.asyncio
    async def test_a_matching_resource_claim_proceeds(self) -> None:
        recorder = TwoLegRecorder()

        result = await exchange_for_access_token(request_for(), client=recorder.client())

        assert result.access_token == "downstream-access-token"


class TestLegTwo:
    @pytest.mark.asyncio
    async def test_redeems_the_id_jag_at_the_resource_authorization_server(self) -> None:
        recorder = TwoLegRecorder()

        await exchange_for_access_token(request_for(), client=recorder.client())

        assert recorder.leg2["grant_type"] == JWT_BEARER_GRANT_TYPE
        assert recorder.leg2["assertion"]
        assert recorder.leg2["client_id"] == "k5n-mcp-hub"

    @pytest.mark.asyncio
    async def test_the_assertion_is_the_id_jag_from_leg_one(self) -> None:
        recorder = TwoLegRecorder()

        await exchange_for_access_token(request_for(), client=recorder.client())

        claims = jwt.decode(recorder.leg2["assertion"], options={"verify_signature": False})
        assert claims["resource"] == RESOURCE_ID


class TestErrorsSayWhichLegFailed:
    @pytest.mark.asyncio
    async def test_idp_refusal_names_the_idp(self) -> None:
        # "the IdP refused" and "the backend's AS refused" have completely different
        # fixes; collapsing them wastes the operator's time.
        recorder = TwoLegRecorder(
            leg1=httpx.Response(400, json={"error": "access_denied", "error_description": "policy"})
        )

        with pytest.raises(IDJagError) as exc_info:
            await exchange_for_access_token(request_for(), client=recorder.client())

        assert exc_info.value.leg == 1
        assert exc_info.value.error == "access_denied"

    @pytest.mark.asyncio
    async def test_resource_as_refusal_names_the_resource_as(self) -> None:
        recorder = TwoLegRecorder(
            leg2=httpx.Response(400, json={"error": "invalid_grant", "error_description": "nope"})
        )

        with pytest.raises(IDJagError) as exc_info:
            await exchange_for_access_token(request_for(), client=recorder.client())

        assert exc_info.value.leg == 2
        assert exc_info.value.error == "invalid_grant"

    def test_error_summary_carries_no_token_material(self) -> None:
        error = IDJagError("failed", leg=1, error="access_denied", error_description="policy")

        assert "alice-id-token" not in str(error)
        assert "leg 1" in error.summary()


class TestSSRFSafety:
    def test_default_client_pins_and_refuses_redirects(self) -> None:
        # An identity assertion travels on leg 1 and the ID-JAG on leg 2; a 3xx to an
        # internal URL would leak either.
        from mcp_hub.mcp.id_jag import build_id_jag_client
        from mcp_hub.utils import SafePinnedTransport

        client = build_id_jag_client()

        assert client.follow_redirects is False
        assert isinstance(client._transport, SafePinnedTransport)


class TestExtensionDeclaration:
    """Story 8.5: a client declares EMA support in its per-request capabilities."""

    def test_ema_server_declares_the_extension(self) -> None:
        from mcp_hub.mcp.constants import META_CLIENT_CAPABILITIES
        from mcp_hub.mcp.id_jag import EMA_EXTENSION
        from mcp_hub.mcp.stateless import stateless_meta
        from mcp_hub.models.server import RegisteredServer

        server = RegisteredServer(id="s", url="http://x", auth_type="ema")

        meta = stateless_meta(server)

        extensions = meta[META_CLIENT_CAPABILITIES]["extensions"]
        assert EMA_EXTENSION in extensions

    def test_other_servers_gain_no_new_meta_keys(self) -> None:
        from mcp_hub.mcp.constants import META_CLIENT_CAPABILITIES
        from mcp_hub.mcp.stateless import stateless_meta
        from mcp_hub.models.server import RegisteredServer

        assert (
            stateless_meta(RegisteredServer(id="s", url="http://x"))[META_CLIENT_CAPABILITIES] == {}
        )

    def test_no_server_in_scope_is_unchanged(self) -> None:
        from mcp_hub.mcp.constants import META_CLIENT_CAPABILITIES
        from mcp_hub.mcp.stateless import stateless_meta

        assert stateless_meta(None)[META_CLIENT_CAPABILITIES] == {}
