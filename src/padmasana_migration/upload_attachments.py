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

import logging
from dataclasses import dataclass
from pathlib import Path

from asana_migration.jobs import JobQueue
from asana_migration.storage import Paths, read_json, write_json

from .asana_source import find_task_dir
from .file_service_client import FileServiceClient

log = logging.getLogger("padmasana_migration.upload_attachments")

UPLOAD_PATH = "asana-migration/attachments"


@dataclass
class UploadContext:
    file_client: FileServiceClient
    data_paths: Paths
    build_dir: Path
    queue: JobQueue


def _build_out_path(build_dir: Path, task_gid: str) -> Path:
    return build_dir / "tasks" / task_gid / "uploaded_attachments.json"


def _link_only_record(att: dict) -> dict:
    return {
        "url": att.get("permanent_url") or att.get("view_url"),
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

    out_path = _build_out_path(ctx.build_dir, task_gid)
    existing = read_json(out_path, default=[]) or []
    if any(e.get("asana_gid") == attachment_gid for e in existing):
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
        record = {"asana_gid": attachment_gid, "host": att.get("host"), "metadata": _link_only_record(att)}

    existing.append(record)
    write_json(out_path, existing)


HANDLERS = {
    "upload_attachment": h_upload_attachment,
}


def already_uploaded_gids(build_dir: Path, task_gid: str) -> set[str]:
    return {e.get("asana_gid") for e in (read_json(_build_out_path(build_dir, task_gid), default=[]) or [])}


def queue_pending_uploads(data_paths: Paths, build_dir: Path, queue: JobQueue) -> tuple[int, int]:
    """Walks every task's `attachments.json` under `data/` and queues an
    `upload_attachment` job for each one not already recorded in
    `build/tasks/<gid>/uploaded_attachments.json`. Returns (queued, already_done)."""
    queued = already_done = 0
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
            job = queue.push(
                "upload_attachment",
                {"task_gid": task_gid, "attachment_gid": gid},
                dedupe_key=f"upload_attachment:{gid}",
            )
            if job:
                queued += 1
    return queued, already_done
