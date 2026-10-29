"""Story 13.4 (part C) -- the in-memory store for OpenAPI import jobs.

Imports run in the background because validation can take minutes. The store is
bounded every way that matters: how many run at once (each spawns a child process
with up to `validate_memory_mb`), how many may wait (each holds up to
`max_spec_bytes` of spec), and how long a finished job is remembered.
"""

from __future__ import annotations

import asyncio

import pytest

from mcp_hub.openapi.errors import SpecError
from mcp_hub.openapi.importer import ImportRejected
from mcp_hub.openapi.jobs import ImportJobStore, JobQueueFull
from mcp_hub.openapi.model import MappingError


async def _wait(store: ImportJobStore, job_id: str) -> None:
    for _ in range(200):
        job = store.get(job_id)
        if job is not None and job.status in ("succeeded", "failed"):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish")


class TestOutcomes:
    async def test_success(self):
        store = ImportJobStore()

        async def work() -> str:
            return "created"

        job = store.submit("pets", work)
        assert job.status in ("queued", "running")
        await _wait(store, job.id)
        done = store.get(job.id)
        assert (done.status, done.outcome, done.server_id) == ("succeeded", "created", "pets")
        assert done.finished_at is not None

    @pytest.mark.parametrize(
        "error, message, pointer, problems",
        [
            (SpecError("bad title", pointer="/info/title"), "bad title", "/info/title", ()),
            (MappingError(["a", "b"]), None, "", ("a", "b")),
            (ImportRejected("no", problems=("p",)), "no", "", ("p",)),
        ],
    )
    async def test_expected_failures_are_reported(self, error, message, pointer, problems):
        store = ImportJobStore()

        async def work() -> str:
            raise error

        job = store.submit("pets", work)
        await _wait(store, job.id)
        done = store.get(job.id)
        assert done.status == "failed"
        if message is not None:
            assert done.error == message
        assert done.pointer == pointer
        assert done.problems == problems

    async def test_an_unexpected_failure_does_not_leak_its_detail(self, caplog):
        store = ImportJobStore()

        async def work() -> str:
            raise RuntimeError("internal detail /etc/secret")

        job = store.submit("pets", work)
        await _wait(store, job.id)
        done = store.get(job.id)
        assert done.status == "failed"
        assert "secret" not in (done.error or "")
        assert "internal detail" in caplog.text  # logged for the operator instead

    def test_unknown_job(self):
        assert ImportJobStore().get("nope") is None


class TestBounds:
    async def test_running_is_limited(self):
        store = ImportJobStore(max_running=1, max_queued=5)
        release = asyncio.Event()
        running = 0
        peak = 0

        async def work() -> str:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await release.wait()
            running -= 1
            return "created"

        jobs = [store.submit(f"s{i}", work) for i in range(3)]
        await asyncio.sleep(0.05)
        assert [store.get(j.id).status for j in jobs].count("running") == 1
        release.set()
        for job in jobs:
            await _wait(store, job.id)
        assert peak == 1

    async def test_queue_is_limited(self):
        store = ImportJobStore(max_running=1, max_queued=2)
        release = asyncio.Event()

        async def work() -> str:
            await release.wait()
            return "created"

        store.submit("a", work)
        store.submit("b", work)
        with pytest.raises(JobQueueFull):
            store.submit("c", work)
        release.set()

    async def test_finished_jobs_expire(self):
        now = [1000.0]
        store = ImportJobStore(retention_seconds=60, clock=lambda: now[0])

        async def work() -> str:
            return "created"

        job = store.submit("a", work)
        await _wait(store, job.id)
        now[0] += 59
        assert store.get(job.id) is not None
        now[0] += 2
        assert store.get(job.id) is None

    async def test_finished_jobs_are_capped(self):
        # Awaits the tasks rather than polling `get`, which prunes as it reads.
        tasks: list[asyncio.Task] = []
        store = ImportJobStore(max_finished=2, on_task=tasks.append)

        async def work() -> str:
            return "created"

        jobs = [store.submit(f"s{i}", work) for i in range(3)]
        await asyncio.gather(*tasks)
        store.submit("trigger", work)  # pruning happens on the way in
        assert store.get(jobs[0].id) is None
        assert store.get(jobs[2].id) is not None

    async def test_tasks_are_handed_to_the_owner(self):
        tasks: list[asyncio.Task] = []
        store = ImportJobStore(on_task=tasks.append)

        async def work() -> str:
            return "created"

        job = store.submit("a", work)
        await _wait(store, job.id)
        assert len(tasks) == 1 and tasks[0].done()
