"""Shutdown must be best-effort: a straggling background task cannot fail the app."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from mcp_hub.app import _cancel_and_await_tasks


@pytest.mark.asyncio
async def test_shutdown_survives_a_task_that_is_slow_to_die() -> None:
    """A task slow to unwind must not turn shutdown into an exception.

    Regression: `_cancel_and_await_tasks` used `wait_for(gather(...))`, which on
    timeout cancels the gather and then *waits for that cancellation to finish* --
    so a task slow to unwind delayed shutdown and then failed it with TimeoutError.
    Reachable in practice: a discovery or health task blocked in `getaddrinfo` runs
    in a thread executor and cannot be interrupted promptly. It surfaced as a CI
    failure on 3.11 only (run 34049214094), because whether it trips is a matter of
    DNS timing.
    """
    released = asyncio.Event()

    async def slow_to_die() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # A tail that cancellation cannot interrupt, as a thread-blocked call has.
            await asyncio.shield(asyncio.sleep(0.5))
            released.set()
            raise

    task: asyncio.Task[Any] = asyncio.create_task(slow_to_die())
    await asyncio.sleep(0)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await _cancel_and_await_tasks([task], 0.1)
    elapsed = loop.time() - started

    # Returned, rather than raising...
    assert elapsed < 0.4, "shutdown must give up on a straggler, not block on it"

    # ...and left the straggler to finish on its own rather than losing track of it.
    await asyncio.wait([task], timeout=2)
    assert released.is_set()


@pytest.mark.asyncio
async def test_shutdown_awaits_tasks_that_do_stop() -> None:
    stopped = False

    async def well_behaved() -> None:
        nonlocal stopped
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            stopped = True
            raise

    task: asyncio.Task[Any] = asyncio.create_task(well_behaved())
    await asyncio.sleep(0)

    await _cancel_and_await_tasks([task], 5)

    assert stopped, "a cooperative task must still be awaited to completion"


@pytest.mark.asyncio
async def test_shutdown_with_no_tasks_is_a_no_op() -> None:
    await _cancel_and_await_tasks([], 5)
