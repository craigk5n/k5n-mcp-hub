from __future__ import annotations

from typing import Any

from fastapi import Request

from mcp_hub.config import AuthConfig

# RFC 9728. A client fetches this before it has a token, to learn which
# authorization server guards the hub.
PROTECTED_RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"

REALM = "k5n-mcp-hub"


def resource_identifier(request: Request, configured: str = "") -> str:
    """The hub's identity as an OAuth protected resource.

    Derived from the request by default, which is right for direct access. Behind a
    reverse proxy the request URL is not the hub's public identity, so
    ``auth.jwt.resource`` overrides it."""
    override = configured.strip().rstrip("/")
    if override:
        return override
    return str(request.base_url).rstrip("/")


def metadata_url(request: Request, configured: str = "") -> str:
    return f"{resource_identifier(request, configured)}{PROTECTED_RESOURCE_METADATA_PATH}"


def protected_resource_metadata(request: Request, auth: AuthConfig) -> dict[str, Any]:
    return {
        "resource": resource_identifier(request, auth.jwt.resource),
        "authorization_servers": [auth.jwt.issuer.strip()],
        "scopes_supported": list(auth.jwt.required_scopes),
        # Header only. A token in a query string ends up in access logs, browser
        # history, and Referer headers.
        "bearer_methods_supported": ["header"],
    }


def bearer_challenge(request: Request, auth: AuthConfig) -> str:
    """The ``WWW-Authenticate`` value for a rejected request.

    ``resource_metadata`` is the RFC 9728 parameter that points a client at the
    document above — it is what makes a 401 self-describing rather than a dead end."""
    return f'Bearer realm="{REALM}", resource_metadata="{metadata_url(request, auth.jwt.resource)}"'
