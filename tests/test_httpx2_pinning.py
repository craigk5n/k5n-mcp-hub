"""SSRF pinning across both HTTP libraries (Story 4.1).

`mcp` 2.x takes an `httpx2.AsyncClient` rather than headers plus an httpx factory,
so the hub now has to keep its SSRF guarantee on two client stacks. A transport that
pinned only httpx would silently leave every SDK call unpinned — the guard would look
present and not be.
"""

from __future__ import annotations

import httpx
import httpx2
import pytest

from mcp_hub.utils import (
    SafePinnedHttpx2Transport,
    SafePinnedTransport,
    safe_http_client_factory,
    safe_httpx2_client_factory,
)


class TestBothTransportsExist:
    def test_httpx_transport_subclasses_httpx(self) -> None:
        assert issubclass(SafePinnedTransport, httpx.AsyncHTTPTransport)

    def test_httpx2_transport_subclasses_httpx2(self) -> None:
        assert issubclass(SafePinnedHttpx2Transport, httpx2.AsyncHTTPTransport)

    def test_they_share_one_pinning_implementation(self) -> None:
        # The security logic must live in one place; two copies drift.
        from mcp_hub.utils import _PinnedTransportMixin

        assert issubclass(SafePinnedTransport, _PinnedTransportMixin)
        assert issubclass(SafePinnedHttpx2Transport, _PinnedTransportMixin)


class TestHttpx2ClientFactory:
    def test_pins_and_refuses_redirects(self) -> None:
        client = safe_httpx2_client_factory()

        assert isinstance(client, httpx2.AsyncClient)
        assert client.follow_redirects is False
        assert isinstance(client._transport, SafePinnedHttpx2Transport)

    def test_carries_headers_through(self) -> None:
        # 2.x dropped the `headers` argument from streamable_http_client, so auth now
        # rides on the client. If this were lost, every authenticated handshake
        # server would start failing.
        client = safe_httpx2_client_factory(headers={"Authorization": "Bearer tok"})

        assert client.headers["Authorization"] == "Bearer tok"

    def test_honours_allow_private_networks(self) -> None:
        blocked = safe_httpx2_client_factory()
        allowed = safe_httpx2_client_factory(allow_private_networks=True)

        assert blocked._transport._allow_private_networks is False
        assert allowed._transport._allow_private_networks is True


class TestPinningActuallyBlocks:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "factory", [safe_http_client_factory, safe_httpx2_client_factory], ids=["httpx", "httpx2"]
    )
    async def test_a_private_address_is_refused_by_default(self, factory) -> None:
        client = factory()
        try:
            with pytest.raises(Exception) as exc_info:
                await client.get("http://127.0.0.1:9/blocked")
            assert "SSRF" in str(exc_info.value) or "failed" in str(exc_info.value).lower()
        finally:
            await client.aclose()
