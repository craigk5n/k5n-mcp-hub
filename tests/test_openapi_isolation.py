"""Loading a spec in a killable child process (security review of Story 13.1).

`openapi-spec-validator` can be made to run indefinitely (catastrophic regex
backtracking via `default` + `pattern`), is slow on large legal specs, and the C
YAML parser can overflow its stack on deep nesting. A regex hang also holds the GIL,
so a thread would stall the whole hub. Each load therefore runs in a spawned child
with a wall-clock timeout and a memory limit, and whatever goes wrong there comes
back as a `SpecError`.

The bounds tests at the bottom go through the child on purpose. With a protection
removed they would hit the timeout and fail on the *message*, instead of hanging
the test run the way an in-process version does.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import pathlib
import signal
import time
from typing import Any

import httpx
import pytest

from mcp_hub.config import OpenAPIConfig
from mcp_hub.openapi import isolation
from mcp_hub.openapi.errors import SpecError
from mcp_hub.openapi.isolation import SpecLimits, load_spec, load_spec_from_url
from mcp_hub.openapi.loader import load_spec_unsandboxed

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "openapi"


def _segfault(*_: Any) -> None:
    """A stand-in for the child's entry point that dies the way a C parser would."""
    os.kill(os.getpid(), signal.SIGSEGV)


HEADER = b"openapi: 3.1.0\ninfo: {title: t, version: '1'}\npaths: {}\n"


def _limits(**overrides: Any) -> SpecLimits:
    values: dict[str, Any] = {
        "max_bytes": 5 * 1024 * 1024,
        "fetch_timeout_seconds": 5,
        "validate_timeout_seconds": 30,
        "memory_limit_mb": 1024,
    }
    values.update(overrides)
    return SpecLimits(**values)


async def _error(data: bytes, **limits: Any) -> SpecError:
    with pytest.raises(SpecError) as excinfo:
        await load_spec(data, limits=_limits(**limits))
    return excinfo.value


def _redos_spec() -> bytes:
    # The validator checks `default` against `pattern`; `(a+)+$` against 40 a's and a
    # mismatch is exponential backtracking.
    doc = {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1"},
        "paths": {},
        "components": {
            "schemas": {"S": {"type": "string", "pattern": "^(a+)+$", "default": "a" * 40 + "!"}}
        },
    }
    return json.dumps(doc).encode()


class TestLimits:
    def test_from_config(self):
        limits = SpecLimits.from_config(OpenAPIConfig())
        assert limits == SpecLimits(
            max_bytes=5 * 1024 * 1024,
            fetch_timeout_seconds=30,
            validate_timeout_seconds=120,
            memory_limit_mb=1024,
        )


class TestIsolatedLoad:
    async def test_result_matches_an_in_process_load(self):
        data = (FIXTURES / "petstore-3.1.json").read_bytes()
        isolated = await load_spec(data, limits=_limits())
        direct = load_spec_unsandboxed(data, max_bytes=5 * 1024 * 1024)
        assert isolated == direct

    async def test_spec_error_comes_back_with_its_pointer(self):
        doc = {"openapi": "3.1.0", "info": {"title": 1, "version": "1"}, "paths": {}}
        err = await _error(json.dumps(doc).encode())
        assert err.pointer == "/info/title"

    async def test_size_cap_applies_before_any_child_is_started(self, monkeypatch):
        started: list[Any] = []
        monkeypatch.setattr(
            multiprocessing.get_context("spawn"), "Process", lambda *a, **k: started.append(1)
        )
        err = await _error(b"{}" * 100, max_bytes=10)
        assert "too large" in err.message.lower()
        assert started == []

    async def test_a_hang_is_stopped_at_the_timeout(self):
        start = time.monotonic()
        err = await _error(_redos_spec(), validate_timeout_seconds=2)
        assert time.monotonic() - start < 10
        assert "longer than 2" in err.message
        assert "openapi.validate_timeout_seconds" in err.message

    async def test_the_event_loop_keeps_running_during_a_hang(self):
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        task = asyncio.create_task(ticker())
        try:
            await _error(_redos_spec(), validate_timeout_seconds=2)
        finally:
            task.cancel()
        # ~40 ticks in 2 s if the loop ran freely; a GIL-holding regex in a thread
        # would starve it.
        assert ticks > 20

    async def test_no_child_is_left_behind(self):
        await _error(_redos_spec(), validate_timeout_seconds=1)
        assert multiprocessing.active_children() == []

    async def test_deeply_nested_yaml_is_refused(self):
        # 30,000 levels of flow mappings can overflow the C YAML parser's stack: a
        # segfault in-process, which would take the hub down with it. Whether it does
        # depends on the machine's stack size (it does not on GitHub's runners, where
        # the depth limit refuses it instead), so either refusal passes.
        deep = b"x-a: " + b"{a: " * 30_000 + b"1" + b"}" * 30_000 + b"\n"
        err = await _error(HEADER + deep)
        message = err.message.lower()
        assert "crashed" in message or "nested too deeply" in message, err.message

    async def test_a_crashing_parser_is_a_spec_error(self, monkeypatch):
        # Spawn sends the target by reference, so the child imports this module and
        # runs the stand-in: a real segfault, on every machine.
        monkeypatch.setattr(isolation, "_child_main", _segfault)
        err = await _error(HEADER)
        assert "crashed" in err.message.lower()
        assert f"signal {int(signal.SIGSEGV)}" in err.message
        assert multiprocessing.active_children() == []

    async def test_memory_limit(self):
        # A 40 MB string needs well over 100 MB to parse, check and hash.
        doc = b'{"openapi": "3.1.0", "info": {"title": "t", "version": "1"}, "paths": {}, "x": "'
        data = doc + b"a" * 40_000_000 + b'"}'
        err = await _error(data, max_bytes=100_000_000, memory_limit_mb=150)
        assert "memory" in err.message.lower()
        assert "openapi.validate_memory_mb" in err.message


