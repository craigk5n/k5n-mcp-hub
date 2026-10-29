"""In-memory OpenAPI import jobs (Story 13.4).

An import loads and validates a spec in a child process, which takes about 19 s per
MB, so a spec near the size cap takes minutes -- longer than proxies and browsers
wait on one request. The route returns 202 with a job id and the client polls.

Kept in memory by design: a restart loses in-flight jobs and the admin re-runs the
import, which costs nothing because nothing is written until a job succeeds. It is
bounded every way that matters: running jobs (each spawns a child that may use
`openapi.validate_memory_mb`), waiting jobs (each holds up to `openapi.max_spec_bytes`
of spec), and finished jobs (remembered for `retention_seconds`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from mcp_hub.openapi.errors import SpecError
from mcp_hub.openapi.importer import ImportRejected
from mcp_hub.openapi.model import MappingError
from mcp_hub.utils import utcnow

logger = logging.getLogger(__name__)

JobStatus = Literal["queued", "running", "succeeded", "failed"]
_FINISHED = ("succeeded", "failed")


class JobQueueFull(Exception):
    """Too many imports are already waiting; the caller should retry later."""


@dataclass(frozen=True)
class ImportJob:
    id: str
    server_id: str
    status: JobStatus
    created_at: datetime
    finished_at: datetime | None = None
    outcome: str | None = None
    error: str | None = None
    pointer: str = ""
    problems: tuple[str, ...] = ()

    def to_json(self) -> dict[str, object]:
        return {
            "job_id": self.id,
            "server_id": self.server_id,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "outcome": self.outcome,
            "error": self.error,
            "pointer": self.pointer,
            "problems": list(self.problems),
        }


class ImportJobStore:
    def __init__(
        self,
        *,
        max_running: int = 2,
        max_queued: int = 10,
        retention_seconds: float = 3600,
        max_finished: int = 100,
        clock: Callable[[], float] = time.monotonic,
        on_task: Callable[[asyncio.Task[None]], None] | None = None,
    ) -> None:
        self._max_running = max_running
        self._max_queued = max_queued
        self._retention = retention_seconds
        self._max_finished = max_finished
        self._clock = clock
        self._on_task = on_task
        self._jobs: dict[str, ImportJob] = {}
        self._finished_at: dict[str, float] = {}
        self._slots: asyncio.Semaphore | None = None

    def submit(self, server_id: str, work: Callable[[], Awaitable[str]]) -> ImportJob:
        """Start `work` in the background. It returns an outcome word, or raises."""
        self._prune()
        waiting = sum(1 for job in self._jobs.values() if job.status not in _FINISHED)
        if waiting >= self._max_queued:
            raise JobQueueFull(f"{waiting} OpenAPI imports are already in progress")
        job = ImportJob(
            id=uuid.uuid4().hex, server_id=server_id, status="queued", created_at=utcnow()
        )
        self._jobs[job.id] = job
        task = asyncio.create_task(self._run(job.id, work), name=f"openapi-import-{job.id}")
        if self._on_task is not None:
            self._on_task(task)
        return job

    def get(self, job_id: str) -> ImportJob | None:
        self._prune()
        return self._jobs.get(job_id)

    async def _run(self, job_id: str, work: Callable[[], Awaitable[str]]) -> None:
        if self._slots is None:  # created here, inside the loop that will use it
            self._slots = asyncio.Semaphore(self._max_running)
        async with self._slots:
            self._update(job_id, status="running")
            try:
                outcome = await work()
            except SpecError as exc:
                self._finish(job_id, error=exc.message, pointer=exc.pointer)
            except MappingError as exc:
                self._finish(job_id, error="The selection cannot be served", problems=exc.problems)
            except ImportRejected as exc:
                self._finish(job_id, error=exc.message, problems=exc.problems)
            except Exception:
                # Logged for the operator; the job says only that it failed, because
                # an unexpected exception's text can carry paths and internals.
                logger.exception("OpenAPI import job %s failed unexpectedly", job_id)
                self._finish(job_id, error="The import failed unexpectedly; see the hub log")
            else:
                self._finish(job_id, outcome=outcome)

    def _update(self, job_id: str, **changes: object) -> None:
        if job_id in self._jobs:
            self._jobs[job_id] = dataclasses.replace(self._jobs[job_id], **changes)  # type: ignore[arg-type]

    def _finish(
        self,
        job_id: str,
        *,
        outcome: str | None = None,
        error: str | None = None,
        pointer: str = "",
        problems: tuple[str, ...] = (),
    ) -> None:
        self._update(
            job_id,
            status="failed" if error is not None else "succeeded",
            finished_at=utcnow(),
            outcome=outcome,
            error=error,
            pointer=pointer,
            problems=tuple(problems),
        )
        self._finished_at[job_id] = self._clock()

    def _prune(self) -> None:
        now = self._clock()
        expired = [jid for jid, at in self._finished_at.items() if now - at > self._retention]
        oldest_first = sorted(self._finished_at, key=self._finished_at.__getitem__)
        excess = oldest_first[: max(0, len(oldest_first) - self._max_finished)]
        for job_id in {*expired, *excess}:
            self._jobs.pop(job_id, None)
            self._finished_at.pop(job_id, None)
