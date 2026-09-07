"""Render a registered server as a registry `server.json`, for review.

The hub does not publish (ADR 0008). This produces a file the operator reads and then
publishes themselves with the official CLI, and that review step is the entire point:
the hub's records describe *this deployment* — internal addresses, stored credentials,
local authorization policy — while a registry record describes a server as everyone
else would use it. Those overlap less than they look.

So this is a translation with deliberate omissions, plus a list of things worth
looking at before anything is published.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any
from urllib.parse import urlparse

from mcp_hub.models.server import RegisteredServer

logger = logging.getLogger(__name__)

# The schema version the registry currently accepts; verified against its own
# /v0.1/validate endpoint.
SERVER_JSON_SCHEMA_URL = (
    "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
)

# Fallback namespace for a server registered by hand. The registry requires reverse-DNS
# names, and a bare hub id like "files" is not one. This is a placeholder the operator
# is expected to replace with a namespace they can actually prove they own — which is
# also why the hub cannot publish on their behalf.
UNOWNED_NAMESPACE = "com.example"

# How the hub's transport marker maps onto the registry's vocabulary. The hub's "sse"
# means *streamable HTTP* (see sdk_client and _health_badge.html); the registry means
# legacy SSE by that word. Exporting the stored value verbatim would publish a false
# claim, so the mapping is explicit in both directions of this epic.
_TRANSPORT_TO_REGISTRY = {
    "sse": "streamable-http",
    "http": "streamable-http",
    "": "streamable-http",
}

# Which header a given auth type needs, so the export can say a credential is required
# without saying anything about the credential itself.
_AUTH_HEADER = {
    "bearer": ("Authorization", "Bearer token for this server"),
    "basic": ("Authorization", "HTTP Basic credentials for this server"),
    "oauth": ("Authorization", "OAuth access token for this server"),
    "obo": ("Authorization", "Access token for the calling user"),
    "ema": ("Authorization", "Access token issued by the resource's own authorization server"),
}


def _registry_name(server: RegisteredServer) -> str:
    """The name to publish under.

    An imported server keeps the name it came with: round-tripping should not rename
    somebody else's server. A hand-registered one gets a placeholder namespace, since
    the registry requires reverse-DNS and the hub has no idea what the operator owns.
    """
    if server.registry_name:
        return server.registry_name
    if "/" in server.id:
        return server.id
    return f"{UNOWNED_NAMESPACE}/{server.id}"


def _package_for(server: RegisteredServer) -> dict[str, Any]:
    """A stdio server as a package entry.

    The hub only knows the allowlist *name* the operator chose, not what it points at
    — deliberately, since the command lives in config. So the identifier is a
    placeholder for the operator to complete, which the warnings call out.
    """
    return {
        "registryType": "npm",
        "identifier": server.stdio_command_name or server.id,
        "version": server.version or "0.0.0",
        "transport": {"type": "stdio"},
    }


def _remote_for(server: RegisteredServer) -> dict[str, Any]:
    remote: dict[str, Any] = {
        "type": _TRANSPORT_TO_REGISTRY.get(server.mcp_transport, "streamable-http"),
        "url": server.url,
    }

    header = _AUTH_HEADER.get(server.auth_type or "")
    if header is not None:
        name, description = header
        # Declared, never valued. A registry record's job is to say a credential is
        # needed; what it is belongs to the deployment, not to the description.
        remote["headers"] = [
            {"name": name, "description": description, "isRequired": True, "isSecret": True}
        ]
    return remote


def to_server_json(server: RegisteredServer) -> dict[str, Any]:
    """The registry-shaped document for this server.

    Carries identity, address and transport. Never carries: any credential or username,
    `required_scope` (local authorization policy, meaningless to anyone else), health,
    trace or fault-injection state, or provenance — a re-export is not evidence about
    where the original came from.
    """
    document: dict[str, Any] = {
        "$schema": SERVER_JSON_SCHEMA_URL,
        "name": _registry_name(server),
        "description": server.description or server.name or server.id,
        "version": server.version or "0.0.0",
    }

    if server.is_stdio:
        document["packages"] = [_package_for(server)]
    else:
        document["remotes"] = [_remote_for(server)]

    return document


def _host_warning(url: str) -> str | None:
    """Whether this address looks like it should not leave the building."""
    host = (urlparse(url).hostname or "").strip()
    if not host:
        return None

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A bare hostname with no dot is a LAN name far more often than a public one.
        if "." not in host and host != "localhost":
            return (
                f"{url!r} uses the bare hostname {host!r}, which usually resolves only "
                "inside your network"
            )
        if host == "localhost":
            return f"{url!r} points at localhost and will not resolve for anyone else"
        return None

    if address.is_loopback:
        return f"{url!r} points at loopback ({host}) and will not resolve for anyone else"
    if address.is_private or address.is_link_local or address.is_reserved:
        return f"{url!r} points at the private address {host}, which discloses internal topology"
    return None


def export_warnings(server: RegisteredServer) -> list[str]:
    """Things worth reading before publishing this.

    Not errors: the hub does not decide what an operator may publish. But a private
    URL in a public index is almost always a mistake, and the moment to notice is
    before it becomes permanent — the registry is append-only.
    """
    warnings: list[str] = []

    if not server.is_stdio:
        host_warning = _host_warning(server.url)
        if host_warning:
            warnings.append(host_warning)

    if server.required_scope:
        warnings.append(
            f"required_scope {server.required_scope!r} is this hub's authorization policy, "
            "not a property of the server. It is not exported, but check the description "
            "does not describe your internal access model either."
        )

    if server.is_stdio:
        warnings.append(
            "This is a stdio server. The exported package identifier is a placeholder: "
            "the hub knows the allowlist entry you named, not the program behind it "
            "(commands live in your config, by design). Fill in registryType, identifier "
            "and version before publishing."
        )

    if not server.registry_name:
        warnings.append(
            f"The name was generated as {_registry_name(server)!r}. The registry requires a "
            "reverse-DNS namespace you can prove you own (via DNS, GitHub, or OIDC), so "
            "replace it with yours before publishing."
        )

    if not server.description:
        warnings.append("No description: the export fell back to the server's name.")

    return warnings
