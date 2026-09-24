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

Workers claim jobs in batches (`JobQueue.pop_batch`) and write results back
in batches (`JobQueue.commit_batch`), not one job at a time: the queue file
is rewritten whole on every mutation, and at 1e5+ jobs doing that ~5 times
per job (pop, each follow-up push, complete) was the real throughput cap,
far below the rate limit. While a batch runs, handlers see a
`_BufferedQueue` in place of the real queue, so the follow-up jobs they
push are held in memory and land in the batch's next commit.
"""

from __future__ import annotations

import copy
import dataclasses
import logging
import math
import threading
import time

from .jobs import JobQueue

log = logging.getLogger("asana_migration.worker")

RPM_PER_WORKER = 75

# Most jobs one worker claims per `pop_batch` (see jobs.py) - fewer once the
# queue runs low, so the remaining work still spreads across every worker.
BATCH_SIZE = 50

# How often a running batch writes back what it's finished so far (done/
# failed jobs, pushed follow-ups) and refreshes its still-pending claims.
# Keeps follow-up work visible to the other workers without waiting for a
# whole batch, and keeps slow batches (e.g. big attachment transfers) well
# inside jobs.STALE_RUNNING_SECONDS so they're never reclaimed as crashed.
FLUSH_INTERVAL_SECONDS = 60

# How often the foreground drain loops (import-all, download-attachments,
# ...) check the queue for "all done". Each check parses the whole queue
# file under its lock, so polling every second competed with the workers
# themselves once the file got large.
STATUS_POLL_SECONDS = 5


def desired_worker_count(rate_limit_per_minute: int) -> int:
    return max(1, math.ceil(rate_limit_per_minute / RPM_PER_WORKER))


class _BufferedQueue:
    """Stands in for the `JobQueue` a handler sees while its batch runs:
    `push`/`push_many` are held in `pending` for the batch's next
    `commit_batch` (which applies the same dedupe rules); every other
    method goes straight to the real queue. `push()` returns None here -
    whether a buffered job survives dedupe isn't known until commit, and
    no handler uses the return value."""

    def __init__(self, queue: JobQueue):
        self._queue = queue
        self.pending: list[tuple[str, dict, str | None]] = []

    def push(self, type: str, payload: dict, dedupe_key: str | None = None, force: bool = False):
        if force:  # commit_batch has no force mode - rare, so just write it through
            return self._queue.push(type, payload, dedupe_key=dedupe_key, force=True)
        self.pending.append((type, payload, dedupe_key))
        return None

    def push_many(self, items, requeue_done: bool = False) -> int:
        if requeue_done:  # same: commit_batch doesn't support this mode
            return self._queue.push_many(items, requeue_done=True)
        items = list(items)
        self.pending.extend(items)
        return len(items)

    def __getattr__(self, name):
        return getattr(self._queue, name)


def _with_queue(ctx, queue):
    """A shallow copy of `ctx` with `.queue` swapped - every context type
    sharing this pool (ImporterContext, padmasana's) is a dataclass with a
    `queue` field."""
    if dataclasses.is_dataclass(ctx):
        return dataclasses.replace(ctx, queue=queue)
    clone = copy.copy(ctx)
    clone.queue = queue
    return clone


class _WorkerThread:
    """One thread draining the shared queue. `stop_event` lets the pool
    shrink cleanly - checked between jobs, never mid-request, so shrinking
    never kills work partway through."""

    def __init__(self, ctx, name: str, poll_interval: float = 1.0, handlers: dict | None = None,
                 share=lambda: 1, batch_size: int = BATCH_SIZE):
        self.ctx = ctx
        self.poll_interval = poll_interval
        # How many workers currently share the queue (a callable, since the
        # pool can be resized live) - for pop_batch's fair-share cap.
        self.share = share
        self.batch_size = batch_size
        # Defaults to asana_migration's own job handlers so every existing
        # caller (which never passed this) keeps working unmodified; other
        # packages that share this pool for their own job types (e.g.
        # padmasana_migration, see its DESIGN.md §4) pass their own dict.
        if handlers is None:
            from .importer import HANDLERS as handlers
        self.handlers = handlers
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float | None = None) -> None:
        self.thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self.thread.is_alive()

    def _run(self) -> None:
        # Runs as this thread's own target, so every log call below picks up
        # this thread's name automatically via the app's %(threadName)s log
        # format - no need to prepend it manually here.
        queue: JobQueue = self.ctx.queue
        log.info("started")
        while not self.stop_event.is_set():
            # This whole body is guarded: an exception that escapes it kills
            # this thread silently, shrinking the pool with no visible
            # crash. Nothing here should ever be allowed to do that.
            try:
                batch = queue.pop_batch(self.batch_size, share=self.share())
                if not batch:
                    time.sleep(self.poll_interval)
                    continue
                self._run_batch(queue, batch)
            except Exception as exc:  # noqa: BLE001 - last-resort guard, see comment above
                log.error("hit an unexpected error, will keep going: %s", exc)
                time.sleep(self.poll_interval)
        log.info("stopped")

    def _run_batch(self, queue: JobQueue, batch: list) -> None:
        """Runs one claimed batch, committing results every
        FLUSH_INTERVAL_SECONDS and once at the end. A stop request is
        honored between jobs: the jobs not yet started are released back to
        the queue rather than left "running" until they go stale. If this
        process dies outright, uncommitted jobs are still "running" on disk
        and get reclaimed after STALE_RUNNING_SECONDS - at worst one batch
        is redone, which handlers tolerate (they rewrite the same output)."""
        buffer = _BufferedQueue(queue)
        ctx = _with_queue(self.ctx, buffer)
        done: list[tuple[int, float]] = []
        failed: list[tuple[int, str]] = []
        last_flush = time.monotonic()

        def flush(**extra) -> None:
            nonlocal last_flush
            queue.commit_batch(done=done, failed=failed, pushes=buffer.pending, **extra)
            done.clear()
            failed.clear()
            buffer.pending.clear()
            last_flush = time.monotonic()

        next_index = 0
        try:
            for next_index, job in enumerate(batch):
                if self.stop_event.is_set():
                    break
                started_at = time.time()
                handler = self.handlers.get(job.type)
                if handler is None:
                    failed.append((job.id, f"no handler registered for job type {job.type!r}"))
                else:
                    log.debug("job #%d: %s %s", job.id, job.type, job.payload)
                    try:
                        handler(ctx, job.payload)
                        done.append((job.id, started_at))
                    except Exception as exc:  # noqa: BLE001 - job errors must not kill the worker
                        log.warning("job %s (%s) failed: %s", job.id, job.type, exc)
                        failed.append((job.id, str(exc)))
                next_index += 1
                if time.monotonic() - last_flush >= FLUSH_INTERVAL_SECONDS and next_index < len(batch):
                    flush(touch=[j.id for j in batch[next_index:]])
        finally:
            flush(release=[j.id for j in batch[next_index:]])


class WorkerPool:
    """Manages however many `_WorkerThread`s the current rate limit calls
    for, and can be resized live (e.g. when the rate limit changes in the
    web UI's Settings panel) without losing in-flight work."""

    def __init__(self, ctx, poll_interval: float = 1.0, handlers: dict | None = None):
        self.ctx = ctx
        self.poll_interval = poll_interval
        self.handlers = handlers
        self._lock = threading.Lock()
        self._workers: list[_WorkerThread] = []
        self._next_id = 1

    def start(self, rate_limit_per_minute: int | None = None, *, worker_count: int | None = None) -> None:
        """Sized from `rate_limit_per_minute` via `desired_worker_count` by
        default - right for job types that are one fast JSON call each, the
        case every caller but `download-attachments` is in. Pass
        `worker_count` directly instead when jobs are bandwidth-bound rather
        than request-rate-bound (multi-second/minute file transfers): worker
        count there is a concurrency knob, not a function of how many API
        calls/minute are allowed - see `client.download_file`'s `pace`
        param for the other half of that split."""
        with self._lock:
            if self._workers:
                return
            count = worker_count if worker_count is not None else desired_worker_count(rate_limit_per_minute)
            self._spawn_locked(count)

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
            w = _WorkerThread(
                self.ctx, name=f"asana-worker-{self._next_id}",
                poll_interval=self.poll_interval, handlers=self.handlers,
                share=self.worker_count,
            )
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
