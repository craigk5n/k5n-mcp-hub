"""A `RegisteredServer` record's OpenAPI choices, as tools (Story 13.4).

The record stores selections and overrides as lists of `(METHOD, path)` refs, and
mapping wants a key set and a key-to-override map. This is the one place that
translates, so the preview, tools/list and tools/call agree.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

from mcp_hub.models import RegisteredServer
from mcp_hub.models.openapi import OpenAPISnapshot
from mcp_hub.openapi.mapping import build_tools, preview
from mcp_hub.openapi.model import MappedOperation, OperationKey, Override, Tool


def _choices(
    server: RegisteredServer,
) -> tuple[frozenset[OperationKey], dict[OperationKey, Override]]:
    selection = frozenset(ref.key() for ref in server.openapi_selection)
    overrides = {
        ref.key(): Override(name=ref.name, description=ref.description)
        for ref in server.openapi_overrides
    }
    return selection, overrides


def openapi_tools(
    server: RegisteredServer,
    *,
    description_limit: int,
    snapshot: OpenAPISnapshot | None = None,
) -> tuple[Tool, ...]:
    """The server's selected tools from its accepted snapshot (or `snapshot`, to check
    a proposed one). Raises `MappingError` when the selection cannot be served."""
    snapshot = snapshot or server.openapi_snapshot
    if snapshot is None:
        return ()
    selection, overrides = _choices(server)
    return build_tools(
        snapshot.document,
        snapshot.dialect,
        selection=selection,
        overrides=overrides,
        description_limit=description_limit,
    )


def openapi_preview(
    server: RegisteredServer, *, description_limit: int
) -> tuple[MappedOperation, ...]:
    if server.openapi_snapshot is None:
        return ()
    selection, overrides = _choices(server)
    return preview(
        server.openapi_snapshot.document,
        server.openapi_snapshot.dialect,
        overrides=overrides,
        description_limit=description_limit,
        selection=selection,
    )


# Mapping a large spec takes most of a second, and tools/list and tools/call would
# otherwise redo it on every request. The key is everything the result depends on:
# the spec's content hash, the choices and the description limit. Bounded, and
# guarded by a lock because mapping runs in worker threads.
_CACHE_SIZE = 32
_cache: OrderedDict[tuple[Any, ...], tuple[Tool, ...]] = OrderedDict()
_cache_lock = threading.Lock()


def cached_openapi_tools(server: RegisteredServer, *, description_limit: int) -> tuple[Tool, ...]:
    """`openapi_tools` for the accepted snapshot, cached. Raises `MappingError` (not
    cached) when the selection cannot be served."""
    snapshot = server.openapi_snapshot
    if snapshot is None:
        return ()
    key = (
        snapshot.sha256,
        snapshot.dialect,
        tuple(sorted((ref.method, ref.path) for ref in server.openapi_selection)),
        tuple(
            (o.method, o.path, o.name, o.description)
            for o in sorted(server.openapi_overrides, key=lambda o: (o.method, o.path))
        ),
        description_limit,
    )
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return cached
    tools = openapi_tools(server, description_limit=description_limit)
    with _cache_lock:
        _cache[key] = tools
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return tools
