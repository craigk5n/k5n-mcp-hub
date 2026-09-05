"""Caller identity reaches the outbound auth path (Story 5.5).

ADR 0004: the identity is a required argument, and a background path names itself
explicitly rather than being inferred from a missing one.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request

from mcp_hub.auth.caller import SERVICE_IDENTITY, ServiceIdentity, caller_from_request
from mcp_hub.auth.principal import Principal
from mcp_hub.mcp.auth import apply_server_auth
from mcp_hub.models.server import RegisteredServer


def request_with(principal: object | None) -> MagicMock:
    request = MagicMock(spec=Request)
    request.state = SimpleNamespace() if principal is None else SimpleNamespace(principal=principal)
    request.url = SimpleNamespace(path="/mcp")
    return request


class TestCallerIsRequired:
    def test_apply_server_auth_will_not_default_the_caller(self) -> None:
        # The point of ADR 0004: a new call site must choose, and forgetting is a
        # TypeError rather than a silent downgrade to the service identity.
        parameter = inspect.signature(apply_server_auth).parameters["caller"]

        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty

    @pytest.mark.asyncio
    async def test_omitting_it_raises(self) -> None:
        with pytest.raises(TypeError, match="caller"):
            await apply_server_auth({}, RegisteredServer(id="s", url="http://x"))  # type: ignore[call-arg]


class TestCallerFromRequest:
    def test_returns_the_authenticated_principal(self) -> None:
        principal = Principal(subject="alice", token="a.b.c")

        assert caller_from_request(request_with(principal)) is principal

    def test_falls_back_to_anonymous_never_to_the_service_identity(self) -> None:
        # An unguarded route must not inherit the hub's broader service rights. The
        # anonymous principal fails closed against an OBO server; SERVICE_IDENTITY
        # would quietly succeed.
        caller = caller_from_request(request_with(None))

        assert isinstance(caller, Principal)
        assert caller.is_anonymous is True
        assert not isinstance(caller, ServiceIdentity)

    def test_ignores_a_non_principal_on_request_state(self) -> None:
        caller = caller_from_request(request_with("alice"))

        assert isinstance(caller, Principal)
        assert caller.is_anonymous is True


class TestThreadingThroughTheProxy:
    @pytest.mark.asyncio
    async def test_proxy_forwards_the_requesting_principal(self) -> None:
        from mcp_hub.proxy.handler import build_outbound_headers

        principal = Principal(subject="alice", token="a.b.c")
        server = RegisteredServer(id="s", url="http://backend.example")

        with patch("mcp_hub.proxy.handler.apply_server_auth", new_callable=AsyncMock) as mock_auth:
            await build_outbound_headers({}, server, caller=principal)

        assert mock_auth.await_args.kwargs["caller"] is principal


class TestBackgroundPathsNameThemselves:
    @pytest.mark.asyncio
    async def test_health_check_uses_the_service_identity(self) -> None:
        import mcp_hub.health.checker as checker

        server = RegisteredServer(id="s", url="http://backend.example")

        from mcp_hub.health.parser import HealthParser

        client = MagicMock()
        client.get = AsyncMock(side_effect=RuntimeError("stop right after auth"))

        with patch.object(checker, "apply_server_auth", new_callable=AsyncMock) as mock_auth:
            await checker.check_service_health(
                server,
                HealthParser(),
                client=client,
                timeout_seconds=1,
                trace_recorder=MagicMock(),
                trace_capture_sse=False,
                trace_body_limit=100,
            )

        assert mock_auth.await_args is not None, "auth was never applied"
        assert mock_auth.await_args.kwargs["caller"] is SERVICE_IDENTITY
