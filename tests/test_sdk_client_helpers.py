"""Tests for MCPClient helpers that don't require the `mcp` SDK to be importable."""

from mcp_hub.mcp.sdk_client import _connection_lock, _extract_status_code, _flatten_exc


class _FakeGroup(Exception):
    """Stand-in for an anyio/ExceptionGroup (avoids needing Python 3.11's builtin)."""

    def __init__(self, subs: list[BaseException]) -> None:
        self.exceptions = subs
        super().__init__("group")

    def __str__(self) -> str:
        return f"unhandled errors in a TaskGroup ({len(self.exceptions)} sub-exceptions)"


class BrokenResourceError(Exception):  # name matches anyio's, which _flatten_exc special-cases
    def __str__(self) -> str:  # anyio's BrokenResourceError has an empty message
        return ""


class TestFlattenExc:
    def test_flattens_group_to_leaf_message(self) -> None:
        assert _flatten_exc(_FakeGroup([ValueError("boom")])) == "boom"

    def test_flattens_nested_group(self) -> None:
        inner = _FakeGroup([RuntimeError("deep")])
        assert _flatten_exc(_FakeGroup([inner])) == "deep"

    def test_dedupes_and_joins_multiple(self) -> None:
        out = _flatten_exc(_FakeGroup([ValueError("a"), ValueError("a"), ValueError("b")]))
        assert out == "a; b"

    def test_broken_resource_error_gets_helpful_message(self) -> None:
        assert "rate-limiting" in _flatten_exc(BrokenResourceError())

    def test_plain_exception_uses_its_message(self) -> None:
        assert _flatten_exc(RuntimeError("nope")) == "nope"


class TestConnectionLock:
    def test_same_url_returns_same_lock(self) -> None:
        a = _connection_lock("https://example.com/mcp")
        b = _connection_lock("https://example.com/mcp")
        assert a is b

    def test_different_urls_get_different_locks(self) -> None:
        a = _connection_lock("https://a.example.com/mcp")
        b = _connection_lock("https://b.example.com/mcp")
        assert a is not b


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeHTTPStatusError(Exception):
    """Mimics httpx.HTTPStatusError (carries a .response.status_code)."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"Client error '{status_code}'")
        self.response = _FakeResponse(status_code)


class TestExtractStatusCode:
    def test_direct_status_error(self) -> None:
        assert _extract_status_code(_FakeHTTPStatusError(429)) == 429

    def test_status_nested_in_exception_group(self) -> None:
        group = _FakeGroup([RuntimeError("noise"), _FakeHTTPStatusError(503)])
        assert _extract_status_code(group) == 503

    def test_status_in_cause_chain(self) -> None:
        try:
            try:
                raise _FakeHTTPStatusError(429)
            except Exception as inner:
                raise RuntimeError("wrapped") from inner
        except RuntimeError as e:
            assert _extract_status_code(e) == 429

    def test_none_when_no_status(self) -> None:
        assert _extract_status_code(RuntimeError("boom")) is None
