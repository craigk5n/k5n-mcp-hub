from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from mcp_hub.mcp.token_exchange import ExchangedToken

logger = logging.getLogger(__name__)

# Bounded by default. Unbounded, this grows with every distinct user ever seen — a
# slow memory leak whose contents happen to be live credentials.
DEFAULT_MAX_ENTRIES = 1024

# Refuse to serve a token this close to expiry, so it doesn't die mid-request.
EXPIRY_MARGIN_SECONDS = 30


@dataclass(frozen=True)
class OBOCacheKey:
    """Everything that must match for a cached token to be reusable.

    ``subject`` and ``issuer`` together are the identity — the same subject at a
    different IdP is a different person. ``server_id``, ``audience`` and ``scope``
    pin what the token is actually good for.
    """

    subject: str
    issuer: str
    server_id: str
    audience: str
    scope: str
    # "obo" (one leg) or "ema" (two legs). The two produce different tokens for the
    # same server, so an entry from one must never satisfy the other.
    flow: str = "obo"


@dataclass
class _Entry:
    token: str
    expires_at: datetime

    def __repr__(self) -> str:
        return f"_Entry(token='<redacted>', expires_at={self.expires_at!r})"


class OBOTokenCache:
    """Exchanged tokens, keyed per subject.

    The client-credentials cache in ``mcp.auth`` keys on server id alone, which is
    correct there — the hub's own identity is the same for everyone. Under OBO that
    same key would return the first caller's token to the next one.
    """

    def __init__(self, *, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self._entries: OrderedDict[OBOCacheKey, _Entry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._max_entries = max_entries

    async def token(
        self,
        key: OBOCacheKey,
        *,
        fetch: Callable[[], Awaitable[ExchangedToken]],
        subject_expires_at: datetime | None = None,
    ) -> str:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is not None and self._is_live(entry):
                self._entries.move_to_end(key)
                return entry.token
            if entry is not None:
                del self._entries[key]

        # Exchange outside the lock so one user's slow IdP round-trip doesn't stall
        # every other user's request.
        exchanged = await fetch()
        expires_at = self._expiry_for(exchanged, subject_expires_at)

        async with self._lock:
            self._entries[key] = _Entry(token=exchanged.access_token, expires_at=expires_at)
            self._entries.move_to_end(key)
            self._evict_if_needed()

        return exchanged.access_token

    async def invalidate(self, key: OBOCacheKey) -> None:
        async with self._lock:
            self._entries.pop(key, None)

    def size(self) -> int:
        return len(self._entries)

    @staticmethod
    def _expiry_for(exchanged: ExchangedToken, subject_expires_at: datetime | None) -> datetime:
        own = datetime.now(timezone.utc) + timedelta(seconds=exchanged.expires_in)
        if subject_expires_at is None:
            return own
        # Never outlive the subject token: doing so would keep acting for a user whose
        # own session has ended.
        return min(own, subject_expires_at)

    @staticmethod
    def _is_live(entry: _Entry) -> bool:
        remaining = (entry.expires_at - datetime.now(timezone.utc)).total_seconds()
        return remaining > EXPIRY_MARGIN_SECONDS

    def _evict_if_needed(self) -> None:
        while len(self._entries) > self._max_entries:
            evicted, _ = self._entries.popitem(last=False)
            logger.debug("evicting cached OBO token for subject %r", evicted.subject)

    def __repr__(self) -> str:
        return f"OBOTokenCache(entries={len(self._entries)}, max_entries={self._max_entries})"

    # -- test seams -------------------------------------------------------------
    # Reaching into private state from tests would couple them to the layout; these
    # keep the assertions readable without exposing tokens.

    def contains_for_test(self, key: OBOCacheKey) -> bool:
        return key in self._entries

    def expires_at_for_test(self, key: OBOCacheKey) -> datetime | None:
        entry = self._entries.get(key)
        return entry.expires_at if entry else None

    def expire_for_test(self, key: OBOCacheKey) -> None:
        entry = self._entries.get(key)
        if entry is not None:
            entry.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