class TestFromUrl:
    async def test_end_to_end(self):
        body = (FIXTURES / "petstore-3.0.yaml").read_bytes()

        async def chunks():
            yield body

        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=chunks()))
        async with httpx.AsyncClient(transport=transport) as client:
            spec = await load_spec_from_url(
                "https://api.example.com/openapi.yaml",
                limits=_limits(),
                allow_private_networks=False,
                http_client=client,
            )
        assert spec.dialect == "3.0"


def _alias_bomb() -> bytes:
    lines = ["x-a0: &a0 lol"]
    for i in range(1, 10):
        lines.append(f"x-a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 10)}]")
    return HEADER + "\n".join(lines).encode()


def _merge_bomb() -> bytes:
    lines = ["x-m0: &m0 {k: 1}"]
    for i in range(1, 30):
        lines.append(f"x-m{i}: &m{i} {{<<: [{', '.join([f'*m{i - 1}'] * 5)}]}}")
    return HEADER + "\n".join(lines).encode()


def _fan_out() -> bytes:
    schemas: dict[str, Any] = {
        f"A{i}": {"allOf": [{"$ref": f"#/components/schemas/A{i + 1}"}] * 2} for i in range(40)
    }
    schemas["A40"] = {"type": "string"}
    doc = {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1"},
        "paths": {},
        "components": {"schemas": schemas},
    }
    return json.dumps(doc).encode()


class TestBoundsFailByAssertion:
    """Each bound, through the child, with a timeout short enough that a missing
    protection surfaces as the wrong message rather than a hung run."""

    @pytest.mark.parametrize(
        "data, expected",
        [
            (_alias_bomb(), "too large once expanded"),
            (_merge_bomb(), None),  # accepted: merging is linear now
            (_fan_out(), "expands to more than"),
            (HEADER + b"x: " + b"[" * 5_000 + b"]" * 5_000 + b"\n", "nested too deeply"),
            (HEADER + b"x: &a [*a]\n", "nested too deeply"),
        ],
        ids=["alias-bomb", "merge-bomb", "ref-fan-out", "deep-nesting", "self-containing"],
    )
    async def test_bound(self, data, expected):
        limits = _limits(validate_timeout_seconds=10)
        if expected is None:
            spec = await load_spec(data, limits=limits)
            assert spec.document["x-m29"] == {"k": 1}
            return
        with pytest.raises(SpecError) as excinfo:
            await load_spec(data, limits=limits)
        assert expected in excinfo.value.message


class TestChildReporting:
    """The child's side, run in-process with a fake pipe so coverage sees it. The
    memory limit is stubbed out: applied here it would limit the test runner."""

    class _Pipe:
        def __init__(self, fail_first_send: bool = False) -> None:
            self.sent: list[Any] = []
            self.closed = False
            self._fail = fail_first_send

        def send(self, outcome: Any) -> None:
            if self._fail:
                self._fail = False
                raise MemoryError
            self.sent.append(outcome)

        def close(self) -> None:
            self.closed = True

    @pytest.fixture(autouse=True)
    def _no_memory_limit(self, monkeypatch):
        import mcp_hub.openapi.isolation as isolation

        monkeypatch.setattr(isolation, "_limit_memory", lambda mb: None)

    def _run(self, data: bytes, pipe: Any = None) -> Any:
        import mcp_hub.openapi.isolation as isolation

        pipe = pipe or self._Pipe()
        isolation._child_main(pipe, data, 5 * 1024 * 1024, 1024)
        assert pipe.closed
        return pipe.sent

    def test_success(self):
        sent = self._run((FIXTURES / "petstore-3.1.json").read_bytes())
        assert sent[0][0] == "ok" and sent[0][2] == "3.1"

    def test_spec_error(self):
        assert self._run(b"[]")[0] == (
            "error",
            "Spec must be an object (a mapping) at the top level",
            "",
        )

    def test_memory_error(self, monkeypatch):
        import mcp_hub.openapi.isolation as isolation

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise MemoryError

        monkeypatch.setattr(isolation, "load_spec_unsandboxed", boom)
        kind, message, _ = self._run(b"{}")[0]
        assert kind == "error" and "openapi.validate_memory_mb" in message

    def test_unexpected_exception_is_still_an_answer(self, monkeypatch):
        import mcp_hub.openapi.isolation as isolation

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise KeyError("surprise")

        monkeypatch.setattr(isolation, "load_spec_unsandboxed", boom)
        kind, message, _ = self._run(b"{}")[0]
        assert kind == "error" and "KeyError" in message

    def test_out_of_memory_while_answering(self):
        pipe = self._Pipe(fail_first_send=True)
        sent = self._run((FIXTURES / "petstore-3.1.json").read_bytes(), pipe=pipe)
        assert sent[0][0] == "error" and "memory" in sent[0][1]

    def test_crash_message_names_the_signal(self):
        from mcp_hub.openapi.isolation import _crash_message

        assert "signal 11" in _crash_message(-11)
        assert "exit 3" in _crash_message(3)
