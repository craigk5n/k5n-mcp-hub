"""Load a spec in a child process that can be killed (security review of Story 13.1).

Parsing and validating a spec is hostile-input processing that can go wrong in ways
the hub cannot recover from in-process:

- `openapi-spec-validator` checks a schema's `default` against its `pattern`, and
  Python's backtracking `re` can be made to run indefinitely while holding the GIL.
  Moving it to a thread would stall the event loop just the same.
- The validator is slow on large legal specs (about 19 s per MB, linear).
- The C YAML parser overflows its stack on deep flow nesting. That is a segfault,
  not an exception.

So every untrusted load happens here: a spawned child with a memory limit, killed
when `validate_timeout_seconds` expires. A child that crashes, times out or runs out
of memory becomes a `SpecError`, and the hub carries on.

Fetching stays in the parent, because the SSRF-pinned client and its injected test
doubles live there; only the fetched bytes cross into the child.
"""

from __future__ import annotations

import asyncio
import multiprocessing
from dataclasses import dataclass
from typing import Any

import httpx

from mcp_hub.config import OpenAPIConfig
from mcp_hub.openapi.errors import SpecError
from mcp_hub.openapi.loader import LoadedSpec, fetch_spec_bytes, load_spec_unsandboxed

# Time a killed child gets to be reaped before the parent stops waiting on it.
_REAP_SECONDS = 5


@dataclass(frozen=True)
class SpecLimits:
    max_bytes: int
    fetch_timeout_seconds: float
    validate_timeout_seconds: float
    memory_limit_mb: int

    @classmethod
    def from_config(cls, config: OpenAPIConfig) -> SpecLimits:
        return cls(
            max_bytes=config.max_spec_bytes,
            fetch_timeout_seconds=config.fetch_timeout_seconds,
            validate_timeout_seconds=config.validate_timeout_seconds,
            memory_limit_mb=config.validate_memory_mb,
        )


async def load_spec(data: bytes, *, limits: SpecLimits) -> LoadedSpec:
    """Parse, check and validate untrusted spec bytes in a killable child."""
    if len(data) > limits.max_bytes:
        # Cheap and certain, so refused here without paying for a process.
        raise SpecError(
            f"Spec is too large: the limit is {limits.max_bytes} bytes (openapi.max_spec_bytes)"
        )
    return await asyncio.to_thread(_load_in_child, data, limits)


async def load_spec_from_url(
    url: str,
    *,
    limits: SpecLimits,
    allow_private_networks: bool,
    http_client: httpx.AsyncClient | None = None,
) -> LoadedSpec:
    data = await fetch_spec_bytes(
        url,
        max_bytes=limits.max_bytes,
        timeout_seconds=limits.fetch_timeout_seconds,
        allow_private_networks=allow_private_networks,
        http_client=http_client,
    )
    return await load_spec(data, limits=limits)


def _load_in_child(data: bytes, limits: SpecLimits) -> LoadedSpec:
    # spawn, not fork: a fork of a running server copies its threads' locks in
    # whatever state they were in, and its sockets.
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(
        target=_child_main,
        args=(sender, data, limits.max_bytes, limits.memory_limit_mb),
        name="k5n-mcp-hub-openapi-load",
        daemon=True,
    )
    child.start()
    sender.close()  # so a dead child reads as EOF here, not a hang
    try:
        if not receiver.poll(limits.validate_timeout_seconds):
            raise SpecError(
                f"Loading the spec took longer than {limits.validate_timeout_seconds:g}s and "
                "was stopped (openapi.validate_timeout_seconds)"
            )
        try:
            outcome = receiver.recv()
        except EOFError:
            child.join(_REAP_SECONDS)
            raise SpecError(_crash_message(child.exitcode)) from None
    finally:
        receiver.close()
        if child.is_alive():
            child.kill()
        child.join(_REAP_SECONDS)
    return _from_outcome(outcome)


def _crash_message(exitcode: int | None) -> str:
    how = f"signal {-exitcode}" if exitcode is not None and exitcode < 0 else f"exit {exitcode}"
    return (
        f"The spec parser crashed ({how}) and the spec was refused. Very deeply nested "
        "YAML is the usual cause"
    )


def _from_outcome(outcome: tuple[Any, ...]) -> LoadedSpec:
    if outcome[0] == "ok":
        _, document, dialect, sha256 = outcome
        return LoadedSpec(document=document, dialect=dialect, sha256=sha256)
    _, message, pointer = outcome
    raise SpecError(message, pointer=pointer)


# --------------------------------------------------------------------------- child side


def _child_main(sender: Any, data: bytes, max_bytes: int, memory_limit_mb: int) -> None:
    """Runs in the child. Always answers, so the parent never waits for a timeout on
    an ordinary failure."""
    try:
        _limit_memory(memory_limit_mb)
        spec = load_spec_unsandboxed(data, max_bytes=max_bytes)
        outcome: tuple[Any, ...] = ("ok", spec.document, spec.dialect, spec.sha256)
    except SpecError as exc:
        outcome = ("error", exc.message, exc.pointer)
    except MemoryError:
        outcome = (
            "error",
            f"Loading the spec needed more than {memory_limit_mb} MB of memory "
            "(openapi.validate_memory_mb)",
            "",
        )
    except Exception as exc:  # any other failure is still an answer, not a hang
        outcome = ("error", f"Spec could not be loaded: {type(exc).__name__}: {exc}", "")
    try:
        sender.send(outcome)
    except MemoryError:
        sender.send(
            ("error", "Loading the spec ran out of memory (openapi.validate_memory_mb)", "")
        )
    finally:
        sender.close()


def _limit_memory(memory_limit_mb: int) -> None:
    try:
        import resource
    except ImportError:  # pragma: no cover -- not POSIX; the timeout still applies
        return
    limit = memory_limit_mb * 1024 * 1024
    _, hard = resource.getrlimit(resource.RLIMIT_AS)
    if hard != resource.RLIM_INFINITY:
        limit = min(limit, hard)
    resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
