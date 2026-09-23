"""Finds and inspects the task that best demonstrates all field types.

Scans all tasks under build/tasks/*/ to locate candidate tasks that contain:
- User mentions in description & comments
- Inline attachments & task-level attachments
- Links to Asana boards and tasks
- Subtasks, tags, collaborators, and comments
And displays full details including before/after HTML formatting.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from asana_migration.storage import read_json

from . import config
from .format_docs import FormatContext, format_html_content, load_format_context

log = logging.getLogger("padmasana_migration.inspect_task")

MENTION_RE = re.compile(r'data-asana-type=["\']user["\']|class=["\']mention["\']|<span\b[^>]*data-type=["\']mention["\']')
IMG_RE = re.compile(r'data-asana-type=["\']attachment["\']|<img\b')
LINK_RE = re.compile(r'https?://app\.asana\.com')


def find_richest_tasks(build_dir: Path, limit: int = 5) -> list[dict]:
    tasks_dir = build_dir / "tasks"
    if not tasks_dir.exists():
        return []

    candidates = []
    for task_dir in tasks_dir.iterdir():
        if not task_dir.is_dir():
            continue
        task = read_json(task_dir / "task.json", default={})
        if not task:
            continue
        comments = read_json(task_dir / "comments.json", default=[])
        attachments = read_json(task_dir / "attachments.json", default=[])
        comment_attachments = read_json(task_dir / "comment_attachments.json", default=[])
        tags = read_json(task_dir / "tags.json", default=[])
        collabs = read_json(task_dir / "task_collaborators.json", default=[])

        desc = task.get("description_html") or task.get("description") or ""
        comments_html = " ".join(c.get("html_text") or c.get("text") or "" for c in comments)

        has_desc_mention = bool(MENTION_RE.search(desc))
        has_comm_mention = bool(MENTION_RE.search(comments_html))
        has_desc_img = bool(IMG_RE.search(desc))
        has_comm_img = bool(IMG_RE.search(comments_html))
        has_desc_link = bool(LINK_RE.search(desc))
        has_comm_link = bool(LINK_RE.search(comments_html))
        has_att = len(attachments) > 0
        has_comm_att = len(comment_attachments) > 0
        has_tags = len(tags) > 0
        has_collabs = len(collabs) > 0
        has_assignee = bool(task.get("assignee_email"))

        score = (
            (3 if has_desc_mention else 0)
            + (3 if has_comm_mention else 0)
            + (3 if has_desc_img else 0)
            + (3 if has_comm_img else 0)
            + (2 if has_desc_link else 0)
            + (2 if has_comm_link else 0)
            + (2 if has_att else 0)
            + (2 if has_comm_att else 0)
            + (1 if has_tags else 0)
            + (1 if has_collabs else 0)
            + (1 if has_assignee else 0)
            + min(len(comments), 5)
        )

        candidates.append({
            "gid": task.get("asana_gid"),
            "name": task.get("name"),
            "uuid": task.get("uuid"),
            "score": score,
            "has_desc_mention": has_desc_mention,
            "has_comm_mention": has_comm_mention,
            "has_desc_img": has_desc_img,
            "has_comm_img": has_comm_img,
            "has_desc_link": has_desc_link,
            "has_comm_link": has_comm_link,
            "attachments_count": len(attachments),
            "comment_attachments_count": len(comment_attachments),
            "comments_count": len(comments),
            "tags_count": len(tags),
            "collabs_count": len(collabs),
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:limit]


def inspect_task(
    build_dir: Path,
    task_gid: str | None = None,
    ctx: FormatContext | None = None,
) -> dict:
    tasks_dir = build_dir / "tasks"
    if not task_gid:
        rich = find_richest_tasks(build_dir, limit=1)
        if not rich:
            raise RuntimeError(f"No tasks found under {tasks_dir}")
        task_gid = rich[0]["gid"]

    task_dir = tasks_dir / task_gid
    if not task_dir.exists():
        raise RuntimeError(f"Task directory {task_dir} does not exist.")

    task = read_json(task_dir / "task.json", default={})
    comments = read_json(task_dir / "comments.json", default=[])
    attachments = read_json(task_dir / "attachments.json", default=[])
    comment_attachments = read_json(task_dir / "comment_attachments.json", default=[])
    tags = read_json(task_dir / "tags.json", default=[])
    collabs = read_json(task_dir / "task_collaborators.json", default=[])

    raw_desc = task.get("description_html") or task.get("description") or ""
    formatted_desc = format_html_content(raw_desc, ctx) if ctx else raw_desc

    formatted_comments = []
    for c in comments:
        raw_c = c.get("html_text") or c.get("text") or ""
        fmt_c = format_html_content(raw_c, ctx) if ctx else raw_c
        formatted_comments.append({
            "asana_gid": c.get("asana_gid"),
            "author_email": c.get("author_email"),
            "raw_html": raw_c,
            "formatted_html": fmt_c,
        })

    return {
        "task_gid": task_gid,
        "uuid": task.get("uuid"),
        "name": task.get("name"),
        "assignee_email": task.get("assignee_email"),
        "created_by_email": task.get("created_by_email"),
        "due_date": task.get("due_date"),
        "tags_count": len(tags),
        "collaborators_count": len(collabs),
        "attachments_count": len(attachments),
        "comment_attachments_count": len(comment_attachments),
        "comments_count": len(comments),
        "raw_description": raw_desc,
        "formatted_description": formatted_desc,
        "comments": formatted_comments,
    }


def print_task_inspection(data: dict) -> None:
    print("\n" + "=" * 80)
    print(f"TASK INSPECTION REPORT: {data['name']} (GID: {data['task_gid']})")
    print("=" * 80)
    print(f"UUID:                {data['uuid']}")
    print(f"Assignee:            {data['assignee_email'] or 'None'}")
    print(f"Created By:          {data['created_by_email'] or 'None'}")
    print(f"Due Date:            {data['due_date'] or 'None'}")
    print(f"Tags Count:          {data['tags_count']}")
    print(f"Collaborators Count: {data['collaborators_count']}")
    print(f"Task Attachments:    {data['attachments_count']}")
    print(f"Comment Attachments: {data['comment_attachments_count']}")
    print(f"Comments Count:      {data['comments_count']}")

    print("\n" + "-" * 40 + " DESCRIPTION " + "-" * 40)
    print("[RAW DESCRIPTION]:")
    print(data["raw_description"][:500] + ("..." if len(data["raw_description"]) > 500 else ""))
    print("\n[FORMATTED DESCRIPTION]:")
    print(data["formatted_description"][:500] + ("..." if len(data["formatted_description"]) > 500 else ""))

    if data["comments"]:
        print("\n" + "-" * 40 + f" COMMENTS ({len(data['comments'])}) " + "-" * 40)
        for i, c in enumerate(data["comments"], 1):
            print(f"\n--- Comment #{i} by {c['author_email']} (GID: {c['asana_gid']}) ---")
            print("[RAW]:")
            print(c["raw_html"][:300] + ("..." if len(c["raw_html"]) > 300 else ""))
            print("[FORMATTED]:")
            print(c["formatted_html"][:300] + ("..." if len(c["formatted_html"]) > 300 else ""))
    print("=" * 80 + "\n")
