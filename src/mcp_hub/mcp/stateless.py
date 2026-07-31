"""Client for the stateless MCP revision (2026-07-28).

The 2026-07-28 spec removed the ``initialize``/``notifications/initialized``
handshake and protocol sessions: every request is a self-contained JSON-RPC
POST carrying the protocol version, client capabilities, and client identity
in ``params._meta``. The installed ``mcp`` SDK only speaks the handshake
revisions, so this module implements the stateless wire format directly over
httpx — using the same SSRF-pinned transport and per-server auth as every
other outbound path in the hub.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from mcp_hub.mcp.auth import apply_server_auth
from mcp_hub.mcp.constants import (
    MCP_CLIENT_NAME,
    MCP_CLIENT_VERSION,
    META_CLIENT_CAPABILITIES,
    META_CLIENT_INFO,
    META_PROTOCOL_VERSION,
    METHOD_SERVER_DISCOVER,
    STATELESS_PROTOCOL_VERSION,
)
from mcp_hub.mcp.sdk_client import MCPClientError, _flatten_exc
from mcp_hub.mcp.sse import extract_sse_data
from mcp_hub.models.server import RegisteredServer
from mcp_hub.utils import safe_http_client_factory


def stateless_meta() -> dict[str, Any]:
    """The ``_meta`` object every stateless request must carry."""
    return {
        META_PROTOCOL_VERSION: STATELESS_PROTOCOL_VERSION,
        META_CLIENT_CAPABILITIES: {},
        META_CLIENT_INFO: {"name": MCP_CLIENT_NAME, "version": MCP_CLIENT_VERSION},
    }


@dataclass
class DiscoverResult:
    """Parsed ``server/discover`` response."""

    server_name: str = ""
    server_version: str = ""
    protocol_version: str = STATELESS_PROTOCOL_VERSION
    protocol_versions: list[str] = field(default_factory=list)
    capabilities: dict[str, Any] = field(default_factory=dict)


class StatelessMCPClient:
    """One JSON-RPC POST per operation; no session, no handshake, no context manager."""

    def __init__(
        self,
        base_url: str,
        *,
        server: RegisteredServer | None = None,
        allow_private_networks: bool = False,
    ) -> None:
        self.base_url = base_url
        self.server = server
        self._allow_private_networks = allow_private_networks
        self._request_counter = 0

    async def discover(self, timeout: float = 30.0) -> DiscoverResult:
        result = await self._post(METHOD_SERVER_DISCOVER, {}, kind="discover", timeout=timeout)
        return self._parse_discover(result)

    async def list(self, method: str, timeout: float = 30.0) -> Any:
        """Run a list method (``tools/list`` etc.) and return the raw result object."""
        return await self._post(method, {}, kind="list", timeout=timeout)

    async def _post(
        self,
        method: str,
        params: dict[str, Any],
        *,
        kind: Any,
        timeout: float,
    ) -> Any:
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": STATELESS_PROTOCOL_VERSION,
            # Required standard request headers on Streamable HTTP POSTs (2026-07-28).
            "Mcp-Method": method,
        }
        if self.server is not None:
            await apply_server_auth(
                headers, self.server, allow_private_networks=self._allow_private_networks
            )

        self._request_counter += 1
        payload = {
            "jsonrpc": "2.0",
            "id": f"hub-{self._request_counter}",
            "method": method,
            "params": {**params, "_meta": stateless_meta()},
        }

        try:
            async with safe_http_client_factory(
                timeout=timeout, allow_private_networks=self._allow_private_networks
            ) as client:
                response = await client.post(self.base_url, json=payload, headers=headers)
        except MCPClientError:
            raise
        except Exception as e:
            raise MCPClientError(f"{method} request failed: {_flatten_exc(e)}", kind=kind) from e

        if response.status_code >= 400:
            raise MCPClientError(
                f"{method} returned HTTP {response.status_code}",
                kind=kind,
                status_code=response.status_code,
            )

        body = self._parse_body(response, method, kind)

        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            code = error.get("code")
            raise MCPClientError(
                f"{method} failed: {error.get('message', 'unknown error')}",
                kind=kind,
                jsonrpc_code=code if isinstance(code, int) else None,
            )

        return body.get("result") if isinstance(body, dict) else body

    def _parse_body(self, response: Any, method: str, kind: Any) -> Any:
        content_type = response.headers.get("Content-Type", "")
        raw: bytes = response.content
        if "text/event-stream" in content_type:
            extracted = extract_sse_data(response.content)
            if extracted is None:
                raise MCPClientError(f"{method} returned an empty event stream", kind=kind)
            raw = extracted if isinstance(extracted, bytes) else bytes(extracted)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            raise MCPClientError(f"{method} returned invalid JSON: {e}", kind=kind) from e

    @staticmethod
    def _parse_discover(result: Any) -> DiscoverResult:
        if not isinstance(result, dict):
            return DiscoverResult()

        versions_raw = result.get("protocolVersions")
        versions = [str(v) for v in versions_raw] if isinstance(versions_raw, list) else []
        if not versions and result.get("protocolVersion"):
            versions = [str(result["protocolVersion"])]

        # Prefer the stateless revision when advertised; otherwise take the newest.
        if STATELESS_PROTOCOL_VERSION in versions:
            chosen = STATELESS_PROTOCOL_VERSION
        elif versions:
            chosen = max(versions)
        else:
            chosen = STATELESS_PROTOCOL_VERSION

        server_info = result.get("serverInfo")
        if not isinstance(server_info, dict):
            server_info = {}
        capabilities = result.get("capabilities")
        if not isinstance(capabilities, dict):
            capabilities = {}

        return DiscoverResult(
            server_name=str(server_info.get("name", "")),
            server_version=str(server_info.get("version", "")),
            protocol_version=chosen,
            protocol_versions=versions,
            capabilities=capabilities,
        )
