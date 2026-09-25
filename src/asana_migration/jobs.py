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
import math
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
        # Compact (no indent) on purpose: this file is rewritten whole on
        # every mutation and holds 1e5+ jobs on a full crawl, so whitespace
        # alone was a large share of each rewrite's cost.
        write_json(self.path, [asdict(j) for j in jobs.values()], indent=None)

    @staticmethod
    def _add_many_locked(jobs: dict[int, Job], items, blocking_statuses: tuple[str, ...]) -> int:
        """The shared body of `push_many`/`commit_batch`: appends `items`
        ((type, payload, dedupe_key) tuples) to the already-loaded `jobs`,
        skipping any whose dedupe_key is held by a job in
        `blocking_statuses` (or by an earlier item in this same call).
        Caller holds the lock and saves."""
        existing_keys = {
            j.dedupe_key for j in jobs.values()
            if j.dedupe_key and j.status in blocking_statuses
        }
        next_id = max((j.id for j in jobs.values()), default=0) + 1
        added = 0
        for type_, payload, dedupe_key in items:
            if dedupe_key and dedupe_key in existing_keys:
                continue
            jobs[next_id] = Job(id=next_id, type=type_, payload=payload, dedupe_key=dedupe_key)
            if dedupe_key:
                existing_keys.add(dedupe_key)
            next_id += 1
            added += 1
        return added

    @staticmethod
    def _apply_failure(job: Job, error: str, now: float) -> None:
        job.attempts += 1
        job.error = str(error)[:2000]
        if job.attempts >= MAX_ATTEMPTS:
            job.status = "error"
        else:
            job.status = "queued"
            job.not_before = now + min(60, 2**job.attempts)

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

    def push_many(self, items: list[tuple[str, dict, str | None]], requeue_done: bool = False) -> int:
        """Bulk `push()` for queueing many jobs at once (e.g. one per task
        found on disk). `push()` loads, dedupe-scans, and saves the *whole*
        jobs file on every single call - fine at the rate real work pushes
        new jobs, but O(n^2) and silent (no work happens between calls, so
        nothing gets logged) when a caller loops it thousands of times in a
        row. This does the load/dedupe-scan/save once for the entire batch.
        `items` is a list of (type, payload, dedupe_key); same dedupe_key
        rules as `push()` (`force` isn't supported here - no bulk caller
        needs it). `requeue_done` lets an already-`done` job's key be pushed
        again (a `queued`/`running` one still blocks it) - for callers whose
        own on-disk output, not the job's status, is the source of truth for
        whether the work really happened, and who only pass items they've
        already confirmed are missing that output. Returns how many were
        actually added."""
        blocking_statuses = ("queued", "running") if requeue_done else ("queued", "running", "done")
        with self._locked():
            jobs = self._load()
            added = self._add_many_locked(jobs, items, blocking_statuses)
            if added:
                self._save(jobs)
            return added

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

    def pop_batch(self, max_n: int, share: int = 1) -> list[Job]:
        """Claims up to `max_n` ready jobs (oldest first) in one locked
        rewrite, instead of one rewrite per job like `pop_next`. Every
        mutation rewrites the whole file, so at 1e5+ jobs that per-job cost
        - not the Asana rate limit - was what capped throughput. `share` is
        how many workers are pulling from this queue: a claim never takes
        more than its fair share of what's ready, so when the queue runs
        low one worker doesn't sit on everything while the rest idle.
        Results go back via `commit_batch`."""
        with self._locked():
            jobs = self._load()
            now = time.time()
            candidates = [j for j in jobs.values() if j.status == "queued" and j.not_before <= now]
            if not candidates:
                return []
            n = max(1, min(max_n, math.ceil(len(candidates) / max(1, share))))
            candidates.sort(key=lambda j: j.id)
            batch = candidates[:n]
            for job in batch:
                job.status = "running"
                job.started_at = now
            self._save(jobs)
            return batch

    def commit_batch(
        self,
        *,
        done: list[tuple[int, float]] = (),
        failed: list[tuple[int, str]] = (),
        pushes: list[tuple[str, dict, str | None]] = (),
        touch: list[int] = (),
        release: list[int] = (),
    ) -> int:
        """Applies a `pop_batch` claim's results in one locked rewrite:
        - `done`: (job_id, started_at) - marked done, with `started_at` set
          to when that job actually began (not when its batch was claimed),
          so `job-status`'s windowed save rate stays accurate.
        - `failed`: (job_id, error) - same retry/backoff policy as `fail()`.
        - `pushes`: jobs the batch's handlers queued, added with `push()`'s
          dedupe rules (checked against the file *and* each other).
        - `touch`: still-running job ids whose `started_at` is refreshed, so
          a long batch isn't mistaken for a crashed one and reclaimed after
          STALE_RUNNING_SECONDS.
        - `release`: claimed-but-never-started job ids, put back to queued
          (e.g. the worker was asked to stop mid-batch).
        Returns how many `pushes` were actually added."""
        with self._locked():
            jobs = self._load()
            now = time.time()
            for job_id, started_at in done:
                job = jobs.get(job_id)
                if job:
                    job.status = "done"
                    job.error = None
                    job.started_at = started_at
            for job_id, error in failed:
                job = jobs.get(job_id)
                if job:
                    self._apply_failure(job, error, now)
            for job_id in touch:
                job = jobs.get(job_id)
                if job and job.status == "running":
                    job.started_at = now
            for job_id in release:
                job = jobs.get(job_id)
                if job and job.status == "running":
                    job.status = "queued"
            added = self._add_many_locked(jobs, pushes, ("queued", "running", "done"))
            self._save(jobs)
            return added

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
            self._apply_failure(job, error, time.time())
            self._save(jobs)

    def archive_paths(self) -> list:
        """Every archive file `archive_done` has written for this queue
        (`jobs-1.json`, `jobs-2.json`, ... next to `jobs.json`), oldest first."""
        found = []
        for p in self.path.parent.glob(f"{self.path.stem}-*{self.path.suffix}"):
            n = p.stem[len(self.path.stem) + 1:]
            if n.isdigit():
                found.append((int(n), p))
        return [p for _, p in sorted(found)]

    def archive_done(self) -> tuple[int, object]:
        """Moves every `done` job out of the live queue file into a new
        numbered archive file next to it (`jobs-1.json`, then `jobs-2.json`,
        ...), so the file every worker re-parses on each mutation stays
        small without losing history - `all_jobs(include_archived=True)`
        (used by `job-status`) still reads them. The highest-id job always
        stays live, even if done, so new job ids never reuse an archived
        one's. Returns (jobs archived, archive path or None)."""
        with self._locked():
            jobs = self._load()
            max_id = max(jobs, default=0)
            done = [j for j in sorted(jobs.values(), key=lambda j: j.id)
                    if j.status == "done" and j.id != max_id]
            if not done:
                return 0, None
            existing = self.archive_paths()
            last_n = int(existing[-1].stem[len(self.path.stem) + 1:]) if existing else 0
            archive = self.path.with_name(f"{self.path.stem}-{last_n + 1}{self.path.suffix}")
            # Written before the live file drops them, so a crash in between
            # can only duplicate history, never lose it.
            write_json(archive, [asdict(j) for j in done], indent=None)
            for job in done:
                del jobs[job.id]
            self._save(jobs)
            return len(done), archive

    def stats(self) -> dict:
        with self._locked():
            jobs = self._load()
            out = {"queued": 0, "running": 0, "done": 0, "error": 0}
            for job in jobs.values():
                out[job.status] = out.get(job.status, 0) + 1
            return out

    def all_jobs(self, include_archived: bool = False) -> list[Job]:
        """A read-only snapshot of every job, id-ordered - for reporting
        (e.g. `job-status`) that needs more than the aggregate counts
        `stats()`/`stats_for()` give, like per-type breakdowns or throughput
        derived from each job's `started_at`. `include_archived` adds the
        done jobs `archive_done` moved out into `jobs-N.json` files."""
        with self._locked():
            jobs = list(self._load().values())
            if include_archived:
                for archive in self.archive_paths():
                    jobs.extend(Job(**item) for item in read_json(archive, default=[]) or [])
            return sorted(jobs, key=lambda j: j.id)

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

    def resolve_superseded_errors(self, predicate) -> list[Job]:
        """Finds every permanently-failed (`error`) job matching `predicate`
        whose `dedupe_key` already has a *different* job that's `queued`,
        `running`, or `done` - meaning some later push already created a
        fresh job for the same work (see `push()`'s docstring: an `error`
        status never blocks a new push, on purpose, so one failure can't
        wedge a dedupe key forever) and that fresh job has since succeeded
        or is in flight. That leaves the original behind as a permanent
        orphan: nothing ever revisits it (there's no pruning for `error`
        rows, only `archive_done`), so it just sits there claiming the same
        work is still broken - inflating `job-status`'s error count and
        `retry-all`'s "still failing" report for something that isn't
        actually still failing.

        Marks each one `done` instead of leaving it `error` (its work
        really is done, just under a different job id), noting which job
        superseded it in `error` for traceability. Must run before
        `requeue_errors`/any retry pass, so a stale orphan doesn't get
        resurrected and have its already-finished work redundantly redone.
        Returns the resolved jobs, for the caller to log."""
        with self._locked():
            jobs = self._load()
            by_key: dict[str, list[Job]] = {}
            for job in jobs.values():
                if job.dedupe_key:
                    by_key.setdefault(job.dedupe_key, []).append(job)
            resolved = []
            for job in jobs.values():
                if job.status != "error" or not job.dedupe_key or not predicate(job):
                    continue
                newer = [j for j in by_key[job.dedupe_key]
                         if j.id != job.id and j.status in ("queued", "running", "done")]
                if newer:
                    superseded_by = max(newer, key=lambda j: j.id)
                    job.status = "done"
                    job.error = f"superseded: job #{superseded_by.id} ({superseded_by.status}) already covers this work"
                    resolved.append(job)
            if resolved:
                self._save(jobs)
            return resolved

    def requeue_errors(self, predicate) -> int:
        """Resets every permanently-failed (`status == "error"`) job matching
        `predicate` back to `queued` with a fresh attempt budget, so the
        normal worker pool picks it up again. Used by `retry-all` for
        non-attachment job types - those don't need their own retry loop
        the way `download_task_attachment` does (see
        `retry_failed_attachment_downloads`, which bypasses the queue
        entirely instead so it can use its own timeout/concurrency); they
        just need another shot through the queue they already came from.
        Call `resolve_superseded_errors` first - this requeues whatever
        `error` jobs remain unconditionally, with no dedupe-key awareness
        of its own. Returns how many jobs were requeued."""
        with self._locked():
            jobs = self._load()
            n = 0
            for job in jobs.values():
                if job.status == "error" and predicate(job):
                    job.status = "queued"
                    job.attempts = 0
                    job.error = None
                    job.not_before = 0.0
                    n += 1
            self._save(jobs)
            return n
