from dataclasses import dataclass
from ipaddress import ip_address, ip_network
from typing import Any, Optional, cast
from urllib.parse import urlparse

import socket

import httpx

from devhub.utils import SafePinnedTransport

PRIVATE_NETWORKS = (
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("127.0.0.0/8"),
    ip_network("169.254.0.0/16"),
    ip_network("::1/128"),
    ip_network("fc00::/7"),
    ip_network("fe80::/10"),
)


def _is_url_safe(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        if not parsed.netloc:
            return False
        host = parsed.hostname
        if not host:
            return False
        if parsed.username or parsed.password:
            return False
        if host in ("localhost", "localhost.", "broadcasthost"):
            return False
        try:
            ip = ip_address(host)
            for network in PRIVATE_NETWORKS:
                if ip in network:
                    return False
        except ValueError:
            try:
                results = socket.getaddrinfo(host, None)
                for result in results:
                    af, socktype, proto, canonname, sockaddr = result
                    ip_str = sockaddr[0]
                    try:
                        ip = ip_address(ip_str)
                        for network in PRIVATE_NETWORKS:
                            if ip in network:
                                return False
                    except ValueError:
                        continue
            except socket.gaierror:
                pass
        return True
    except Exception:
        return False


@dataclass
class AuthChallenge:
    scheme: str
    error: str = ""
    error_description: str = ""
    scope: str = ""
    resource: str = ""
    raw: str = ""


def parse_www_authenticate(header: str) -> Optional[AuthChallenge]:
    if not header or not header.strip():
        return None

    header = header.strip()
    parts = header.split()
    if not parts:
        return None

    scheme = parts[0]

    if scheme != "Bearer":
        return AuthChallenge(scheme=scheme, raw=header)

    if len(parts) == 1:
        return AuthChallenge(scheme=scheme, raw=header)

    param_str = " ".join(parts[1:])
    params = _split_params(param_str)

    error = ""
    error_description = ""
    scope = ""
    resource = ""

    for key, value in params.items():
        if key == "error":
            error = value
        elif key == "error_description":
            error_description = value
        elif key == "scope":
            scope = value
        elif key == "resource":
            resource = value

    return AuthChallenge(
        scheme=scheme,
        error=error,
        error_description=error_description,
        scope=scope,
        resource=resource,
        raw=header,
    )


def _split_params(param_str: str) -> dict[str, str]:
    result: dict[str, str] = {}
    i = 0
    n = len(param_str)

    while i < n:
        while i < n and param_str[i] in " \t":
            i += 1
        if i >= n:
            break

        equals_pos = param_str.find("=", i)
        if equals_pos == -1:
            break

        key = param_str[i:equals_pos].strip()
        i = equals_pos + 1

        while i < n and param_str[i] in " \t":
            i += 1
        if i >= n:
            break

        if param_str[i] == '"':
            i += 1
            value_start = i
            while i < n and param_str[i] != '"':
                i += 1
            value = param_str[value_start:i]
            if i < n:
                i += 1
        else:
            value_start = i
            while i < n and param_str[i] not in ", \t":
                i += 1
            value = param_str[value_start:i]

        result[key.lower()] = value

        while i < n and param_str[i] in " \t":
            i += 1
        if i < n and param_str[i] == ",":
            i += 1

    return result


def format_auth_challenge(challenge: Optional[AuthChallenge]) -> str:
    if challenge is None:
        return ""

    if not challenge.error and not challenge.scope and not challenge.resource:
        return challenge.raw

    segments: list[str] = []

    if challenge.error:
        if challenge.error_description:
            segments.append(f"{challenge.error}: {challenge.error_description}")
        else:
            segments.append(challenge.error)

    if challenge.scope:
        segments.append(f"scope={challenge.scope}")

    if challenge.resource:
        segments.append(f"resource={challenge.resource}")

    return " • ".join(segments)


async def discover_oauth_metadata(
    server_url: str,
    discovery_url: str = "",
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, str, dict[str, Any]]:
    if not _is_url_safe(server_url):
        raise ValueError("server_url is not a valid safe URL")

    parsed = urlparse(server_url)
    scheme = parsed.scheme if parsed.scheme else "https"
    host = parsed.netloc if parsed.netloc else server_url

    candidates: list[str] = []
    if discovery_url:
        if not _is_url_safe(discovery_url):
            raise ValueError("discovery_url is not a valid safe URL")
        candidates.append(discovery_url)
    else:
        candidates.append(f"{scheme}://{host}/.well-known/oauth-authorization-server")
        candidates.append(f"{scheme}://{host}/.well-known/openid-configuration")

    own_client = client is None
    if own_client:
        # SafePinnedTransport resolves+validates each host and connects to that exact
        # IP; follow_redirects=False so a 3xx to an internal URL can't bypass the pin.
        # Both live in devhub.utils as the single SSRF-safe HTTP layer.
        client = httpx.AsyncClient(
            follow_redirects=False,
            transport=SafePinnedTransport(),
        )

    http_client = cast(httpx.AsyncClient, client)

    try:
        for candidate in candidates:
            if not _is_url_safe(candidate):
                continue
            try:
                response = await http_client.get(
                    candidate,
                    headers={"Accept": "application/json"},
                    timeout=10.0,
                )
                if response.status_code >= 200 and response.status_code < 300:
                    payload = response.json()
                    if isinstance(payload, dict):
                        return (candidate, payload.get("issuer", ""), payload)
            except (httpx.RequestError, ValueError):
                continue
    finally:
        if own_client:
            await http_client.aclose()

    raise RuntimeError("no oauth discovery document found")


def token_endpoint_from_metadata(metadata: dict | None) -> str:
    if metadata is None:
        return ""
    return metadata.get("token_endpoint", "").strip()
