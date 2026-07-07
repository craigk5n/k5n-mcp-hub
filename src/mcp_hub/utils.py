import asyncio
import ipaddress
import json
import re
import socket
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import httpx


def pretty_json(s: str) -> str:
    try:
        return json.dumps(json.loads(s), indent=2)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(f"Invalid JSON: {str(e)}") from e


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_hostname_blocking(hostname: str) -> list[str]:
    resolved = socket.getaddrinfo(hostname, None)
    return [str(sockaddr[0]) for _, _, _, _, sockaddr in resolved]


# The SSRF pin permits loopback/private/LAN targets only when the caller passes
# allow_private_networks=True (from config.security.allow_private_networks) — a local-first
# hub must reach the localhost/LAN MCP servers it manages. It defaults to False everywhere,
# so any path that forgets to opt in fails safe. The value is threaded explicitly through the
# transport/client rather than held in a module global, so it can't leak across app instances.


def _ip_allowed(ip: Any, allow_private_networks: bool) -> bool:
    """True for a routable public address; also allows loopback/private/link-local when
    allow_private_networks is enabled (local-first mode)."""
    if allow_private_networks:
        return True
    return not (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved)


async def resolve_pinned_ip(host: str, *, allow_private_networks: bool = False) -> Optional[str]:
    """
    Resolve ``host`` once (off the event loop) and return a single validated public IP
    to connect to, or ``None`` if it can't be resolved or resolves to any disallowed
    address. Connecting to this exact IP — rather than re-resolving the name at connect
    time — is what closes the DNS-rebinding window between the safety check and the fetch.
    """
    try:
        ip_literal = ipaddress.ip_address(host)
        return host if _ip_allowed(ip_literal, allow_private_networks) else None
    except ValueError:
        pass  # not a literal IP — resolve it

    try:
        loop = asyncio.get_running_loop()
        resolved = await loop.run_in_executor(None, _resolve_hostname_blocking, host)
    except socket.gaierror:
        return None

    pinned: Optional[str] = None
    for ip_str in resolved:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if not _ip_allowed(ip, allow_private_networks):
            return None  # reject the whole host if ANY answer is disallowed
        if pinned is None:
            pinned = ip_str
    return pinned


class SafePinnedTransport(httpx.AsyncHTTPTransport):
    """
    The single SSRF-safe httpx transport for outbound discovery/MCP calls. It resolves
    and validates the target host, then connects to that exact IP while preserving the
    original hostname for the ``Host`` header and TLS SNI/certificate verification.

    There is no check-then-reconnect TOCTOU here: the connection uses the literal
    validated IP (``request.url`` is rewritten to it), so httpcore performs no second DNS
    resolution that a rebind could influence.
    """

    def __init__(self, *args: Any, allow_private_networks: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._allow_private_networks = allow_private_networks

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        pinned_ip = await resolve_pinned_ip(
            host, allow_private_networks=self._allow_private_networks
        )
        if pinned_ip is None:
            raise httpx.ConnectError(
                f"host {host!r} failed SSRF validation (unresolvable or non-public)"
            )
        request.url = request.url.copy_with(host=pinned_ip)
        request.headers["Host"] = host
        request.extensions = {**request.extensions, "sni_hostname": host}
        return await super().handle_async_request(request)


def safe_http_client_factory(
    headers: Optional[dict] = None,
    timeout: Any = None,
    auth: Any = None,
    *,
    allow_private_networks: bool = False,
) -> httpx.AsyncClient:
    """
    Build an httpx client that is SSRF-safe by construction: every connection is pinned
    to a validated public IP and redirects are never followed (a 3xx to an internal URL
    would bypass the pin). The positional signature matches the MCP SDK's
    ``httpx_client_factory`` (headers/timeout/auth) so it can guard the main
    server-connection path; bind ``allow_private_networks`` via ``functools.partial`` before
    handing it to the SDK. Reused for OAuth discovery.
    """
    kwargs: dict = {
        "follow_redirects": False,
        "transport": SafePinnedTransport(allow_private_networks=allow_private_networks),
    }
    if headers is not None:
        kwargs["headers"] = headers
    if timeout is not None:
        kwargs["timeout"] = timeout
    if auth is not None:
        kwargs["auth"] = auth
    return httpx.AsyncClient(**kwargs)


async def is_url_safe_for_discovery(
    url: str,
    require_reachability: bool = True,
    allow_private: bool = False,
) -> tuple[bool, str, list[str]]:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, "invalid URL: only http and https supported", []

        hostname = parsed.hostname
        if not hostname:
            return False, "invalid URL: no hostname", []

        resolved_ips: list[str] = []

        # allow_private lets a local-first hub target loopback/LAN MCP servers; the SSRF
        # guard below is skipped for those ranges only when the operator opts in.
        try:
            ip = ipaddress.ip_address(hostname)
            if not allow_private:
                if ip.is_loopback:
                    return False, f"invalid URL: {hostname} is a loopback IP", []
                if ip.is_private or ip.is_link_local or ip.is_reserved:
                    return False, f"invalid URL: {hostname} is a reserved/private IP", []
        except ValueError:
            pass

        try:
            loop = asyncio.get_running_loop()
            resolved_ips = await loop.run_in_executor(None, _resolve_hostname_blocking, hostname)
            for ip_str in resolved_ips:
                ip = ipaddress.ip_address(ip_str)
                if allow_private:
                    continue
                if ip.is_loopback:
                    return False, f"invalid URL: {hostname} resolves to loopback IP", []
                if ip.is_private or ip.is_link_local or ip.is_reserved:
                    return False, f"invalid URL: {hostname} resolves to private/reserved IP", []
        except socket.gaierror:
            if require_reachability:
                return False, f"invalid URL: cannot resolve {hostname}", []
            return True, "", []

        return True, "", resolved_ips
    except Exception as e:
        return False, f"invalid URL: {str(e)}", []


def sanitize_filename(name: str) -> str:
    r"""
    Sanitize a filename to prevent path traversal and other malicious inputs.

    Replaces `/`, `..`, `\`, and NUL with `_`, preventing writes outside the intended directory.
    """
    sanitized = name.replace("\0", "_")
    sanitized = sanitized.replace("/", "_")
    sanitized = sanitized.replace("\\", "_")
    sanitized = sanitized.replace("..", "_")
    return re.sub(r"[\x00-\x1f\x7f]", "_", sanitized)
