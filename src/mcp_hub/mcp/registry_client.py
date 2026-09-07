"""A read-only client for the official MCP registry.

Read-only is a decision, not an omission: the hub never publishes. Its records hold
internal URLs and credentials, the public index is append-only, and publishing needs a
namespace the operator can prove they own. See
`docs/adr/0008-registry-import-yes-publish-no.md`.

Two properties of this API shape the code:

- **The list is per version.** Without `version=latest` the same server comes back
  once for every version it has ever published, which is useless in a picker.
- **The schema moves.** Records in the live index carry `$schema` values from several
  dates at once, so anything not recognised is ignored rather than rejected — the same
  leniency `sdk_client` applies to a non-conformant capability response.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_REGISTRY_BASE_URL = "https://registry.modelcontextprotocol.io"

# The API is served under both /v0 and /v0.1 with the same surface; /v0.1 is the newer.
API_PREFIX = "/v0.1"

# A registry that always returns a cursor would otherwise loop forever. Same reasoning
# and roughly the same ceiling as `pagination.MAX_PAGES` for capability listing.
MAX_PAGES = 50

DEFAULT_PAGE_SIZE = 50


@dataclass
class RegistryRecord:
    """One server as the registry describes it.

    Deliberately a flat, tolerant view rather than a model of the published schema:
    the hub only needs enough to offer an import, and a strict model would break every
    time the registry adds a field.
    """

    name: str
    version: str = ""
    description: str = ""
    title: str = ""
    repository_url: str = ""
    remotes: list[dict[str, Any]] = field(default_factory=list)
    packages: list[dict[str, Any]] = field(default_factory=list)
    schema_url: str = ""
    is_latest: bool | None = None

    @property
    def is_remote(self) -> bool:
        """Directly registrable: it has a URL the hub can proxy to."""
        return bool(self.remotes)

    @property
    def needs_stdio(self) -> bool:
        """A program rather than a URL. Importable only as a suggestion — ADR 0007
        requires the command to come from the operator's allowlist, and a registry
        record is exactly the untrusted input that rule exists to refuse."""
        return not self.remotes and bool(self.packages)


def _record_from(entry: Any) -> RegistryRecord | None:
    if not isinstance(entry, dict):
        return None
    server = entry.get("server")
    if not isinstance(server, dict):
        return None
    name = server.get("name")
    if not isinstance(name, str) or not name:
        return None

    repository = server.get("repository")
    repository_url = ""
    if isinstance(repository, dict):
        url = repository.get("url")
        if isinstance(url, str):
            repository_url = url

    is_latest: bool | None = None
    meta = entry.get("_meta")
    if isinstance(meta, dict):
        official = meta.get("io.modelcontextprotocol.registry/official")
        if isinstance(official, dict) and isinstance(official.get("isLatest"), bool):
            is_latest = official["isLatest"]

    def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    return RegistryRecord(
        name=name,
        version=str(server.get("version") or ""),
        description=str(server.get("description") or ""),
        title=str(server.get("title") or ""),
        repository_url=repository_url,
        remotes=_list_of_dicts(server.get("remotes")),
        packages=_list_of_dicts(server.get("packages")),
        schema_url=str(server.get("$schema") or ""),
        is_latest=is_latest,
    )


def parse_registry_page(payload: Any) -> tuple[list[RegistryRecord], str | None]:
    """Records and the next cursor. A row that cannot be read is skipped, not fatal.

    One malformed record costing an entire page would make the picker fail for reasons
    the operator can neither see nor fix, on data the hub does not control.
    """
    if not isinstance(payload, dict):
        return [], None

    records: list[RegistryRecord] = []
    entries = payload.get("servers")
    if isinstance(entries, list):
        for entry in entries:
            record = _record_from(entry)
            if record is None:
                logger.warning("skipping unreadable registry record: %.120r", entry)
                continue
            records.append(record)

    cursor: str | None = None
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        next_cursor = metadata.get("nextCursor")
        if isinstance(next_cursor, str) and next_cursor:
            cursor = next_cursor

    return records, cursor


class RegistryClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_REGISTRY_BASE_URL,
        http_client: Any = None,
        allow_private_networks: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http_client = http_client
        self._allow_private_networks = allow_private_networks

    @property
    def servers_url(self) -> str:
        return f"{self.base_url}{API_PREFIX}/servers"

    async def _get(self, params: dict[str, Any]) -> Any:
        if self._http_client is not None:
            response = await self._http_client.get(self.servers_url, params=params)
            response.raise_for_status()
            return response.json()

        # Same SSRF-pinned transport as every other outbound call. A registry base URL
        # is operator-configurable, so it is not exempt from the guard.
        import httpx

        from mcp_hub.utils import SafePinnedTransport

        async with httpx.AsyncClient(
            follow_redirects=False,
            transport=SafePinnedTransport(allow_private_networks=self._allow_private_networks),
            timeout=30.0,
        ) as client:
            response = await client.get(self.servers_url, params=params)
            response.raise_for_status()
            return response.json()

    async def search(
        self, query: str = "", *, limit: int = DEFAULT_PAGE_SIZE
    ) -> list[RegistryRecord]:
        """One page of matches, latest versions only."""
        params: dict[str, Any] = {"limit": limit, "version": "latest"}
        if query:
            params["search"] = query
        records, _ = parse_registry_page(await self._get(params))
        return records

    async def list_all(
        self, *, query: str = "", limit: int = DEFAULT_PAGE_SIZE, max_pages: int = MAX_PAGES
    ) -> list[RegistryRecord]:
        """Follow `nextCursor` up to `max_pages`, then stop."""
        collected: list[RegistryRecord] = []
        cursor: str | None = None

        for page in range(max_pages):
            params: dict[str, Any] = {"limit": limit, "version": "latest"}
            if query:
                params["search"] = query
            if cursor:
                params["cursor"] = cursor

            records, cursor = parse_registry_page(await self._get(params))
            collected.extend(records)

            if not cursor:
                break
            if page == max_pages - 1:
                logger.warning(
                    "registry: stopping after %d pages; the index is still offering a cursor",
                    max_pages,
                )

        return collected
