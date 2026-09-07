"""Spans on the OBO/EMA token exchange (Epic 11, Story 11.3, remainder).

The exchange is the single most security-sensitive thing the hub does and the place
its own history says secrets appear in unexpected shapes: `sanitize_trace_body` grew a
prose pattern because an IdP echoed the rejected token back inside an error
description. A span here must therefore be useful and say almost nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from mcp_hub.auth.principal import Principal
from mcp_hub.config import Settings
from mcp_hub.mcp.auth import apply_server_auth
from mcp_hub.mcp.obo_cache import OBOTokenCache
from mcp_hub.mcp.token_exchange import ExchangedToken, TokenExchangeError
from mcp_hub.models.server import RegisteredServer

pytest.importorskip("opentelemetry.sdk")


def _recording_provider() -> tuple[Any, Any]:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from mcp_hub.observability.otel import OtelProvider

    exporter = InMemorySpanExporter()
    sdk = TracerProvider()
    sdk.add_span_processor(SimpleSpanProcessor(exporter))
    config = Settings(otel={"enabled": True, "endpoint": "http://localhost:4318"}).otel
    return OtelProvider(config, tracer=sdk.get_tracer("test")), exporter


def _alice() -> Principal:
    return Principal(
        subject="alice",
        issuer="https://idp.example.com",
        token="caller-token",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


def _obo_server() -> RegisteredServer:
    return RegisteredServer(
        id="files",
        url="https://files.example.com/mcp",
        auth_type="obo",
        obo_audience="mcp-server-files",
        oauth_token_url="https://idp.example.com/token",
        oauth_client_id="k5n-mcp-hub",
        oauth_client_secret="hub-secret",
    )


class TestSuccessfulExchange:
    @pytest.mark.asyncio
    async def test_it_produces_a_span_naming_the_flow_and_audience(self) -> None:
        provider, exporter = _recording_provider()

        async def exchange(request: Any, **kwargs: Any) -> ExchangedToken:
            return ExchangedToken(access_token="downstream-token", expires_in=300)

        await apply_server_auth(
            {},
            _obo_server(),
            caller=_alice(),
            obo_cache=OBOTokenCache(),
            exchange=exchange,
            otel=provider,
        )

        spans = [s for s in exporter.get_finished_spans() if s.name == "mcp.token_exchange"]
        assert spans, "an on-behalf-of exchange must be traced"
        attributes = spans[0].attributes
        assert attributes["mcp.server.id"] == "files"
        assert attributes["mcp.auth.flow"] == "obo"
        # The audience is the backend's name at the IdP -- configuration, not a secret,
        # and the single most useful attribute when an exchange starts failing.
        assert attributes["mcp.auth.audience"] == "mcp-server-files"

    @pytest.mark.asyncio
    async def test_no_token_or_client_secret_is_exported(self) -> None:
        provider, exporter = _recording_provider()

        async def exchange(request: Any, **kwargs: Any) -> ExchangedToken:
            return ExchangedToken(access_token="downstream-token", expires_in=300)

        await apply_server_auth(
            {},
            _obo_server(),
            caller=_alice(),
            obo_cache=OBOTokenCache(),
            exchange=exchange,
            otel=provider,
        )

        exported = str([s.attributes for s in exporter.get_finished_spans()])
        for secret in ("caller-token", "downstream-token", "hub-secret"):
            assert secret not in exported, f"{secret!r} reached the exporter"

    @pytest.mark.asyncio
    async def test_the_subject_is_withheld_by_default(self) -> None:
        provider, exporter = _recording_provider()

        async def exchange(request: Any, **kwargs: Any) -> ExchangedToken:
            return ExchangedToken(access_token="downstream-token", expires_in=300)

        await apply_server_auth(
            {},
            _obo_server(),
            caller=_alice(),
            obo_cache=OBOTokenCache(),
            exchange=exchange,
            otel=provider,
        )

        exported = str([s.attributes for s in exporter.get_finished_spans()])
        assert "alice" not in exported


class TestFailedExchange:
    @pytest.mark.asyncio
    async def test_the_error_type_is_recorded_and_the_idp_text_is_not(self) -> None:
        """The exact leak sanitize_trace_body exists for: an IdP error description
        quoting the token it rejected."""
        provider, exporter = _recording_provider()

        async def failing(request: Any, **kwargs: Any) -> ExchangedToken:
            raise TokenExchangeError(
                "invalid_grant: subject_token 'eyJhbGciOiJSUzI1.leaked.token' is not active"
            )

        with pytest.raises(Exception):
            await apply_server_auth(
                {},
                _obo_server(),
                caller=_alice(),
                obo_cache=OBOTokenCache(),
                exchange=failing,
                otel=provider,
            )

        spans = [s for s in exporter.get_finished_spans() if s.name == "mcp.token_exchange"]
        assert spans, "a failed exchange is exactly what an operator needs to see"
        attributes = spans[0].attributes
        assert attributes["error.type"] == "TokenExchangeError"
        assert "eyJhbGciOiJSUzI1" not in str(attributes)
        assert "leaked" not in str(attributes)
        assert spans[0].status.is_ok is False


class TestWithoutTelemetry:
    @pytest.mark.asyncio
    async def test_the_exchange_works_with_no_provider_passed(self) -> None:
        # Every existing call site passes no `otel`; none of them should change.
        async def exchange(request: Any, **kwargs: Any) -> ExchangedToken:
            return ExchangedToken(access_token="downstream-token", expires_in=300)

        headers: dict[str, str] = {}
        await apply_server_auth(
            headers, _obo_server(), caller=_alice(), obo_cache=OBOTokenCache(), exchange=exchange
        )
        assert headers["Authorization"] == "Bearer downstream-token"
