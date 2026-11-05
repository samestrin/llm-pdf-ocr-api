"""Jobs held in memory, for PDFs too long to read in one request.

A job is `queued`, `running`, then `done` or `failed`. It holds the PDF bytes
only until it ends; its result and page images are dropped once it has been
finished for `ttl` seconds, and the job is forgotten. At most `max_jobs` jobs are held at once,
finished ones included until they expire, and their page images together hold at
most `max_image_bytes`: past that, a job keeps reading but renders no more images.
Jobs run one at a time, in a thread of this process: they are only visible to the
process that holds them, so the service must run a single uvicorn worker.
"""

import logging
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from enum import StrEnum
from typing import Any

log = logging.getLogger(__name__)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class TooManyJobsError(Exception):
    """The store already holds its maximum number of jobs."""

    code = "too_many_jobs"


class ImageBudget:
    """The bytes of page images every held job may keep between them; `None` is no cap."""

    def __init__(self, max_bytes: int | None) -> None:
        self.max_bytes = max_bytes
        self.held = 0
        self._lock = threading.Lock()

    def take(self, size: int) -> bool:
        """Count `size` more bytes as held, unless that would pass the cap."""
        with self._lock:
            if self.max_bytes is not None and self.held + size > self.max_bytes:
                return False
            self.held += size
            return True

    def give_back(self, size: int) -> None:
        with self._lock:
            self.held -= size


class Job:
    """One job. `pages` maps each selected page to `pending`, `ok` or `failed`;
    `images` maps a page to its PNG once it is rendered and fits the budget."""

    def __init__(
        self,
        job_id: str,
        data: bytes,
        options: dict[str, Any],
        budget: ImageBudget | None = None,
    ):
        self.id = job_id
        self.data: bytes | None = data
        self.options = options
        self.status = JobStatus.QUEUED
        self.pages: dict[int, str] = {}
        self.images: dict[int, bytes] = {}
        self.pages_total: int | None = None
        self.result: Any = None
        self.error: dict[str, str] | None = None
        self.finished_at: float | None = None
        self.finished = threading.Event()
        self._budget = ImageBudget(None) if budget is None else budget

    def selected(self, numbers: Sequence[int]) -> None:
        """Record the pages the job will read."""
        self.pages = dict.fromkeys(numbers, "pending")
        self.pages_total = len(self.pages)

    def page_done(self, number: int, status: str) -> None:
        """Record that one page was read (`ok`) or could not be (`failed`)."""
        self.pages[number] = status

    def add_image(self, number: int, png: bytes) -> bool:
        """Hold one page's PNG if it fits the image budget; say whether it did."""
        if not self._budget.take(len(png)):
            return False
        self.images[number] = png
        return True

    def drop_images(self) -> None:
        """Drop the page images and give their bytes back to the budget."""
        images, self.images = self.images, {}
        self._budget.give_back(sum(len(png) for png in images.values()))


Work = Callable[[bytes, Job], Any]


class JobStore:
    """Holds jobs and runs them, one at a time."""

    def __init__(
        self,
        max_jobs: int,
        ttl: float,
        clock: Callable[[], float] = time.monotonic,
        max_image_bytes: int | None = None,
    ) -> None:
        self.max_jobs = max_jobs
        self.ttl = ttl
        self.max_image_bytes = max_image_bytes
        self._images = ImageBudget(max_image_bytes)
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
            job = Job(secrets.token_urlsafe(), data, options, self._images)
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
        except Exception as exc:  # the message stays generic: no detail of the failure leaves
            log.error("a job failed: %s", type(exc).__name__)
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
                job.drop_images()
                del self._jobs[job_id]
