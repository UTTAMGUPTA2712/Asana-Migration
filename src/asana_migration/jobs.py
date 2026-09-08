"""A persistent, resumable work queue - safe for more than one process.

Every unit of import work (fetch this project, fetch this task's subtasks,
fetch this task's comments, ...) is one small job. Jobs are kept in a single
JSON file (``var/jobs.json``) so if the process is killed mid-import, the
next `serve` or `import-all` picks the queue up exactly where it left off
instead of starting over. Fine granularity (one Asana call per job) is what
lets the worker interleave many projects/tasks fairly while the rate limiter
paces the actual network calls.

`serve` (its background worker) and a standalone `import-all` are separate
OS processes that can both point at the same `var/jobs.json`. Every mutating
method here re-reads the file fresh from disk under an exclusive
cross-process file lock, mutates, writes it back, then releases - so two
processes genuinely can't clobber each other's writes (the bug behind the
`FileNotFoundError` / lost-progress issue from running both without this).
"""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import asdict, dataclass, field

from .config import JOBS_PATH
from .storage import read_json, write_json

MAX_ATTEMPTS = 5

# A job stays "running" this long before another process is allowed to
# assume its owner crashed and reclaim it. Generous relative to how long a
# single rate-limited Asana call could plausibly take (including retries).
STALE_RUNNING_SECONDS = 600

try:
    import fcntl
    _HAVE_FLOCK = True
except ImportError:  # pragma: no cover - non-POSIX platform
    _HAVE_FLOCK = False


@dataclass
class Job:
    id: int
    type: str
    payload: dict
    status: str = "queued"  # queued | running | done | error
    attempts: int = 0
    error: str | None = None
    dedupe_key: str | None = None
    created_at: float = field(default_factory=time.time)
    not_before: float = 0.0
    started_at: float | None = None


class JobQueue:
    def __init__(self, path=JOBS_PATH):
        self.path = path
        self.lock_path = path.parent / (path.name + ".lock")
        self._thread_lock = threading.Lock()

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            if not _HAVE_FLOCK:
                yield
                return
            with open(self.lock_path, "a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)

    def _load(self) -> dict[int, Job]:
        raw = read_json(self.path, default=[]) or []
        jobs: dict[int, Job] = {}
        now = time.time()
        for item in raw:
            job = Job(**item)
            if job.status == "running" and (job.started_at is None or now - job.started_at > STALE_RUNNING_SECONDS):
                # Its owner (this process or another) almost certainly died
                # mid-job; make it eligible again. Not stale yet -> leave it
                # alone, another live process may genuinely be working it.
                job.status = "queued"
            jobs[job.id] = job
        return jobs

    def _save(self, jobs: dict[int, Job]) -> None:
        write_json(self.path, [asdict(j) for j in jobs.values()])

    def push(self, type: str, payload: dict, dedupe_key: str | None = None, force: bool = False) -> Job | None:
        """Add a job, unless `dedupe_key` already names one that's queued,
        running, or (unless `force`) already done - the last part is what
        stops a project/task shared across teams or parents from having its
        sub-work (sections, tasks, comments, ...) redundantly re-walked once
        the first crawl that reached it has already finished. A permanently
        failed ("error") job with the same key doesn't block a fresh attempt.
        """
        with self._locked():
            jobs = self._load()
            if dedupe_key and not force:
                for job in jobs.values():
                    if job.dedupe_key == dedupe_key and job.status in ("queued", "running", "done"):
                        return None
            next_id = max((j.id for j in jobs.values()), default=0) + 1
            job = Job(id=next_id, type=type, payload=payload, dedupe_key=dedupe_key)
            jobs[job.id] = job
            self._save(jobs)
            return job

    def pop_next(self) -> Job | None:
        with self._locked():
            jobs = self._load()
            now = time.time()
            candidates = [j for j in jobs.values() if j.status == "queued" and j.not_before <= now]
            if not candidates:
                return None
            candidates.sort(key=lambda j: j.id)
            job = candidates[0]
            job.status = "running"
            job.started_at = now
            self._save(jobs)
            return job

    def complete(self, job_id: int) -> None:
        with self._locked():
            jobs = self._load()
            job = jobs.get(job_id)
            if job:
                job.status = "done"
                job.error = None
                self._save(jobs)

    def fail(self, job_id: int, error: str) -> None:
        with self._locked():
            jobs = self._load()
            job = jobs.get(job_id)
            if not job:
                return
            job.attempts += 1
            job.error = str(error)[:2000]
            if job.attempts >= MAX_ATTEMPTS:
                job.status = "error"
            else:
                job.status = "queued"
                job.not_before = time.time() + min(60, 2**job.attempts)
            self._save(jobs)

    def prune_done(self, keep_last: int = 200) -> None:
        with self._locked():
            jobs = self._load()
            done = sorted((j for j in jobs.values() if j.status == "done"), key=lambda j: j.id)
            for job in (done[:-keep_last] if keep_last else done):
                del jobs[job.id]
            self._save(jobs)

    def stats(self) -> dict:
        with self._locked():
            jobs = self._load()
            out = {"queued": 0, "running": 0, "done": 0, "error": 0}
            for job in jobs.values():
                out[job.status] = out.get(job.status, 0) + 1
            return out

    def pending_count_for(self, predicate) -> int:
        with self._locked():
            jobs = self._load()
            return sum(1 for j in jobs.values() if j.status in ("queued", "running") and predicate(j))

    def stats_for(self, predicate) -> dict:
        """Like `stats()`, but scoped to jobs matching `predicate` (e.g.
        `lambda j: j.type == "download_task_attachment"`) - needed anywhere
        the queue is shared with other job types (every command shares one
        `var/jobs.json`, see jobs.py's module docstring) and a caller wants
        to know how much of *its* work is left, not the whole file's."""
        with self._locked():
            jobs = self._load()
            out = {"queued": 0, "running": 0, "done": 0, "error": 0}
            for job in jobs.values():
                if predicate(job):
                    out[job.status] = out.get(job.status, 0) + 1
            return out

    def errors_for(self, predicate) -> list[Job]:
        with self._locked():
            jobs = self._load()
            return [j for j in jobs.values() if j.status == "error" and predicate(j)]
