"""Validated tool arguments to the parts of an HTTP request (Story 13.5, ADR 0011 §6).

Pure: no I/O, no credentials. The executor adds those afterwards, so no argument can
override them.

- **Path** values are percent-encoded as one segment each (`/`, `?`, `#` and `%`
  included), and a value of `.` or `..` is encoded as `%2E` so it cannot act as a
  dot segment. An empty value for a path parameter is refused rather than producing
  `//`.
- **Query** parameters use `form` style: arrays repeat the key when `explode`, and
  are comma-joined otherwise. Mapping refuses every other style.
- **Headers** refuse control characters, so a value cannot inject another header.
- **Body** is JSON or form-urlencoded, with `Content-Type` from the plan, never from
  an argument (mapping drops a declared `Content-Type` parameter).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from mcp_hub.openapi.arguments import FORM_MEDIA
from mcp_hub.openapi.model import ParameterPlan, RequestPlan

_PATH_VARIABLE = re.compile(r"\{([^}]+)\}")
# Characters a spec's literal path text may keep as is (RFC 3986 pchar plus "/").
_LITERAL_SAFE = "/:@!$&'()*+,;=-._~%"


class ArgumentProblem(Exception):
    """The arguments passed schema validation but cannot be sent as given."""


@dataclass(frozen=True)
class PreparedRequest:
    method: str
    path: str
    query: tuple[tuple[str, str], ...]
    headers: tuple[tuple[str, str], ...]
    body: bytes | None
    content_type: str | None


def build_request(plan: RequestPlan, arguments: dict[str, Any]) -> PreparedRequest:
    by_name: dict[tuple[str, str], ParameterPlan] = {
        (p.location, p.name): p for p in plan.parameters
    }
    query: list[tuple[str, str]] = []
    headers: list[tuple[str, str]] = []
    for param in plan.parameters:
        if param.argument not in arguments or arguments[param.argument] is None:
            continue
        value = arguments[param.argument]
        if param.location == "query":
            query.extend(_query_pairs(param, value))
        elif param.location == "header":
            headers.append((param.name, _header_value(param, value)))
    path = _path(plan.path, by_name, arguments)
    body, content_type = _body(plan, arguments)
    return PreparedRequest(plan.method, path, tuple(query), tuple(headers), body, content_type)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return str(value)


def _segment(value: Any) -> str:
    encoded = quote(_scalar(value), safe="")
    return encoded.replace(".", "%2E") if encoded in (".", "..") else encoded


def _path(
    template: str, by_name: dict[tuple[str, str], ParameterPlan], arguments: dict[str, Any]
) -> str:
    out: list[str] = []
    position = 0
    for match in _PATH_VARIABLE.finditer(template):
        out.append(quote(template[position : match.start()], safe=_LITERAL_SAFE))
        param = by_name[("path", match.group(1))]
        value = arguments.get(param.argument)
        if value is None or value == "" or value == []:
            raise ArgumentProblem(f"path parameter {param.name!r} must not be empty")
        if isinstance(value, list):
            out.append(",".join(_segment(item) for item in value))
        else:
            out.append(_segment(value))
        position = match.end()
    out.append(quote(template[position:], safe=_LITERAL_SAFE))
    return "".join(out)


def _query_pairs(param: ParameterPlan, value: Any) -> list[tuple[str, str]]:
    if isinstance(value, list):
        if not value:
            return []
        if param.explode:
            return [(param.name, _scalar(item)) for item in value]
        return [(param.name, ",".join(_scalar(item) for item in value))]
    return [(param.name, _scalar(value))]


def _header_value(param: ParameterPlan, value: Any) -> str:
    text = ",".join(_scalar(v) for v in value) if isinstance(value, list) else _scalar(value)
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise ArgumentProblem(f"header {param.name!r} must not contain control characters")
    return text


def _body(plan: RequestPlan, arguments: dict[str, Any]) -> tuple[bytes | None, str | None]:
    body = plan.body
    if body is None:
        return None, None
    if body.mode == "whole":
        ((argument, _),) = body.fields
        if argument not in arguments:
            return None, None
        payload: Any = arguments[argument]
    else:
        payload = {prop: arguments[arg] for arg, prop in body.fields if arg in arguments}
        if not payload and not body.required:
            return None, None
    if body.media_type == FORM_MEDIA:
        return _form(payload).encode("ascii"), body.media_type
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return encoded, body.media_type


def _form(payload: dict[str, Any]) -> str:
    pairs: list[tuple[str, str]] = []
    for name, value in payload.items():
        if isinstance(value, list):
            pairs.extend((name, _scalar(item)) for item in value)
        else:
            pairs.append((name, _scalar(value)))
    return urlencode(pairs)
