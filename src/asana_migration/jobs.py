"""A persistent, resumable work queue.

Every unit of import work (fetch this project, fetch this task's subtasks,
fetch this task's comments, ...) is one small job. Jobs are kept in a single
JSON file (``var/jobs.json``) so if the process is killed mid-import, the
next `serve` picks the queue up exactly where it left off instead of
starting over. Fine granularity (one Asana call per job) is what lets the
worker interleave many projects/tasks fairly while the rate limiter paces
the actual network calls.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import asdict, dataclass, field

from .config import JOBS_PATH
from .storage import read_json, write_json

MAX_ATTEMPTS = 5

_id_counter = itertools.count(1)


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


class JobQueue:
    def __init__(self, path=JOBS_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._jobs: dict[int, Job] = {}
        self._load()

    # -- persistence -----------------------------------------------------

    def _load(self) -> None:
        raw = read_json(self.path, default=[]) or []
        max_id = 0
        for item in raw:
            job = Job(**item)
            self._jobs[job.id] = job
            max_id = max(max_id, job.id)
            if job.status == "running":
                # process died mid-job; make it eligible again
                job.status = "queued"
        global _id_counter
        _id_counter = itertools.count(max_id + 1)

    def _save(self) -> None:
        write_json(self.path, [asdict(j) for j in self._jobs.values()])

    # -- mutation ----------------------------------------------------------

    def push(self, type: str, payload: dict, dedupe_key: str | None = None) -> Job | None:
        with self._lock:
            if dedupe_key:
                for job in self._jobs.values():
                    if job.dedupe_key == dedupe_key and job.status in ("queued", "running"):
                        return None
            job = Job(id=next(_id_counter), type=type, payload=payload, dedupe_key=dedupe_key)
            self._jobs[job.id] = job
            self._save()
            return job

    def pop_next(self) -> Job | None:
        with self._lock:
            now = time.time()
            candidates = [
                j for j in self._jobs.values() if j.status == "queued" and j.not_before <= now
            ]
            if not candidates:
                return None
            candidates.sort(key=lambda j: j.id)
            job = candidates[0]
            job.status = "running"
            self._save()
            return job

    def complete(self, job_id: int) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.status = "done"
                job.error = None
                self._save()

    def fail(self, job_id: int, error: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.attempts += 1
            job.error = str(error)[:2000]
            if job.attempts >= MAX_ATTEMPTS:
                job.status = "error"
            else:
                job.status = "queued"
                job.not_before = time.time() + min(60, 2**job.attempts)
            self._save()

    def prune_done(self, keep_last: int = 200) -> None:
        with self._lock:
            done = sorted(
                (j for j in self._jobs.values() if j.status == "done"), key=lambda j: j.id
            )
            for job in done[:-keep_last] if keep_last else done:
                del self._jobs[job.id]
            self._save()

    # -- queries -------------------------------------------------------

    def stats(self) -> dict:
        with self._lock:
            out = {"queued": 0, "running": 0, "done": 0, "error": 0}
            for job in self._jobs.values():
                out[job.status] = out.get(job.status, 0) + 1
            return out

    def pending_count_for(self, predicate) -> int:
        with self._lock:
            return sum(
                1
                for j in self._jobs.values()
                if j.status in ("queued", "running") and predicate(j)
            )

    def errors_for(self, predicate) -> list[Job]:
        with self._lock:
            return [j for j in self._jobs.values() if j.status == "error" and predicate(j)]
