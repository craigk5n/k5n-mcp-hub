import pytest
from devhub.proxy import compose_backend_url


class TestComposeBackendUrl:
    def test_path_equals_mcp_appends_trailing_slash(self):
        result = compose_backend_url(
            server_url="http://x/mcp",
            incoming_path="/mcp",
        )
        assert result == "http://x/mcp/"

    def test_path_with_session_appends_remaining_path(self):
        result = compose_backend_url(
            server_url="http://x/mcp",
            incoming_path="/mcp/abc",
        )
        assert result == "http://x/mcp/abc"

    def test_path_mcp_with_server_having_trailing_slash(self):
        result = compose_backend_url(
            server_url="http://x/",
            incoming_path="/mcp",
        )
        assert result == "http://x"

    def test_query_string_is_appended(self):
        result = compose_backend_url(
            server_url="http://x/mcp",
            incoming_path="/mcp",
            incoming_query="foo=bar",
        )
        assert result == "http://x/mcp/?foo=bar"

    def test_query_string_with_question_mark(self):
        result = compose_backend_url(
            server_url="http://x/mcp",
            incoming_path="/mcp/abc",
            incoming_query="?foo=bar",
        )
        assert result == "http://x/mcp/abc?foo=bar"

    def test_no_double_slashes(self):
        result = compose_backend_url(
            server_url="http://x/mcp",
            incoming_path="/mcp/",
        )
        assert result == "http://x/mcp/"

    def test_empty_rel_no_trailing_slash(self):
        result = compose_backend_url(
            server_url="http://x/mcp/",
            incoming_path="/mcp",
        )
        assert result == "http://x/mcp/"
