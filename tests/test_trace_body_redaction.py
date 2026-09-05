"""Credential redaction inside captured trace bodies (Story 7.2).

Header redaction has been in place for a while; bodies were not covered. OBO makes
that urgent -- an IdP or backend error body routinely echoes token material, and the
trace UI is readable by anyone who can reach the admin UI.
"""

from __future__ import annotations

import json

from mcp_hub.trace.recorder import TraceEntry, TraceRecorder, sanitize_trace_body
from mcp_hub.trace import Entry, utcnow


class TestJSONBodies:
    def test_oauth_token_response_is_redacted(self) -> None:
        body = json.dumps(
            {"access_token": "super-secret", "refresh_token": "also-secret", "expires_in": 300}
        )

        result = json.loads(sanitize_trace_body(body))

        assert result["access_token"] == "****"
        assert result["refresh_token"] == "****"
        # Non-credential fields stay readable, or the trace stops being useful.
        assert result["expires_in"] == 300

    def test_exchange_request_fields_are_redacted(self) -> None:
        body = json.dumps({"subject_token": "alice-token", "actor_token": "hub-token"})

        result = json.loads(sanitize_trace_body(body))

        assert result["subject_token"] == "****"
        assert result["actor_token"] == "****"

    def test_nested_credentials_are_found(self) -> None:
        body = json.dumps({"result": {"credentials": {"client_secret": "shh"}}})

        result = json.loads(sanitize_trace_body(body))

        assert result["result"]["credentials"]["client_secret"] == "****"

    def test_credentials_inside_arrays_are_found(self) -> None:
        body = json.dumps({"items": [{"id_token": "one"}, {"id_token": "two"}]})

        result = json.loads(sanitize_trace_body(body))

        assert [item["id_token"] for item in result["items"]] == ["****", "****"]

    def test_ordinary_bodies_are_untouched(self) -> None:
        body = json.dumps({"jsonrpc": "2.0", "result": {"tools": [{"name": "search"}]}})

        assert json.loads(sanitize_trace_body(body)) == json.loads(body)


class TestFormEncodedBodies:
    def test_token_exchange_form_is_redacted(self) -> None:
        body = (
            "grant_type=urn:ietf:params:oauth:grant-type:token-exchange"
            "&subject_token=alice-token&client_secret=hub-secret&audience=files"
        )

        result = sanitize_trace_body(body)

        assert "alice-token" not in result
        assert "hub-secret" not in result
        # The non-secret fields are what make the trace worth reading.
        assert "audience=files" in result
        assert "grant_type=" in result


class TestTruncatedAndMalformedBodies:
    def test_a_truncated_json_body_is_still_redacted(self) -> None:
        # The proxy truncates before the trace is built, so the common case is a body
        # that no longer parses. A structured-only redactor would miss exactly this.
        body = '{"access_token": "super-secret-value", "refresh_tok'

        result = sanitize_trace_body(body)

        assert "super-secret-value" not in result

    def test_non_json_text_with_a_token_is_redacted(self) -> None:
        body = 'error: bad token access_token="leaked-value" at line 3'

        assert "leaked-value" not in sanitize_trace_body(body)

    def test_plain_text_without_credentials_is_unchanged(self) -> None:
        body = "Internal Server Error"

        assert sanitize_trace_body(body) == body

    def test_empty_body_is_unchanged(self) -> None:
        assert sanitize_trace_body("") == ""
        assert sanitize_trace_body(b"") == b""


class TestBytesBodies:
    def test_bytes_in_bytes_out(self) -> None:
        body = json.dumps({"access_token": "secret"}).encode()

        result = sanitize_trace_body(body)

        assert isinstance(result, bytes)
        assert b"secret" not in result

    def test_undecodable_bytes_are_returned_unchanged(self) -> None:
        body = b"\xff\xfe\x00binary"

        assert sanitize_trace_body(body) == body


class TestRecorderAppliesIt:
    def test_str_entries_are_redacted_on_add(self) -> None:
        recorder = TraceRecorder()
        recorder.add(
            TraceEntry(
                timestamp=utcnow(),
                server_id="files",
                operation="proxy",
                response_body=json.dumps({"access_token": "leaked"}),
            )
        )

        assert "leaked" not in recorder.list("files")[0].response_body

    def test_bytes_entries_are_redacted_on_add(self) -> None:
        # The proxy path uses the bytes-bodied Entry rather than TraceEntry.
        recorder = TraceRecorder()
        recorder.add(
            Entry(
                timestamp=utcnow(),
                server_id="files",
                operation="proxy",
                http_method="POST",
                url="http://hub/mcp",
                outbound_url="http://backend/mcp",
                status=200,
                duration_ms=1.0,
                error="",
                response_body=json.dumps({"access_token": "leaked"}).encode(),
            )
        )

        assert b"leaked" not in recorder.list("files")[0].response_body

    def test_the_callers_entry_is_not_mutated(self) -> None:
        recorder = TraceRecorder()
        entry = TraceEntry(
            timestamp=utcnow(),
            server_id="files",
            operation="proxy",
            response_body=json.dumps({"access_token": "leaked"}),
        )

        recorder.add(entry)

        assert "leaked" in entry.response_body
