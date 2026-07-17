import pytest
from mcp_hub.proxy import compose_backend_url


class TestComposeBackendUrl:
    def test_bare_mcp_path_preserves_registered_url(self):
        """When the incoming path is exactly `/mcp`, the registered server URL IS the
        target and is sent verbatim — a registered `.../mcp` must stay `.../mcp` so
        hosted servers like api.x.com (which don't redirect to a trailing slash) work."""
        result = compose_backend_url(
            server_url="http://x/mcp",
            incoming_path="/mcp",
        )
        assert result == "http://x/mcp"

    def test_bare_mcp_path_preserves_registered_trailing_slash(self):
        """A registered `.../mcp/` must stay `.../mcp/` so SDK/Starlette-mounted servers
        (which 307-redirect `/mcp`→`/mcp/`, and we don't follow redirects) work. The
        operator disambiguates by how they register the URL."""
        result = compose_backend_url(
            server_url="http://x/mcp/",
            incoming_path="/mcp",
        )
        assert result == "http://x/mcp/"

    def test_path_with_session_appends_remaining_path(self):
        result = compose_backend_url(
            server_url="http://x/mcp",
            incoming_path="/mcp/abc",
        )
        assert result == "http://x/mcp/abc"

    def test_path_mcp_with_server_being_root(self):
        result = compose_backend_url(
            server_url="http://x/",
            incoming_path="/mcp",
        )
        assert result == "http://x/"

    def test_query_string_is_appended(self):
        result = compose_backend_url(
            server_url="http://x/mcp",
            incoming_path="/mcp",
            incoming_query="foo=bar",
        )
        assert result == "http://x/mcp?foo=bar"

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

    def test_no_double_slashes_when_server_has_trailing_slash(self):
        result = compose_backend_url(
            server_url="http://x/mcp/",
            incoming_path="/mcp/abc",
        )
        assert result == "http://x/mcp/abc"
