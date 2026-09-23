"""Script 1 - `upload_attachments.py` (DESIGN.md §6).

Walks every task's `attachments.json` under `data/`, one upload job per
attachment, reusing `asana_migration`'s own `JobQueue`/`WorkerPool` (its own
queue file, `var/padmasana_jobs.json` - DESIGN.md §4). `data/` is never
edited; the result lands in `build/tasks/<task_gid>/uploaded_attachments.json`.

Real files (`host == "asana"`) are uploaded via the file-service's
`POST /files`; the response is saved verbatim as this attachment's
`metadata` (DESIGN.md §5.7). Link-only attachments (`gdrive`/`external`)
have nothing to upload - they're carried over as a small
`{url, original_name, source, asana_gid}` record instead.

DESIGN.md §6/§8 both name this script's output `attachments.json` - but §8
also has `build_tasks.py` write a *different*, task-level-only
`attachments.json` under the very same per-task directory once it's done
splitting task-level from comment-level (§5.7). Writing both under the
identical filename would mean `build_tasks.py` clobbers this script's own
upload bookkeeping the moment it runs, breaking the exact
independently-resumable-scripts guarantee DESIGN.md §4 asks for (a later
`upload_attachments.py` run for a newly-added attachment would wrongly
think nothing's uploaded yet, since `already_uploaded_gids` reads whatever
`build_tasks.py` most recently overwrote here). This script's own staging
file is therefore named `uploaded_attachments.json` instead - same content,
same role, just not sharing a filename with `build_tasks.py`'s own output.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from asana_migration.jobs import JobQueue
from asana_migration.storage import Paths, read_json, write_json

from .asana_source import find_task_dir
from .file_service_client import FileServiceClient

log = logging.getLogger("padmasana_migration.upload_attachments")

UPLOAD_PATH = "asana-migration/attachments"

try:
    import fcntl
    _HAVE_FLOCK = True
except ImportError:  # pragma: no cover - non-POSIX platform
    _HAVE_FLOCK = False

# Guards every read-modify-write of an `uploaded_attachments.json`. Without
# it, two workers finishing uploads for the same task at the same time both
# read the file, each append their own record, and the last write wins -
# silently dropping the other's record (the file service still has the
# file, but nothing in build/ points at it). Jobs for one task's attachments
# sit next to each other in the queue, so with --concurrency > 1 this was
# the common case, not a rare one. The critical section is just a small
# JSON read + write - never the upload itself - so one lock for everything
# costs nothing measurable. Same threading-lock + flock pairing as
# `JobQueue._locked`, so a second process on the same build/ is safe too.
_records_thread_lock = threading.Lock()


@dataclass
class UploadContext:
    file_client: FileServiceClient
    data_paths: Paths
    build_dir: Path
    queue: JobQueue


def _build_out_path(build_dir: Path, task_gid: str) -> Path:
    return build_dir / "tasks" / task_gid / "uploaded_attachments.json"


@contextlib.contextmanager
def _records_locked(build_dir: Path):
    build_dir.mkdir(parents=True, exist_ok=True)
    with _records_thread_lock:
        if not _HAVE_FLOCK:
            yield
            return
        with open(build_dir / ".uploaded_attachments.lock", "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def _append_record(build_dir: Path, task_gid: str, record: dict) -> bool:
    """Adds `record` to the task's `uploaded_attachments.json` under
    `_records_locked`. Returns False (writing nothing) if a record for the
    same attachment is already there - only possible if another worker
    uploaded the same attachment concurrently (e.g. a stale `running` job
    reclaimed mid-upload, see `asana_migration.jobs.STALE_RUNNING_SECONDS`);
    first record wins, so an attachment never ends up with two."""
    out_path = _build_out_path(build_dir, task_gid)
    with _records_locked(build_dir):
        existing = read_json(out_path, default=[]) or []
        if any(e.get("asana_gid") == record["asana_gid"] for e in existing):
            return False
        existing.append(record)
        write_json(out_path, existing)
        return True


def _link_only_record(att: dict) -> dict:
    # `view_url` is the real external destination (the gdrive/figma/etc. link
    # itself); `permanent_url` is Asana's own `get_asset` redirect proxy,
    # which depends on an active Asana session and won't resolve once this
    # data lives outside Asana - so it's only a fallback, never preferred.
    return {
        "url": att.get("view_url") or att.get("permanent_url"),
        "original_name": att.get("name"),
        "source": att.get("host"),
        "asana_gid": att["gid"],
    }


def h_upload_attachment(ctx: UploadContext, payload: dict) -> None:
    task_gid = payload["task_gid"]
    attachment_gid = payload["attachment_gid"]
    task_data_dir = find_task_dir(ctx.data_paths, task_gid)
    if task_data_dir is None:
        raise RuntimeError(f"task {task_gid} not found anywhere under {ctx.data_paths.root}")

    attachments = read_json(task_data_dir / "attachments.json", default=[]) or []
    att = next((a for a in attachments if a.get("gid") == attachment_gid), None)
    if att is None:
        raise RuntimeError(f"attachment {attachment_gid} not found in {task_data_dir}/attachments.json anymore")

    if attachment_gid in already_uploaded_gids(ctx.build_dir, task_gid):
        log.info("Task %s: attachment %s already uploaded - skipping", task_gid, attachment_gid)
        return

    if att.get("host") == "asana":
        local_path = att.get("local_path")
        if not local_path or not (task_data_dir / local_path).exists():
            raise RuntimeError(
                f"attachment {attachment_gid} (host=asana) has no downloaded bytes at {local_path!r} - "
                "run `download-attachments` (asana_migration) first"
            )
        log.info("Task %s: uploading attachment %s '%s'...", task_gid, attachment_gid, att.get("name"))
        response = ctx.file_client.upload_file(
            file_path=task_data_dir / local_path,
            path=UPLOAD_PATH,
            name=att.get("name") or attachment_gid,
            metadata={"asana_gid": attachment_gid, "source": "asana"},
        )
        record = {"asana_gid": attachment_gid, "host": "asana", "metadata": response}
    else:
        log.info("Task %s: attachment %s is %s - link only, nothing to upload", task_gid, attachment_gid, att.get("host"))
        # A real upload's identity is the file service's own `metadata.uuid`;
        # a link has no such thing, so one is minted here, once, and kept
        # with the record - `build_tasks.py` reuses it rather than minting a
        # fresh one on every rebuild (which would duplicate the row on
        # every re-seed).
        record = {
            "asana_gid": attachment_gid, "host": att.get("host"),
            "uuid": str(uuid.uuid4()), "metadata": _link_only_record(att),
        }

    if not _append_record(ctx.build_dir, task_gid, record):
        log.warning(
            "Task %s: attachment %s was recorded by another worker while this one was uploading - "
            "keeping that record; this upload (file-service uuid %s) is an unreferenced duplicate",
            task_gid, attachment_gid, (record.get("metadata") or {}).get("uuid"),
        )


HANDLERS = {
    "upload_attachment": h_upload_attachment,
}


def already_uploaded_gids(build_dir: Path, task_gid: str) -> set[str]:
    return {e.get("asana_gid") for e in (read_json(_build_out_path(build_dir, task_gid), default=[]) or [])}


def queue_pending_uploads(data_paths: Paths, build_dir: Path, queue: JobQueue) -> tuple[int, int]:
    """Walks every task's `attachments.json` under `data/` and queues an
    `upload_attachment` job for each one not already recorded in
    `build/tasks/<gid>/uploaded_attachments.json`. Returns (queued, already_done).

    That record - not the job's status - is what "uploaded" means here: an
    attachment with no record is queued again even if an earlier job for it
    is `done`. That's how a re-run recovers records lost to the old
    concurrent-write bug (see `_records_thread_lock`), whose jobs were all
    marked `done` and would otherwise be skipped by the queue's dedupe
    forever. One `push_many` for the whole batch, not one `push` per
    attachment (see `build_tasks.queue_pass1_jobs` for why)."""
    items: list[tuple[str, dict, str]] = []
    already_done = 0
    for attachments_path in data_paths.root.glob("*/tasks/*/attachments.json"):
        task_dir = attachments_path.parent
        task_gid = task_dir.name.split("_", 1)[0]
        done_gids = already_uploaded_gids(build_dir, task_gid)
        attachments = read_json(attachments_path, default=[]) or []
        for att in attachments:
            gid = att.get("gid")
            if not gid:
                continue
            if gid in done_gids:
                already_done += 1
                continue
            items.append((
                "upload_attachment",
                {"task_gid": task_gid, "attachment_gid": gid},
                f"upload_attachment:{gid}",
            ))
    queued = queue.push_many(items, requeue_done=True)
    return queued, already_done
