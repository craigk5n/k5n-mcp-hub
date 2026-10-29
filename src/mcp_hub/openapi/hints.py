"""What a spec suggests about configuring its server -- suggestions only (Story 13.8).

The admin decides the base URL and the credential (ADR 0011 §4, §8). These helpers
read the spec's `servers[]` and security schemes so the UI can pre-fill a form the
admin then confirms; nothing here is applied automatically. Pure.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from mcp_hub.models.openapi import OpenAPISnapshot
from mcp_hub.openapi.model import OperationKey
from mcp_hub.openapi.swagger2 import swagger2_to_openapi3

_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_VARIABLE = re.compile(r"\{([^}]+)\}")


def _as_3x(snapshot: OpenAPISnapshot) -> dict[str, Any]:
    if snapshot.dialect == "2.0":
        return swagger2_to_openapi3(snapshot.document)
    return snapshot.document


def suggested_base_urls(snapshot: OpenAPISnapshot) -> list[str]:
    """`servers[]` with each variable at its default. A relative URL is resolved
    against the URL the spec was fetched from, as OpenAPI specifies; an uploaded spec
    has nothing to resolve against, so it is offered as written."""
    suggestions: list[str] = []
    for server in _as_3x(snapshot).get("servers", []):
        if not isinstance(server, dict) or not isinstance(server.get("url"), str):
            continue
        raw_variables = server.get("variables")
        variables: dict[str, Any] = raw_variables if isinstance(raw_variables, dict) else {}

        def default(match: re.Match[str]) -> str:
            variable = variables.get(match.group(1), {})
            value = variable.get("default") if isinstance(variable, dict) else None
            return str(value) if value is not None else match.group(0)

        url = _VARIABLE.sub(default, server["url"])
        if not urlsplit(url).scheme and snapshot.source_url.startswith(("http://", "https://")):
            url = urljoin(snapshot.source_url, url)
        if url not in suggestions:
            suggestions.append(url)
    return suggestions


def security_scheme_hints(snapshot: OpenAPISnapshot) -> list[dict[str, Any]]:
    """Each security scheme, and the hub auth fields it suggests -- or None where the
    hub has no matching credential (cookies, which it never sends; OpenID Connect
    discovery; mutual TLS)."""
    schemes = _as_3x(snapshot).get("components", {}).get("securitySchemes", {})
    hints: list[dict[str, Any]] = []
    for name, scheme in schemes.items():
        if not isinstance(scheme, dict):
            continue
        hints.append(
            {
                "name": name,
                "type": scheme.get("type", ""),
                "description": scheme.get("description", ""),
                "suggest": _suggestion(scheme),
            }
        )
    return hints


def _suggestion(scheme: dict[str, Any]) -> dict[str, str] | None:
    kind = scheme.get("type")
    if kind == "apiKey" and scheme.get("in") in ("header", "query") and scheme.get("name"):
        return {
            "auth_type": "api_key",
            "api_key_name": str(scheme["name"]),
            "api_key_in": scheme["in"],
        }
    if kind == "http":
        http_scheme = str(scheme.get("scheme", "")).lower()
        if http_scheme == "basic":
            return {"auth_type": "basic"}
        if http_scheme == "bearer":
            return {"auth_type": "bearer"}
    if kind == "oauth2":
        flows = scheme.get("flows", {}) if isinstance(scheme.get("flows"), dict) else {}
        for flow in ("clientCredentials", "authorizationCode", "password"):
            token_url = (
                flows.get(flow, {}).get("tokenUrl") if isinstance(flows.get(flow), dict) else None
            )
            if token_url:
                return {"auth_type": "oauth", "oauth_token_url": str(token_url)}
    return None


def operation_tags(snapshot: OpenAPISnapshot) -> dict[OperationKey, list[str]]:
    tags: dict[OperationKey, list[str]] = {}
    document = snapshot.document
    for path, item in document.get("paths", {}).items():
        if path.startswith("x-") or not isinstance(item, dict):
            continue
        for method in _METHODS:
            operation = item.get(method)
            if isinstance(operation, dict):
                value = operation.get("tags", [])
                tags[OperationKey(method, path)] = (
                    [str(t) for t in value] if isinstance(value, list) else []
                )
    return tags
