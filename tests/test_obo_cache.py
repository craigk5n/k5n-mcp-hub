"""Per-subject exchanged-token cache (Story 6.2).

The existing client-credentials cache keys on server.id alone, which under OBO would
hand one user's downstream token to the next caller. That cross-user test is the
reason this class exists.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mcp_hub.mcp.obo_cache import OBOCacheKey, OBOTokenCache
from mcp_hub.mcp.token_exchange import ExchangedToken


def key(
    subject: str = "alice",
    *,
    issuer: str = "https://idp.example.com",
    server_id: str = "files",
    audience: str = "mcp-server-files",
    scope: str = "",
) -> OBOCacheKey:
    return OBOCacheKey(
        subject=subject, issuer=issuer, server_id=server_id, audience=audience, scope=scope
    )


def fetcher(value: str, *, expires_in: int = 300):
    calls: list[int] = []

    async def fetch() -> ExchangedToken:
        calls.append(1)
        return ExchangedToken(access_token=value, expires_in=expires_in)

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


class TestCrossUserIsolation:
    @pytest.mark.asyncio
    async def test_one_users_token_is_never_returned_for_another(self) -> None:
        cache = OBOTokenCache()

        alice = await cache.token(key("alice"), fetch=fetcher("alice-downstream-token"))
        bob = await cache.token(key("bob"), fetch=fetcher("bob-downstream-token"))

        assert alice == "alice-downstream-token"
        assert bob == "bob-downstream-token"

    @pytest.mark.asyncio
    async def test_a_second_user_does_not_hit_the_first_users_entry(self) -> None:
        cache = OBOTokenCache()
        await cache.token(key("alice"), fetch=fetcher("alice-token"))

        bob_fetch = fetcher("bob-token")
        await cache.token(key("bob"), fetch=bob_fetch)

        # Bob's request must actually perform its own exchange, not reuse a cache hit.
        assert len(bob_fetch.calls) == 1  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_same_subject_from_a_different_issuer_is_a_different_identity(self) -> None:
        # "alice" at one IdP is not "alice" at another.
        cache = OBOTokenCache()
        await cache.token(key("alice", issuer="https://idp-a.example"), fetch=fetcher("a"))

        second = fetcher("b")
        await cache.token(key("alice", issuer="https://idp-b.example"), fetch=second)

        assert len(second.calls) == 1  # type: ignore[attr-defined]


class TestKeyComponents:
    @pytest.mark.parametrize(
        "overrides",
        [{"server_id": "other"}, {"audience": "other-audience"}, {"scope": "mcp:admin"}],
        ids=["server", "audience", "scope"],
    )
    @pytest.mark.asyncio
    async def test_each_component_separates_entries(self, overrides: dict) -> None:
        cache = OBOTokenCache()
        await cache.token(key(), fetch=fetcher("first"))

        second = fetcher("second")
        result = await cache.token(key(**overrides), fetch=second)

        assert result == "second"
        assert len(second.calls) == 1  # type: ignore[attr-defined]


class TestReuseAndExpiry:
    @pytest.mark.asyncio
    async def test_a_live_entry_is_reused(self) -> None:
        cache = OBOTokenCache()
        fetch = fetcher("token")

        for _ in range(3):
            assert await cache.token(key(), fetch=fetch) == "token"

        assert len(fetch.calls) == 1  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_an_expired_entry_is_refetched(self) -> None:
        cache = OBOTokenCache()
        # expires_in floors at the minimum lifetime, so expire it explicitly.
        await cache.token(key(), fetch=fetcher("stale"))
        cache.expire_for_test(key())

        fresh = fetcher("fresh")
        assert await cache.token(key(), fetch=fresh) == "fresh"
        assert len(fresh.calls) == 1  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_entry_never_outlives_the_subject_token(self) -> None:
        # The exchanged token may be granted a longer life than the user's session;
        # caching past the subject's expiry would keep acting for a logged-out user.
        cache = OBOTokenCache()
        subject_expiry = datetime.now(timezone.utc) + timedelta(seconds=45)

        await cache.token(
            key(), fetch=fetcher("token", expires_in=3600), subject_expires_at=subject_expiry
        )

        assert cache.expires_at_for_test(key()) == subject_expiry

    @pytest.mark.asyncio
    async def test_invalidate_forces_a_fresh_exchange(self) -> None:
        # Story 6.4 uses this when a backend rejects a previously-good token.
        cache = OBOTokenCache()
        await cache.token(key(), fetch=fetcher("old"))

        await cache.invalidate(key())
        fresh = fetcher("new")

        assert await cache.token(key(), fetch=fresh) == "new"
        assert len(fresh.calls) == 1  # type: ignore[attr-defined]


class TestBounded:
    @pytest.mark.asyncio
    async def test_evicts_least_recently_used_beyond_the_cap(self) -> None:
        # Unbounded, this grows with every user ever seen -- a slow memory leak that
        # doubles as a store of live credentials.
        cache = OBOTokenCache(max_entries=3)

        for name in ("a", "b", "c"):
            await cache.token(key(name), fetch=fetcher(f"{name}-token"))
        await cache.token(key("a"), fetch=fetcher("unused"))  # refresh a's recency
        await cache.token(key("d"), fetch=fetcher("d-token"))

        assert cache.size() == 3
        assert cache.contains_for_test(key("a")) is True
        assert cache.contains_for_test(key("b")) is False  # least recently used


class TestNeverLeaksTokens:
    @pytest.mark.asyncio
    async def test_repr_redacts_cached_tokens(self) -> None:
        cache = OBOTokenCache()
        await cache.token(key(), fetch=fetcher("super-secret-token"))

        assert "super-secret-token" not in repr(cache)

    def test_cache_key_repr_carries_no_secret(self) -> None:
        assert "alice" in repr(key("alice"))  # subject is an identifier, not a secret
