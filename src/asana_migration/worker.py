"""The background worker: pops one job at a time and runs it.

Deliberately single-threaded. The `RateLimiter` already caps how many
requests per minute leave the process, so a single worker draining the queue
serially gives predictable, easy-to-reason-about pacing ("slowly, one thing
at a time") and avoids any need to lock the on-disk JSON tree against
concurrent writers.
"""

from __future__ import annotations

import logging
import threading
import time

from .importer import HANDLERS, ImporterContext
from .jobs import JobQueue

log = logging.getLogger("asana_migration.worker")


class Worker:
    def __init__(self, ctx: ImporterContext, poll_interval: float = 1.0):
        self.ctx = ctx
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="asana-import-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        queue: JobQueue = self.ctx.queue
        log.info("import worker started")
        while not self._stop.is_set():
            job = queue.pop_next()
            if job is None:
                time.sleep(self.poll_interval)
                continue
            handler = HANDLERS.get(job.type)
            if handler is None:
                queue.fail(job.id, f"no handler registered for job type {job.type!r}")
                continue
            log.debug("job #%d: %s %s", job.id, job.type, job.payload)
            try:
                handler(self.ctx, job.payload)
                queue.complete(job.id)
            except Exception as exc:  # noqa: BLE001 - job errors must not kill the worker
                log.warning("job %s (%s) failed: %s", job.id, job.type, exc)
                queue.fail(job.id, str(exc))
