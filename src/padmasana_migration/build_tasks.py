"""Script 3 - `build_tasks.py` (DESIGN.md §8), the big one.

Walks the export task by task (including subtasks - every task, wherever it
sits in the tree, lives once in `data/<workspace>/tasks/`, already deduped
by `asana_migration`'s own pooling, see its `storage.py` module docstring),
one job per task via the same `JobQueue`/`WorkerPool` as script 1
(`--concurrency N`, its own `var/padmasana_jobs.json`), rather than holding
everything in memory at once.

Two passes (DESIGN.md §8's "the subtask problem"):

- **Pass 1** (`h_build_task_pass1`, many worker threads) - one job per task,
  `parent_task_uuid` left `null`. Every task row can be created safely
  regardless of order, since nothing points at anything yet. Also builds, in
  the same job: tags, section placement, collaborators, attachments,
  comments, and the activity log.
- **Pass 2** (`run_pass2`, single-threaded, only once pass 1's queue has
  fully drained) - patches in `parent_task_uuid` for every task that had a
  parent in Asana, now that every task's own `uuid` is knowable.
- **Compile** (`compile_build`, single-threaded) - concatenates every
  per-task file under `build/tasks/*/` into the flat files padmasana's
  seeders actually read.

Depends on script 2's output (`build/tags.json`, `build/boards.json`,
`build/sections.json`) already being on disk - `build_teams_and_boards.py`
has to run first.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from asana_migration.jobs import JobQueue
from asana_migration.storage import Paths, read_json, write_json

from . import activity
from .asana_source import find_task_dir, iter_all_task_dirs
from .people import email_of

log = logging.getLogger("padmasana_migration.build_tasks")

# <img ...> tags in a comment's html_text carrying both of these attributes
# (attribute order isn't consistent across the export - seen both ways) are
# an inline reference to one of the same task's own attachments (DESIGN.md
# §5.7).
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_ASANA_GID_ATTR_RE = re.compile(r'data-asana-gid="(\d+)"')
_ASANA_TYPE_ATTACHMENT_RE = re.compile(r'data-asana-type="attachment"')
_ASSET_ID_RE = re.compile(r"asset_id=(\d+)")


@dataclass
class ReferenceData:
    registry: dict[str, dict]
    board_by_project_gid: dict[str, dict]  # asana project gid -> {uuid, name}
    section_by_asana_gid: dict[str, dict]  # asana section gid -> {uuid, board_uuid, board_asana_gid, name}
    tag_by_asana_gid: dict[str, dict]  # asana tag gid -> {uuid}


@dataclass
class BuildContext:
    data_paths: Paths
    build_dir: Path
    queue: JobQueue
    ref: ReferenceData


def load_reference_data(build_dir: Path, registry: dict[str, dict]) -> ReferenceData:
    boards = {b["asana_gid"]: b for b in read_json(build_dir / "boards.json", default=[]) or []}
    sections = {s["asana_gid"]: s for s in read_json(build_dir / "sections.json", default=[]) or []}
    tags = {t["asana_gid"]: t for t in read_json(build_dir / "tags.json", default=[]) or []}
    return ReferenceData(registry=registry, board_by_project_gid=boards, section_by_asana_gid=sections, tag_by_asana_gid=tags)


def _task_out_dir(build_dir: Path, task_gid: str) -> Path:
    return build_dir / "tasks" / task_gid


# --- pass 1 ---------------------------------------------------------------

def h_build_task_pass1(ctx: BuildContext, payload: dict) -> None:
    task_gid = payload["task_gid"]
    task_data_dir = find_task_dir(ctx.data_paths, task_gid)
    if task_data_dir is None:
        raise RuntimeError(f"task {task_gid} not found anywhere under {ctx.data_paths.root}")

    task = read_json(task_data_dir / "task.json", default=None)
    if task is None:
        raise RuntimeError(f"{task_data_dir}/task.json missing")
    stories = read_json(task_data_dir / "stories.json", default=None)
    comments_raw = read_json(task_data_dir / "comments.json", default=None)
    collaborators_raw = read_json(task_data_dir / "collaborators.json", default=[]) or []
    attachments_raw = read_json(task_data_dir / "attachments.json", default=[]) or []
    if stories is None or comments_raw is None:
        raise RuntimeError(f"task {task_gid}: stories/comments not imported yet - re-run `import-all` first")

    out_dir = _task_out_dir(ctx.build_dir, task_gid)
    task_uuid = _stable_uuid(out_dir / "task.json")

    created_by_email = _resolve_created_by(task, stories, ctx.ref)

    comments = _build_comments(out_dir, task_uuid, comments_raw)
    write_json(out_dir / "comments.json", comments)

    tags_out = _build_task_tags(task, task_uuid, ctx.ref)
    write_json(out_dir / "tags.json", tags_out)

    board_sections, section_tasks = _build_sections(task, task_uuid, ctx.ref)
    write_json(out_dir / "board_section.json", board_sections)
    write_json(out_dir / "section_task.json", section_tasks)

    collaborators_out = [
        {"task_uuid": task_uuid, "collaborator_email": email}
        for c in collaborators_raw
        if (email := email_of(c))
    ]
    write_json(out_dir / "task_collaborators.json", collaborators_out)

    task_attachments, comment_attachments = _build_attachments(
        ctx, out_dir, task_gid, task_uuid, attachments_raw, comments, stories, created_by_email,
    )
    write_json(out_dir / "attachments.json", task_attachments)
    write_json(out_dir / "comment_attachments.json", comment_attachments)

    activity_log = activity.build_activity_log(
        task=task, stories=stories, comments=comments,
        task_created_by_email=created_by_email, registry=ctx.ref.registry,
    )
    write_json(out_dir / "activity_log.json", activity_log)

    assignee_email = email_of(task.get("assignee"))
    task_out = {
        "asana_gid": task_gid,
        "uuid": task_uuid,
        "name": task.get("name"),
        "description": task.get("notes") or "",
        "assignee_email": assignee_email,
        "created_at": task.get("created_at"),
        "due_date": task.get("due_on") or ((task.get("due_at") or "")[:10] or None),
        "completed_at": task.get("completed_at"),
        "assigned_at": _resolve_assigned_at(task, stories, assignee_email),
        "created_by_email": created_by_email,
        "parent_asana_gid": (task.get("parent") or {}).get("gid"),
        "parent_task_uuid": None,  # patched by pass 2
    }
    write_json(out_dir / "task.json", task_out)
    log.info("Task '%s' (%s): built (%d comment(s), %d activity row(s))",
              task.get("name"), task_gid, len(comments), len(activity_log))


def _stable_uuid(existing_task_json_path: Path) -> str:
    """Reuses the `uuid` a previous run (or a previous, possibly incomplete
    attempt at this same job - a worker can crash mid-job, or its `running`
    status can go stale and get reclaimed, see `asana_migration.jobs`'s
    module docstring) already wrote for this task, rather than minting a
    fresh one every time. Matters most across full re-runs of this script
    (e.g. after fixing a bug, or picking up newly-imported tasks): if
    `build/` was already seeded into a real padmasana instance once, this
    keeps every already-seeded task's identity stable so §10's upsert
    matches existing rows instead of duplicating them."""
    existing = read_json(existing_task_json_path, default=None)
    if existing and existing.get("uuid"):
        return existing["uuid"]
    return str(uuid.uuid4())


def _resolve_created_by(task: dict, stories: list[dict], ref: ReferenceData) -> str | None:
    if stories:
        earliest = min(stories, key=lambda s: s.get("created_at") or "")
        email = email_of(earliest.get("created_by"))
        if email:
            return email
    for project in task.get("projects", []) or []:
        board = ref.board_by_project_gid.get(project.get("gid"))
        if board and board.get("created_by_email"):
            return board["created_by_email"]
    return email_of(task.get("assignee"))


def _resolve_assigned_at(task: dict, stories: list[dict], assignee_email: str | None) -> str | None:
    if not assignee_email:
        return None
    assigned_stories = [s for s in stories if s.get("resource_subtype") == "assigned"]
    if assigned_stories:
        return max(assigned_stories, key=lambda s: s.get("created_at") or "")["created_at"]
    return task.get("created_at")


def _build_comments(out_dir: Path, task_uuid: str, comments_raw: list[dict]) -> list[dict]:
    existing_by_gid = {c["asana_gid"]: c for c in (read_json(out_dir / "comments.json", default=[]) or [])}
    out = []
    for c in sorted(comments_raw, key=lambda c: c.get("created_at") or ""):
        gid = c.get("gid")
        existing = existing_by_gid.get(gid)
        out.append({
            "asana_gid": gid,
            "uuid": existing["uuid"] if existing else str(uuid.uuid4()),
            "task_uuid": task_uuid,
            "author_email": email_of(c.get("created_by")),
            "text": c.get("text") or "",
            "html_text": c.get("html_text") or "",
            "created_at": c.get("created_at"),
        })
    return out


def _build_task_tags(task: dict, task_uuid: str, ref: ReferenceData) -> list[dict]:
    out = []
    for tag in task.get("tags", []) or []:
        entry = ref.tag_by_asana_gid.get(tag.get("gid"))
        if entry is None:
            log.warning("Task %s: tag %s ('%s') not found in build/tags.json - skipping", task.get("gid"), tag.get("gid"), tag.get("name"))
            continue
        out.append({"task_uuid": task_uuid, "tag_uuid": entry["uuid"]})
    return out


def _build_sections(task: dict, task_uuid: str, ref: ReferenceData) -> tuple[list[dict], list[dict]]:
    board_sections: dict[str, dict] = {}
    section_tasks = []
    for membership in task.get("memberships", []) or []:
        section = membership.get("section")
        if not section or not section.get("gid"):
            continue
        entry = ref.section_by_asana_gid.get(section["gid"])
        if entry is None:
            log.warning("Task %s: section %s ('%s') not found in build/sections.json - skipping",
                        task.get("gid"), section.get("gid"), section.get("name"))
            continue
        board_sections[entry["uuid"]] = {
            "uuid": entry["uuid"],
            "board_uuid": entry["board_uuid"],
        }
        section_tasks.append({"task_uuid": task_uuid, "board_section_uuid": entry["uuid"]})
    return list(board_sections.values()), section_tasks


def _embedded_attachment_gids(html_text: str) -> set[str]:
    found = set()
    for img_tag in _IMG_TAG_RE.findall(html_text or ""):
        if not _ASANA_TYPE_ATTACHMENT_RE.search(img_tag):
            continue
        m = _ASANA_GID_ATTR_RE.search(img_tag)
        if m:
            found.add(m.group(1))
    return found


def _build_attachments(
    ctx: BuildContext, out_dir: Path, task_gid: str, task_uuid: str,
    attachments_raw: list[dict], comments: list[dict], stories: list[dict], created_by_email: str | None,
) -> tuple[list[dict], list[dict]]:
    uploaded_by_gid = read_json(_uploaded_attachments_path(ctx.build_dir, task_gid), default=[]) or []
    uploaded_by_gid = {a["asana_gid"]: a for a in uploaded_by_gid}

    # First comment (chronologically) that embeds a given attachment gid
    # wins (DESIGN.md §5.7); comments is already sorted by created_at.
    embedded_in: dict[str, dict] = {}
    for comment in comments:
        for gid in _embedded_attachment_gids(comment.get("html_text")):
            embedded_in.setdefault(gid, comment)

    attachment_added_by_asset_id: dict[str, dict] = {}
    for story in stories:
        if story.get("resource_subtype") != "attachment_added":
            continue
        m = _ASSET_ID_RE.search(story.get("html_text") or "")
        if m:
            attachment_added_by_asset_id[m.group(1)] = story

    task_level: list[dict] = []
    comment_level: list[dict] = []
    for att in attachments_raw:
        gid = att.get("gid")
        if not gid:
            continue
        uploaded = uploaded_by_gid.get(gid)
        if uploaded is None:
            log.warning("Task %s: attachment %s has no uploaded record in build/ yet - run `upload_attachments` "
                        "first, skipping for now", task_gid, gid)
            continue

        added_story = attachment_added_by_asset_id.get(gid)
        uploaded_by_email = email_of(added_story.get("created_by")) if added_story else created_by_email
        created_at = added_story.get("created_at") if added_story else att.get("created_at")

        comment = embedded_in.get(gid)
        if comment is not None:
            comment_level.append({
                "asana_gid": gid,
                "comment_uuid": comment["uuid"],
                "name": att.get("name"),
                "metadata": uploaded["metadata"],
                "uploaded_by_email": uploaded_by_email,
                "created_at": created_at,
            })
        else:
            task_level.append({
                "asana_gid": gid,
                "task_uuid": task_uuid,
                "name": att.get("name"),
                "metadata": uploaded["metadata"],
                "uploaded_by_email": uploaded_by_email,
                "created_at": created_at,
            })
    return task_level, comment_level


def _uploaded_attachments_path(build_dir: Path, task_gid: str) -> Path:
    # See upload_attachments.py's module docstring for why this isn't named
    # `attachments.json` - that name is this script's own task-level output.
    return build_dir / "tasks" / task_gid / "uploaded_attachments.json"


# --- queueing / pass 2 / compile -------------------------------------------

def queue_pass1_jobs(data_paths: Paths, queue: JobQueue) -> int:
    queued = 0
    for task_dir in iter_all_task_dirs(data_paths):
        task_gid = task_dir.name.split("_", 1)[0]
        job = queue.push("build_task_pass1", {"task_gid": task_gid}, dedupe_key=f"build_task:{task_gid}")
        if job:
            queued += 1
    return queued


def run_pass2(build_dir: Path) -> tuple[int, int]:
    """Level-by-level isn't needed here the way it is in §10's live seeder -
    every task's own `uuid` already exists on disk by the time pass 2 runs
    (pass 1's queue has fully drained), so a single sweep resolves every
    `parent_task_uuid` in one pass, regardless of how deep the tree goes."""
    tasks_dir = build_dir / "tasks"
    if not tasks_dir.exists():
        return 0, 0
    uuid_by_asana_gid: dict[str, str] = {}
    task_paths: list[Path] = []
    for task_dir in tasks_dir.iterdir():
        task_json_path = task_dir / "task.json"
        task = read_json(task_json_path, default=None)
        if task is None:
            continue
        task_paths.append(task_json_path)
        uuid_by_asana_gid[task["asana_gid"]] = task["uuid"]

    patched = missing = 0
    for task_json_path in task_paths:
        task = read_json(task_json_path, default={})
        parent_gid = task.get("parent_asana_gid")
        if not parent_gid:
            continue
        parent_uuid = uuid_by_asana_gid.get(parent_gid)
        if parent_uuid is None:
            log.warning("Task %s: parent %s not found among built tasks - leaving parent_task_uuid null",
                        task.get("asana_gid"), parent_gid)
            missing += 1
            continue
        if task.get("parent_task_uuid") != parent_uuid:
            task["parent_task_uuid"] = parent_uuid
            write_json(task_json_path, task)
            patched += 1
    return patched, missing


def compile_build(build_dir: Path) -> dict[str, int]:
    tasks_dir = build_dir / "tasks"
    tasks, task_tags, comments = [], [], []
    board_sections_by_uuid: dict[str, dict] = {}
    section_tasks_raw: list[dict] = []
    task_collaborators, task_attachments, comment_attachments, activity_rows = [], [], [], []
    created_at_by_task_uuid: dict[str, str] = {}

    if tasks_dir.exists():
        for task_dir in sorted(tasks_dir.iterdir()):
            task = read_json(task_dir / "task.json", default=None)
            if task is None:
                continue
            tasks.append(task)
            created_at_by_task_uuid[task["uuid"]] = task.get("created_at") or ""
            task_tags.extend(read_json(task_dir / "tags.json", default=[]) or [])
            comments.extend(read_json(task_dir / "comments.json", default=[]) or [])
            for bs in read_json(task_dir / "board_section.json", default=[]) or []:
                board_sections_by_uuid[bs["uuid"]] = bs
            section_tasks_raw.extend(read_json(task_dir / "section_task.json", default=[]) or [])
            task_collaborators.extend(read_json(task_dir / "task_collaborators.json", default=[]) or [])
            task_attachments.extend(read_json(task_dir / "attachments.json", default=[]) or [])
            comment_attachments.extend(read_json(task_dir / "comment_attachments.json", default=[]) or [])
            activity_rows.extend(read_json(task_dir / "activity_log.json", default=[]) or [])

    # section_task's `order` needs every task in a section known at once -
    # assigned here, at compile time, ordered by each task's own created_at
    # (DESIGN.md doesn't specify a source for this - Asana's own task-order-
    # within-a-section wasn't captured by the export - so this is the best
    # deterministic proxy available: oldest task first).
    by_section: dict[str, list[dict]] = {}
    for st in section_tasks_raw:
        by_section.setdefault(st["board_section_uuid"], []).append(st)
    section_tasks = []
    for rows in by_section.values():
        rows.sort(key=lambda r: created_at_by_task_uuid.get(r["task_uuid"], ""))
        for order, row in enumerate(rows):
            section_tasks.append({**row, "order": order})

    write_json(build_dir / "tasks.json", tasks)
    write_json(build_dir / "task_tags.json", task_tags)
    write_json(build_dir / "board_sections.json", list(board_sections_by_uuid.values()))
    write_json(build_dir / "section_tasks.json", section_tasks)
    write_json(build_dir / "task_collaborators.json", task_collaborators)
    write_json(build_dir / "attachments.json", task_attachments)
    write_json(build_dir / "comment_attachments.json", comment_attachments)
    write_json(build_dir / "comments.json", comments)
    write_json(build_dir / "task_activity_log.json", activity_rows)

    return {
        "tasks": len(tasks), "task_tags": len(task_tags), "board_sections": len(board_sections_by_uuid),
        "section_tasks": len(section_tasks), "task_collaborators": len(task_collaborators),
        "attachments": len(task_attachments), "comment_attachments": len(comment_attachments),
        "comments": len(comments), "activity_log": len(activity_rows),
    }


HANDLERS = {
    "build_task_pass1": h_build_task_pass1,
}
