import json
import time
from typing import Any, cast

import pytest

from mcp_hub.proxy.fault_injection import apply_fault_injection, DEFAULT_FAULT_TIMEOUT_MS
from mcp_hub.models.server import RegisteredServer, FaultInjection


def make_server(**kwargs: Any) -> RegisteredServer:
    fault_injection = FaultInjection(**kwargs)
    return RegisteredServer(
        id="test-id",
        url="https://test.example.com",
        fault_injection=fault_injection,
    )


class TestFaultInjectionEnabledFalse:
    @pytest.mark.asyncio
    async def test_returns_none_when_enabled_false(self) -> None:
        server = make_server(enabled=False, malformed_json=True, timeout_enabled=True)
        request = {"headers": {}, "method": "POST", "body": {}}

        result = await apply_fault_injection(request, server)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_enabled_false_sse_interrupt(self) -> None:
        server = make_server(enabled=False, sse_interrupt=True)
        request = {
            "headers": {"Accept": "text/event-stream"},
            "method": "POST",
            "body": {},
        }

        result = await apply_fault_injection(request, server)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_enabled_false_invalid_method(self) -> None:
        server = make_server(enabled=False, invalid_method=True)
        request = {"headers": {}, "method": "POST", "body": {}}

        result = await apply_fault_injection(request, server)

        assert result is None


class TestFaultInjectionMalformedJson:
    @pytest.mark.asyncio
    async def test_returns_malformed_json(self) -> None:
        server = make_server(enabled=True, malformed_json=True)
        request = {"headers": {}, "method": "POST", "body": {}}

        result = await apply_fault_injection(request, server)

        assert result is not None
        assert result.body == b"{bad json"
        assert result.status_code == 200
        assert result.media_type == "application/json"


class TestFaultInjectionTimeout:
    @pytest.mark.asyncio
    async def test_returns_504_after_delay(self) -> None:
        server = make_server(enabled=True, timeout_enabled=True, timeout_millis=50)
        request = {"headers": {}, "method": "POST", "body": {}}

        start = time.time()
        result = await apply_fault_injection(request, server)
        elapsed = (time.time() - start) * 1000

        assert result is not None
        assert result.status_code == 504
        assert result.media_type == "text/plain"
        assert result.body == b"Injected timeout\n"
        assert elapsed >= 45

    @pytest.mark.asyncio
    async def test_uses_default_timeout_when_not_specified(self) -> None:
        server = make_server(enabled=True, timeout_enabled=True, timeout_millis=0)
        request = {"headers": {}, "method": "POST", "body": {}}

        start = time.time()
        result = await apply_fault_injection(request, server)
        elapsed = (time.time() - start) * 1000

        assert result is not None
        assert elapsed >= (DEFAULT_FAULT_TIMEOUT_MS - 100)


class TestFaultInjectionInvalidMethod:
    @pytest.mark.asyncio
    async def test_returns_method_not_found_with_id(self) -> None:
        server = make_server(enabled=True, invalid_method=True)
        request = {
            "headers": {},
            "method": "POST",
            "body": {"jsonrpc": "2.0", "method": "foo", "id": 42},
        }

        result = await apply_fault_injection(request, server)

        assert result is not None
        assert result.status_code == 200
        assert result.media_type == "application/json"
        response_body = json.loads(cast(bytes, result.body).decode())
        assert response_body["id"] == 42
        assert response_body["error"]["code"] == -32601
        assert response_body["error"]["message"] == "Method not found"

    @pytest.mark.asyncio
    async def test_returns_method_not_found_with_null_id(self) -> None:
        server = make_server(enabled=True, invalid_method=True)
        request = {
            "headers": {},
            "method": "POST",
            "body": {"jsonrpc": "2.0", "method": "foo", "id": None},
        }

        result = await apply_fault_injection(request, server)

        assert result is not None
        response_body = json.loads(cast(bytes, result.body).decode())
        assert response_body["id"] is None

    @pytest.mark.asyncio
    async def test_returns_method_not_found_with_string_body(self) -> None:
        server = make_server(enabled=True, invalid_method=True)
        request = {
            "headers": {},
            "method": "POST",
            "body": '{"jsonrpc": "2.0", "method": "foo", "id": 99}',
        }

        result = await apply_fault_injection(request, server)

        assert result is not None
        response_body = json.loads(cast(bytes, result.body).decode())
        assert response_body["id"] == 99

    @pytest.mark.asyncio
    async def test_returns_method_not_found_with_missing_id(self) -> None:
        server = make_server(enabled=True, invalid_method=True)
        request = {"headers": {}, "method": "POST", "body": {"jsonrpc": "2.0", "method": "foo"}}

        result = await apply_fault_injection(request, server)

        assert result is not None
        response_body = json.loads(cast(bytes, result.body).decode())
        assert response_body["id"] is None


class TestFaultInjectionSSEInterrupt:
    @pytest.mark.asyncio
    async def test_returns_sse_interrupt_with_accept_header(self) -> None:
        server = make_server(enabled=True, sse_interrupt=True)
        request = {
            "headers": {"Accept": "text/event-stream"},
            "method": "POST",
            "body": {},
        }

        result = await apply_fault_injection(request, server)

        assert result is not None
        assert result.status_code == 200
        assert result.media_type == "text/event-stream"
        assert result.body == b'event: error\ndata: {"error":"sse interrupted"}\n\n'
        assert result.headers.get("cache-control") == "no-cache"

    @pytest.mark.asyncio
    async def test_returns_sse_interrupt_with_get_method(self) -> None:
        server = make_server(enabled=True, sse_interrupt=True)
        request = {"headers": {"Accept": "application/json"}, "method": "GET", "body": {}}

        result = await apply_fault_injection(request, server)

        assert result is not None
        assert result.status_code == 200
        assert result.media_type == "text/event-stream"

    @pytest.mark.asyncio
    async def test_does_not_trigger_sse_interrupt_without_header_or_get(self) -> None:
        server = make_server(enabled=True, sse_interrupt=True)
        request = {
            "headers": {"Accept": "application/json"},
            "method": "POST",
            "body": {},
        }

        result = await apply_fault_injection(request, server)

        assert result is None


class TestFaultInjectionOrder:
    @pytest.mark.asyncio
    async def test_sse_interrupt_takes_precedence_over_malformed_json(self) -> None:
        server = make_server(enabled=True, sse_interrupt=True, malformed_json=True)
        request = {
            "headers": {"Accept": "text/event-stream"},
            "method": "POST",
            "body": {},
        }

        result = await apply_fault_injection(request, server)

        assert result is not None
        assert result.media_type == "text/event-stream"


class TestFaultInjectionDefault:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_faults_enabled(self) -> None:
        server = make_server(enabled=True)
        request = {"headers": {}, "method": "POST", "body": {}}

        result = await apply_fault_injection(request, server)

        assert result is None
