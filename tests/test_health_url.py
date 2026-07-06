import pytest
from mcp_hub.health import build_health_url


class TestBuildHealthUrl:
    def test_strip_trailing_mcp(self) -> None:
        assert build_health_url("http://x:9000/mcp") == "http://x:9000/health"

    def test_strip_trailing_mcp_with_slash(self) -> None:
        assert build_health_url("http://x:9000/mcp/") == "http://x:9000/health"

    def test_strip_trailing_slash_only(self) -> None:
        assert build_health_url("http://x:9000/") == "http://x:9000/health"

    def test_nested_mcp_path(self) -> None:
        assert build_health_url("http://x:9000/api/mcp") == "http://x:9000/api/health"
