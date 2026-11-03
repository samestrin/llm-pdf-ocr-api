"""Jobs held in memory, for PDFs too long to read in one request.

A job is `queued`, `running`, then `done` or `failed`. It holds the PDF bytes
only until it ends; its result is dropped once it has been finished for `ttl`
seconds, and the job is forgotten. At most `max_jobs` jobs are held at once,
finished ones included until they expire. Jobs run one at a time, in a thread of
this process: they are only visible to the process that holds them, so the
service must run a single uvicorn worker.
"""

import secrets
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from enum import StrEnum
from typing import Any


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class TooManyJobsError(Exception):
    """The store already holds its maximum number of jobs."""

    code = "too_many_jobs"


class Job:
    """One job. `pages` maps each selected page to `pending`, `ok` or `failed`."""

    def __init__(self, job_id: str, data: bytes, options: dict[str, Any]):
        self.id = job_id
        self.data: bytes | None = data
        self.options = options
        self.status = JobStatus.QUEUED
        self.pages: dict[int, str] = {}
        self.pages_total: int | None = None
        self.result: Any = None
        self.error: dict[str, str] | None = None
        self.finished_at: float | None = None
        self.finished = threading.Event()

    def selected(self, numbers: Sequence[int]) -> None:
        """Record the pages the job will read."""
        self.pages = dict.fromkeys(numbers, "pending")
        self.pages_total = len(self.pages)

    def page_done(self, number: int, status: str) -> None:
        """Record that one page was read (`ok`) or could not be (`failed`)."""
        self.pages[number] = status


Work = Callable[[bytes, Job], Any]


class JobStore:
    """Holds jobs and runs them, one at a time."""

    def __init__(
        self, max_jobs: int, ttl: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.max_jobs = max_jobs
        self.ttl = ttl
        self._clock = clock
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="job")

    def __len__(self) -> int:
        with self._lock:
            self._expire()
            return len(self._jobs)

    def submit(self, data: bytes, work: Work, **options: Any) -> Job:
        """Hold a new job for `data` and queue `work(data, job)`; its return is the result.

        Raises `TooManyJobsError` when `max_jobs` jobs are already held.
        """
        with self._lock:
            self._expire()
            if len(self._jobs) >= self.max_jobs:
                raise TooManyJobsError(f"{self.max_jobs} jobs are held already; try again later")
            job = Job(secrets.token_urlsafe(), data, options)
            self._jobs[job.id] = job
        self._executor.submit(self._run, job, work)
        return job

    def get(self, job_id: str) -> Job | None:
        """The job, or None if there is no such job or it has expired."""
        with self._lock:
            self._expire()
            return self._jobs.get(job_id)

    def shutdown(self) -> None:
        """Stop running jobs: the one running finishes, queued ones are dropped."""
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _run(self, job: Job, work: Work) -> None:
        data, job.data = job.data, None  # nothing but this call holds the bytes now
        job.status = JobStatus.RUNNING
        try:
            job.result = work(data, job)
            job.status = JobStatus.DONE
        except Exception:  # the message stays generic: no detail of the failure leaves
            job.error = {"code": "job_failed", "message": "the job could not be completed"}
            job.status = JobStatus.FAILED
        finally:
            del data
            job.finished_at = self._clock()
            job.finished.set()

    def _expire(self) -> None:
        now = self._clock()
        for job_id, job in list(self._jobs.items()):
            if job.finished_at is not None and now - job.finished_at >= self.ttl:
                job.result = None
                job.error = None
                del self._jobs[job_id]
