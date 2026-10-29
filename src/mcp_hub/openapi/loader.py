"""Load an OpenAPI document from bytes or a URL into a validated snapshot (Story 13.1).

The order matters: parse with bounds, identify the dialect, prove every ref is local
(`refs.check_refs`), and only then hand the document to `openapi-spec-validator`,
which would otherwise follow external refs itself.

`load_spec` is synchronous and CPU-bound -- a large spec takes a noticeable time to
validate -- so async callers use `load_spec_from_url`, or `asyncio.to_thread` for an
upload, rather than calling it on the event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from openapi_spec_validator import (
    OpenAPIV2SpecValidator,
    OpenAPIV30SpecValidator,
    OpenAPIV31SpecValidator,
)

from mcp_hub.openapi.errors import SpecError
from mcp_hub.openapi.parse import parse_document
from mcp_hub.openapi.refs import check_refs, encode_pointer
from mcp_hub.utils import safe_http_client_factory

Dialect = Literal["2.0", "3.0", "3.1"]

SUPPORTED = "supported versions are Swagger 2.0, OpenAPI 3.0 and OpenAPI 3.1"

_VALIDATORS: dict[str, Any] = {
    "2.0": OpenAPIV2SpecValidator,
    "3.0": OpenAPIV30SpecValidator,
    "3.1": OpenAPIV31SpecValidator,
}
_OPENAPI_VERSION = re.compile(r"3\.(\d+)\.\d+(?:-[0-9A-Za-z.-]+)?")
_ACCEPT = "application/json, application/yaml, application/x-yaml;q=0.9, text/yaml;q=0.9, */*;q=0.5"


@dataclass(frozen=True)
class LoadedSpec:
    """A validated spec. `document` is plain JSON data with its local refs in place;
    treat it as read-only. `sha256` is over a canonical serialization, so the same
    content re-indented, re-ordered or converted between JSON and YAML hashes the
    same -- which is what makes it usable for revision detection (ADR 0011 §2)."""

    document: dict[str, Any]
    dialect: Dialect
    sha256: str


def load_spec(data: bytes, *, max_bytes: int) -> LoadedSpec:
    """Parse, check and validate a spec, or raise `SpecError` saying why not."""
    if len(data) > max_bytes:
        raise _too_large(max_bytes)
    document, dialect = _detect_dialect(parse_document(data))
    check_refs(document)
    _validate(document, dialect)
    return LoadedSpec(document=document, dialect=dialect, sha256=canonical_sha256(document))


def canonical_sha256(document: dict[str, Any]) -> str:
    canonical = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _detect_dialect(document: dict[str, Any]) -> tuple[dict[str, Any], Dialect]:
    if "swagger" in document:
        swagger = document["swagger"]
        # `swagger: 2.0` unquoted is a float in YAML and common in older specs.
        if swagger == "2.0" or (type(swagger) is float and swagger == 2.0):
            return {**document, "swagger": "2.0"}, "2.0"
        raise SpecError(f"Unsupported Swagger version {swagger!r}: {SUPPORTED}", pointer="/swagger")
    if "openapi" not in document:
        raise SpecError("Not an OpenAPI document: it has no `openapi` or `swagger` version field")
    version = document["openapi"]
    match = _OPENAPI_VERSION.fullmatch(version) if isinstance(version, str) else None
    if match is None:
        raise SpecError(
            f"Unsupported OpenAPI version {version!r} (expected a string such as '3.1.0'): "
            f"{SUPPORTED}",
            pointer="/openapi",
        )
    minor = match.group(1)
    if minor not in ("0", "1"):
        raise SpecError(f"Unsupported OpenAPI version {version!r}: {SUPPORTED}", pointer="/openapi")
    return document, "3.0" if minor == "0" else "3.1"


def _validate(document: dict[str, Any], dialect: Dialect) -> None:
    try:
        first = next(iter(_VALIDATORS[dialect](document).iter_errors()), None)
    except RecursionError as exc:
        raise SpecError("Spec is too deeply self-referential to validate") from exc
    if first is not None:
        raise SpecError(
            f"Spec is not a valid {_dialect_name(dialect)} document: {first.message}",
            pointer=encode_pointer(first.absolute_path),
        )


def _dialect_name(dialect: Dialect) -> str:
    return "Swagger 2.0" if dialect == "2.0" else f"OpenAPI {dialect}"


def _too_large(max_bytes: int) -> SpecError:
    return SpecError(f"Spec is too large: the limit is {max_bytes} bytes (openapi.max_spec_bytes)")


# --------------------------------------------------------------------------- fetching


async def fetch_spec_bytes(
    url: str,
    *,
    max_bytes: int,
    timeout_seconds: float,
    allow_private_networks: bool,
    http_client: httpx.AsyncClient | None = None,
) -> bytes:
    """GET a spec through the SSRF-pinned client, capped in size and in total time.

    Redirects are refused, not followed: the pinned client never follows them (a 3xx
    to an internal address would bypass the pin), and the admin can register the URL
    the redirect names. `timeout_seconds` bounds the whole fetch, not each read, so a
    server dribbling bytes cannot hold an import open.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SpecError(f"Spec URL must be an absolute http(s) URL, got {url!r}")
    client = http_client or safe_http_client_factory(
        timeout=timeout_seconds, allow_private_networks=allow_private_networks
    )
    try:
        async with asyncio.timeout(timeout_seconds):
            return await _read_capped(client, url, max_bytes=max_bytes, timeout=timeout_seconds)
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise SpecError(f"Fetching the spec timed out after {timeout_seconds}s") from exc
    except httpx.HTTPError as exc:
        raise SpecError(f"Could not fetch the spec: {exc}") from exc
    finally:
        if http_client is None:
            await client.aclose()


async def _read_capped(
    client: httpx.AsyncClient, url: str, *, max_bytes: int, timeout: float
) -> bytes:
    async with client.stream("GET", url, headers={"Accept": _ACCEPT}, timeout=timeout) as response:
        if 300 <= response.status_code < 400:
            location = response.headers.get("location", "")
            raise SpecError(
                f"The spec URL redirected (HTTP {response.status_code}) to {location!r}. "
                "Redirects are not followed; import from the final URL instead"
            )
        if not response.is_success:
            raise SpecError(f"Fetching the spec returned HTTP {response.status_code}")
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            raise _too_large(max_bytes)
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > max_bytes:
                raise _too_large(max_bytes)
        return bytes(body)


async def load_spec_from_url(
    url: str,
    *,
    max_bytes: int,
    timeout_seconds: float,
    allow_private_networks: bool,
    http_client: httpx.AsyncClient | None = None,
) -> LoadedSpec:
    data = await fetch_spec_bytes(
        url,
        max_bytes=max_bytes,
        timeout_seconds=timeout_seconds,
        allow_private_networks=allow_private_networks,
        http_client=http_client,
    )
    return await asyncio.to_thread(load_spec, data, max_bytes=max_bytes)
