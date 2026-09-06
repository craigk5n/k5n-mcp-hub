"""Multi-round-trip tool results (`resultType: "input_required"`) — Story 4.3.

Shape per the 2026-07-28 spec: `inputRequests` is an **object keyed by request id**
(not an array), each entry carrying a `method` and `params`; the retry echoes
`inputResponses` under the same keys plus any `requestState`, and MUST use a
different JSON-RPC id.
"""

from __future__ import annotations

import json

import pytest

from mcp_hub.mcp.mrtr import build_retry_body, parse_input_required

INPUT_REQUIRED = {
    "jsonrpc": "2.0",
    "id": 2,
    "result": {
        "resultType": "input_required",
        "inputRequests": {
            "github_login": {
                "method": "elicitation/create",
                "params": {
                    "mode": "form",
                    "message": "Please provide your GitHub username",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
                },
            }
        },
        "requestState": "eyJsb2NhdGlvbiI6Ik5ldyBZb3JrIn0",
    },
}

ORIGINAL_REQUEST = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {"name": "get_weather", "arguments": {"location": "New York"}},
}


class TestParsing:
    def test_detects_an_input_required_result(self) -> None:
        parsed = parse_input_required(json.dumps(INPUT_REQUIRED))

        assert parsed is not None
        assert parsed.request_state == "eyJsb2NhdGlvbiI6Ik5ldyBZb3JrIn0"

    def test_exposes_each_request_with_its_id(self) -> None:
        parsed = parse_input_required(json.dumps(INPUT_REQUIRED))

        assert parsed is not None
        assert [r.id for r in parsed.requests] == ["github_login"]
        assert parsed.requests[0].message == "Please provide your GitHub username"
        assert parsed.requests[0].method == "elicitation/create"
        assert parsed.requests[0].schema["properties"]["name"]["type"] == "string"

    def test_a_complete_result_is_not_input_required(self) -> None:
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": {"resultType": "complete", "content": []}}
        )

        assert parse_input_required(body) is None

    def test_a_pre_2026_result_without_result_type_is_not_input_required(self) -> None:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": []}})

        assert parse_input_required(body) is None

    @pytest.mark.parametrize("body", ["", "not json", "[]", "null"])
    def test_unparseable_bodies_are_ignored(self, body: str) -> None:
        assert parse_input_required(body) is None

    def test_missing_input_requests_is_ignored(self) -> None:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"resultType": "input_required"}})

        assert parse_input_required(body) is None


class TestRetryBody:
    def test_preserves_the_original_method_and_arguments(self) -> None:
        parsed = parse_input_required(json.dumps(INPUT_REQUIRED))
        assert parsed is not None

        retry = build_retry_body(json.dumps(ORIGINAL_REQUEST), parsed)

        assert retry["method"] == "tools/call"
        assert retry["params"]["name"] == "get_weather"
        assert retry["params"]["arguments"] == {"location": "New York"}

    def test_the_id_must_differ_from_the_original(self) -> None:
        # Spec: "the JSON-RPC id MUST be different between the initial request and
        # the retry." Reusing it is the easy mistake and servers may reject it.
        parsed = parse_input_required(json.dumps(INPUT_REQUIRED))
        assert parsed is not None

        retry = build_retry_body(json.dumps(ORIGINAL_REQUEST), parsed)

        assert retry["id"] != ORIGINAL_REQUEST["id"]

    def test_carries_request_state_back_when_the_server_sent_one(self) -> None:
        parsed = parse_input_required(json.dumps(INPUT_REQUIRED))
        assert parsed is not None

        retry = build_retry_body(json.dumps(ORIGINAL_REQUEST), parsed)

        assert retry["params"]["requestState"] == "eyJsb2NhdGlvbiI6Ik5ldyBZb3JrIn0"

    def test_omits_request_state_when_the_server_sent_none(self) -> None:
        payload = json.loads(json.dumps(INPUT_REQUIRED))
        del payload["result"]["requestState"]
        parsed = parse_input_required(json.dumps(payload))
        assert parsed is not None

        retry = build_retry_body(json.dumps(ORIGINAL_REQUEST), parsed)

        assert "requestState" not in retry["params"]

    def test_scaffolds_one_input_response_per_request_keyed_by_id(self) -> None:
        parsed = parse_input_required(json.dumps(INPUT_REQUIRED))
        assert parsed is not None

        retry = build_retry_body(json.dumps(ORIGINAL_REQUEST), parsed)

        responses = retry["params"]["inputResponses"]
        assert set(responses) == {"github_login"}
        assert responses["github_login"]["action"] == "accept"

    def test_supplied_answers_are_used(self) -> None:
        parsed = parse_input_required(json.dumps(INPUT_REQUIRED))
        assert parsed is not None

        retry = build_retry_body(
            json.dumps(ORIGINAL_REQUEST), parsed, answers={"github_login": {"name": "octocat"}}
        )

        assert retry["params"]["inputResponses"]["github_login"]["content"] == {"name": "octocat"}

    def test_an_unparseable_original_request_yields_nothing(self) -> None:
        parsed = parse_input_required(json.dumps(INPUT_REQUIRED))
        assert parsed is not None

        assert build_retry_body("not json", parsed) is None
