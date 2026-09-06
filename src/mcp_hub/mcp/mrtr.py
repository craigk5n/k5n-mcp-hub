from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from mcp_hub.mcp.constants import RESULT_TYPE_INPUT_REQUIRED

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InputRequest:
    """One thing the server needs before it can finish the call.

    ``id`` is the key it arrived under in ``inputRequests``; the retry must answer
    under exactly that key.
    """

    id: str
    method: str = ""
    message: str = ""
    mode: str = ""
    schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InputRequired:
    requests: list[InputRequest]
    # Opaque server state. Echoed back verbatim on the retry when present; it is how
    # the server reassociates the retry with the original call.
    request_state: str = ""


def parse_input_required(response_body: str) -> InputRequired | None:
    """Read a `resultType: "input_required"` response, or return None.

    Returns None for anything else — a complete result, a pre-2026 result with no
    ``resultType`` at all, an error, or an unparseable body — so callers can treat
    this as a simple "is this MRTR?" test.
    """
    try:
        payload = json.loads(response_body)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    if result.get("resultType") != RESULT_TYPE_INPUT_REQUIRED:
        return None

    raw_requests = result.get("inputRequests")
    if not isinstance(raw_requests, dict) or not raw_requests:
        # `input_required` with nothing to ask for is not actionable.
        logger.info("input_required result carried no usable inputRequests")
        return None

    requests: list[InputRequest] = []
    for request_id, entry in raw_requests.items():
        raw_params = entry.get("params") if isinstance(entry, dict) else None
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
        raw_schema = params.get("requestedSchema")
        schema: dict[str, Any] = raw_schema if isinstance(raw_schema, dict) else {}
        requests.append(
            InputRequest(
                id=str(request_id),
                method=str(entry.get("method", "")) if isinstance(entry, dict) else "",
                message=str(params.get("message", "")),
                mode=str(params.get("mode", "")),
                schema=schema,
            )
        )

    state = result.get("requestState")
    return InputRequired(
        requests=requests,
        request_state=state if isinstance(state, str) else "",
    )


def build_retry_body(
    original_request: str,
    input_required: InputRequired,
    answers: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build the retry for an `input_required` result.

    Keeps the original method and params, adds ``inputResponses`` keyed by request
    id, echoes ``requestState`` when the server sent one, and assigns a **new**
    JSON-RPC id — the spec requires the retry's id to differ from the original's, and
    reusing it is the easy mistake.
    """
    try:
        payload = json.loads(original_request)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    retry = dict(payload)
    retry["id"] = _next_id(payload.get("id"))

    params = dict(retry.get("params") or {})
    answers = answers or {}
    params["inputResponses"] = {
        request.id: {"action": "accept", "content": answers.get(request.id, {})}
        for request in input_required.requests
    }
    if input_required.request_state:
        params["requestState"] = input_required.request_state
    retry["params"] = params

    return retry


def _next_id(original: Any) -> Any:
    """A JSON-RPC id guaranteed to differ from the original."""
    if isinstance(original, int) and not isinstance(original, bool):
        return original + 1
    if isinstance(original, str) and original:
        return f"{original}-retry"
    return 1
