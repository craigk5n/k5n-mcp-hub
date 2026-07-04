import pytest

from devhub.mcp.sse import extract_sse_data


class TestExtractSseData:
    def test_extract_json_from_data_line_bytes(self) -> None:
        result = extract_sse_data(b'data: {"x":1}\n\n')
        assert result == b'{"x":1}'

    def test_extract_data_from_event_message_bytes(self) -> None:
        result = extract_sse_data(b"event: message\ndata: hello\n\n")
        assert result == b"hello"

    def test_no_data_substring_returns_none(self) -> None:
        result = extract_sse_data(b'{"plain":"json"}')
        assert result is None

    def test_data_done_returns_none(self) -> None:
        result = extract_sse_data(b"data: [DONE]\n\n")
        assert result is None

    def test_str_input_equivalent_to_bytes(self) -> None:
        result_str = extract_sse_data('data: {"x":1}\n\n')
        result_bytes = extract_sse_data(b'data: {"x":1}\n\n')
        assert result_str == result_bytes

    def test_str_input_no_data_substring(self) -> None:
        result = extract_sse_data('{"plain":"json"}')
        assert result is None

    def test_str_input_data_done(self) -> None:
        result = extract_sse_data("data: [DONE]\n\n")
        assert result is None

    def test_event_message_str_equivalent(self) -> None:
        result_str = extract_sse_data("event: message\ndata: hello\n\n")
        result_bytes = extract_sse_data(b"event: message\ndata: hello\n\n")
        assert result_str == result_bytes
        assert result_str == b"hello"
