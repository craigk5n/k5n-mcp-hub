"""`$ref` checks: every reference stays inside the document, resolves, and terminates.

This is a security boundary, not tidiness. `openapi-spec-validator` resolves refs
itself: given an ``http://`` ref it performs the fetch (outside the SSRF-pinned
transport), and given a ``file://`` ref it reads the file and can echo the contents
in its error message. So nothing reaches the validator until every ref has been shown
to be a local JSON pointer (ADR 0011 §11).

Refs are checked and resolved here but left in place in the stored document: inlining
would let one recursive or heavily reused schema multiply the snapshot (ADR 0011 §2).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import unquote

from mcp_hub.openapi.errors import SpecError

# Keywords that change how a ref is resolved. `$id` re-bases relative refs beneath it
# onto whatever URI it names; the dynamic forms resolve at validation time. None of
# them belongs in a self-contained API description.
_REBASING_KEYWORDS = ("$id", "$dynamicRef", "$recursiveRef")

# Keywords that apply a subschema to the *same* instance. A ref cycle through these
# never consumes any structure, so a validator following it recurses forever. A cycle
# through `properties`/`items` is an ordinary recursive type and is fine.
_SAME_INSTANCE_LISTS = ("allOf", "anyOf", "oneOf")
_SAME_INSTANCE_SCHEMAS = ("not", "if", "then", "else")

# How many schemas one ref may expand to through allOf/anyOf/oneOf/not chains. The
# graph is acyclic by then, but branching makes it exponential: 40 levels of two refs
# each is 2**40, which the validator walks in full and never finishes (security
# review finding 3). Real specs compose a handful of schemas, so this is generous.
MAX_COMPOSITION_EXPANSION = 10_000

Pointer = tuple[str, ...]


def encode_pointer(parts: Iterable[Any]) -> str:
    """RFC 6901: ``("paths", "/pets/{id}")`` -> ``/paths/~1pets~1{id}``."""
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def decode_pointer(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError(f"not a JSON pointer: {pointer!r}")
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def check_refs(document: dict[str, Any]) -> None:
    """Raise `SpecError` unless every ref is local, resolvable, and cycle-free."""
    sites = _collect_ref_sites(document)
    targets: dict[Pointer, dict[str, Any]] = {}
    for path, ref in sites:
        pointer = _local_pointer(ref, path)
        if pointer not in targets:
            targets[pointer] = _resolve(document, pointer, ref, path)
    _check_cycles(targets)


def _collect_ref_sites(document: dict[str, Any]) -> list[tuple[tuple[Any, ...], str]]:
    sites: list[tuple[tuple[Any, ...], str]] = []
    stack: list[tuple[Any, tuple[Any, ...]]] = [(document, ())]
    while stack:
        value, path = stack.pop()
        if isinstance(value, dict):
            for keyword in _REBASING_KEYWORDS:
                if isinstance(value.get(keyword), str):
                    raise SpecError(
                        f"{keyword} is not supported in an imported spec: it changes where "
                        f"references resolve ({value[keyword]!r})",
                        pointer=encode_pointer((*path, keyword)),
                    )
            # Only a string `$ref` is a reference. `properties: {"$ref": {...}}` is a
            # property that happens to be called "$ref".
            if isinstance(value.get("$ref"), str):
                sites.append((path, value["$ref"]))
            stack.extend((child, (*path, key)) for key, child in value.items())
        elif isinstance(value, list):
            stack.extend((child, (*path, index)) for index, child in enumerate(value))
    return sites


def _local_pointer(ref: str, path: tuple[Any, ...]) -> Pointer:
    if not ref.startswith("#/"):
        raise SpecError(
            f"$ref {ref!r} is external or not a JSON pointer. Imported specs must be "
            "self-contained: only local refs of the form '#/...' are supported",
            pointer=encode_pointer((*path, "$ref")),
        )
    return tuple(decode_pointer(unquote(ref[1:])))


def _resolve(
    document: dict[str, Any], pointer: Pointer, ref: str, path: tuple[Any, ...]
) -> dict[str, Any]:
    node: Any = document
    for part in pointer:
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            raise SpecError(
                f"$ref {ref!r} points at nothing in this document",
                pointer=encode_pointer((*path, "$ref")),
            )
    if not isinstance(node, dict):
        raise SpecError(
            f"$ref {ref!r} must point at an object", pointer=encode_pointer((*path, "$ref"))
        )
    return node


def _same_instance_refs(schema: dict[str, Any]) -> list[Pointer]:
    """Refs reachable from `schema` without descending into any part of the instance."""
    found: list[Pointer] = []
    stack: list[Any] = [schema]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        ref = node.get("$ref")
        if isinstance(ref, str):
            found.append(tuple(decode_pointer(unquote(ref[1:]))))
        for keyword in _SAME_INSTANCE_LISTS:
            if isinstance(node.get(keyword), list):
                stack.extend(node[keyword])
        stack.extend(node[keyword] for keyword in _SAME_INSTANCE_SCHEMAS if keyword in node)
    return found


def _check_cycles(targets: dict[Pointer, dict[str, Any]]) -> None:
    edges = {pointer: _same_instance_refs(schema) for pointer, schema in targets.items()}
    # Schemas each target expands to, itself included. Filled as the DFS finishes a
    # node, when every successor is already finished, so it costs one pass.
    expansion: dict[Pointer, int] = {}
    done: set[Pointer] = set()
    for start in edges:
        if start in done:
            continue
        # Iterative DFS: a long ref chain must not become a RecursionError here, which
        # is the very failure this check exists to replace.
        trail: list[Pointer] = [start]
        on_trail = {start}
        pending = [iter(edges[start])]
        while pending:
            successor = next(pending[-1], None)
            if successor is None:
                pending.pop()
                finished = trail.pop()
                _record_expansion(finished, edges, expansion)
                done.add(finished)
                on_trail.discard(finished)
                continue
            if successor in on_trail:
                cycle = trail[trail.index(successor) :] + [successor]
                raise SpecError(
                    "$ref cycle with no structure between the steps, which can never be "
                    "resolved: " + " -> ".join("#" + encode_pointer(p) for p in cycle)
                )
            if successor in done or successor not in edges:
                continue
            trail.append(successor)
            on_trail.add(successor)
            pending.append(iter(edges[successor]))


def _record_expansion(
    pointer: Pointer, edges: dict[Pointer, list[Pointer]], expansion: dict[Pointer, int]
) -> None:
    total = 1 + sum(expansion.get(successor, 1) for successor in edges[pointer])
    if total > MAX_COMPOSITION_EXPANSION:
        raise SpecError(
            f"$ref {'#' + encode_pointer(pointer)!r} expands to more than "
            f"{MAX_COMPOSITION_EXPANSION} schemas through allOf/anyOf/oneOf/not, "
            "which is too many to validate"
        )
    expansion[pointer] = total
