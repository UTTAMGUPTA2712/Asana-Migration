"""A pool of workers draining the shared queue, sized to the rate limit.

A single worker is bound by request round-trip latency (network + disk
write + queue-lock overhead), not by the rate limiter itself - measured in
practice at roughly 50-90 completed jobs/minute regardless of how high the
configured limit is set, since only one request is ever in flight. Raising
the limit alone doesn't raise that ceiling; running more requests
concurrently does. `RPM_PER_WORKER` is the assumed per-worker ceiling used
to size the pool - one worker for roughly every 75 requests/minute of
configured budget.

Every worker thread runs the exact same loop, and they all share one
`ImporterContext` - the same `AsanaClient`/`RateLimiter` (already
thread-safe: `RateLimiter.acquire()` has its own lock, so the token bucket
is correctly coordinated across every worker) and the same `JobQueue`
(already safe for concurrent access - see jobs.py - proven under real
multi-process load, which is a strictly harder case than multiple threads
in one process).
"""

from __future__ import annotations

import logging
import math
import threading
import time

from .importer import HANDLERS, ImporterContext
from .jobs import JobQueue

log = logging.getLogger("asana_migration.worker")

RPM_PER_WORKER = 75


def desired_worker_count(rate_limit_per_minute: int) -> int:
    return max(1, math.ceil(rate_limit_per_minute / RPM_PER_WORKER))


class _WorkerThread:
    """One thread draining the shared queue. `stop_event` lets the pool
    shrink cleanly - checked between jobs, never mid-request, so shrinking
    never kills work partway through."""

    def __init__(self, ctx: ImporterContext, name: str, poll_interval: float = 1.0):
        self.ctx = ctx
        self.poll_interval = poll_interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float | None = None) -> None:
        self.thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self.thread.is_alive()

    def _run(self) -> None:
        queue: JobQueue = self.ctx.queue
        name = self.thread.name
        log.info("%s: started", name)
        while not self.stop_event.is_set():
            # This whole body is guarded: an exception that escapes it kills
            # this thread silently, shrinking the pool with no visible
            # crash. Nothing here should ever be allowed to do that.
            try:
                job = queue.pop_next()
                if job is None:
                    time.sleep(self.poll_interval)
                    continue
                handler = HANDLERS.get(job.type)
                if handler is None:
                    queue.fail(job.id, f"no handler registered for job type {job.type!r}")
                    continue
                log.debug("%s: job #%d: %s %s", name, job.id, job.type, job.payload)
                try:
                    handler(self.ctx, job.payload)
                    queue.complete(job.id)
                except Exception as exc:  # noqa: BLE001 - job errors must not kill the worker
                    log.warning("%s: job %s (%s) failed: %s", name, job.id, job.type, exc)
                    queue.fail(job.id, str(exc))
            except Exception as exc:  # noqa: BLE001 - last-resort guard, see comment above
                log.error("%s hit an unexpected error, will keep going: %s", name, exc)
                time.sleep(self.poll_interval)
        log.info("%s: stopped", name)


class WorkerPool:
    """Manages however many `_WorkerThread`s the current rate limit calls
    for, and can be resized live (e.g. when the rate limit changes in the
    web UI's Settings panel) without losing in-flight work."""

    def __init__(self, ctx: ImporterContext, poll_interval: float = 1.0):
        self.ctx = ctx
        self.poll_interval = poll_interval
        self._lock = threading.Lock()
        self._workers: list[_WorkerThread] = []
        self._next_id = 1

    def start(self, rate_limit_per_minute: int) -> None:
        with self._lock:
            if self._workers:
                return
            self._spawn_locked(desired_worker_count(rate_limit_per_minute))

    def resize(self, rate_limit_per_minute: int) -> None:
        target = desired_worker_count(rate_limit_per_minute)
        with self._lock:
            current = len(self._workers)
            if target > current:
                log.info("scaling worker pool up: %d -> %d worker(s) (%d req/min)",
                          current, target, rate_limit_per_minute)
                self._spawn_locked(target - current)
            elif target < current:
                log.info("scaling worker pool down: %d -> %d worker(s) (%d req/min)",
                          current, target, rate_limit_per_minute)
                excess, self._workers = self._workers[target:], self._workers[:target]
                for w in excess:
                    w.stop_event.set()  # each finishes its current job, then exits on its own

    def _spawn_locked(self, count: int) -> None:
        for _ in range(count):
            w = _WorkerThread(self.ctx, name=f"asana-worker-{self._next_id}", poll_interval=self.poll_interval)
            self._next_id += 1
            self._workers.append(w)
            w.start()

    def stop(self, join_timeout: float | None = 5) -> None:
        with self._lock:
            workers, self._workers = self._workers, []
        for w in workers:
            w.stop_event.set()
        for w in workers:
            w.join(timeout=join_timeout)

    def worker_count(self) -> int:
        with self._lock:
            return len(self._workers)
