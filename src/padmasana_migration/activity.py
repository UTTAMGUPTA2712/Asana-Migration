"""Turns one task's `stories.json` (+ its already-built `comments`, DESIGN.md
§5.8) into padmasana's `task_activity_log` rows - the 13 fixed event types
in DESIGN.md §5.8's table, nothing else. Everything in `stories.json` with
no row in that table (custom fields, tags, dependencies, mentions, rule
automations, reactions, `comment_added` - covered instead via `comments.json`
directly, ...) is left out rather than force-fit.

Asana's story `text` gives *some* of what a fixed-shape event needs and not
the rest - documented per parser below. Where it's genuinely unrecoverable
(no per-event content, only the task's current, final state), the gap is
left as `""` rather than invented (DESIGN.md's own "best-effort" caveat for
`task_renamed`/`task_description_updated`).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .people import email_of, resolve_profile_link_email

# --- text parsers -----------------------------------------------------

_NAME_CHANGED_RE = re.compile(r'changed the name to "(.*)"\s*$', re.DOTALL)
_DUE_DATE_SET_RE = re.compile(r"changed the due date to (.+)$")
_SECTION_CHANGED_RE = re.compile(r'moved this task from "(.*?)" to "(.*?)" in (.+)$', re.DOTALL)
_ADDED_TO_PROJECT_RE = re.compile(r"added this task to (.+)$")
_REMOVED_FROM_PROJECT_RE = re.compile(r"removed from (.+)$")

_MONTH_DAY_YEAR_FORMATS = ("%b %d, %Y",)
_MONTH_DAY_FORMATS = ("%b %d",)


def _parse_due_date_text(date_text: str, story_created_at: str | None) -> str | None:
    date_text = date_text.strip()
    for fmt in _MONTH_DAY_YEAR_FORMATS:
        try:
            return datetime.strptime(date_text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    for fmt in _MONTH_DAY_FORMATS:
        try:
            parsed = datetime.strptime(date_text, fmt)
        except ValueError:
            continue
        year = datetime.now(timezone.utc).year
        if story_created_at:
            try:
                year = int(story_created_at[:4])
            except ValueError:
                pass
        return parsed.replace(year=year).strftime("%Y-%m-%d")
    return None


def _actor_email(story: dict) -> str | None:
    return email_of(story.get("created_by"))


# --- per-task synthesis -------------------------------------------------

def build_activity_log(
    *,
    task: dict,
    stories: list[dict],
    comments: list[dict],
    task_created_by_email: str | None,
    registry: dict,
) -> list[dict]:
    """`comments` is the list already built by `build_tasks.py` (each with
    its own generated `uuid`, in the same order as `comments.json`) - every
    entry becomes exactly one `task_comment_added` row here, keyed by that
    `uuid` (DESIGN.md §5.8). `stories.json`'s own `comment_added` entries are
    never read for this - `comments.json` is the source of truth."""
    events: list[dict] = []

    events.append({
        "event_type": "task_created",
        "actor_email": task_created_by_email,
        "created_at": task.get("created_at"),
        "data": {},
    })

    sorted_stories = sorted(stories, key=lambda s: s.get("created_at") or "")
    notes_changed_indexes = [i for i, s in enumerate(sorted_stories) if s.get("resource_subtype") == "notes_changed"]
    last_notes_changed_index = notes_changed_indexes[-1] if notes_changed_indexes else None
    running_name = None  # chain of task_renamed old_value <- previous new_value

    for i, story in enumerate(sorted_stories):
        subtype = story.get("resource_subtype")
        text = story.get("text") or ""
        actor = _actor_email(story)
        created_at = story.get("created_at")

        if subtype == "marked_complete":
            events.append({"event_type": "task_completed", "actor_email": actor, "created_at": created_at, "data": {}})
        elif subtype == "marked_incomplete":
            events.append({"event_type": "task_in_completed", "actor_email": actor, "created_at": created_at, "data": {}})
        elif subtype == "assigned":
            email, _ = resolve_profile_link_email(story.get("html_text"), registry)
            fallback_name = text.split(" assigned to ", 1)[-1] if " assigned to " in text else None
            events.append({
                "event_type": "task_assigned", "actor_email": actor, "created_at": created_at,
                "data": {"assignee": email or fallback_name},
            })
        elif subtype == "unassigned":
            email, _ = resolve_profile_link_email(story.get("html_text"), registry)
            fallback_name = text.split(" unassigned from ", 1)[-1] if " unassigned from " in text else None
            events.append({
                "event_type": "task_unassigned", "actor_email": actor, "created_at": created_at,
                "data": {"prev_assignee": email or fallback_name},
            })
        elif subtype == "name_changed":
            m = _NAME_CHANGED_RE.search(text)
            new_value = m.group(1) if m else ""
            events.append({
                "event_type": "task_renamed", "actor_email": actor, "created_at": created_at,
                "data": {"old_value": running_name or "", "new_value": new_value},
            })
            if new_value:
                running_name = new_value
        elif subtype == "notes_changed":
            # Asana's story text never carries the actual description
            # content (just "added"/"changed the description") - only the
            # task's own final `notes` is ever recoverable, and only for
            # whichever notes_changed event happened last.
            is_first = i == notes_changed_indexes[0]
            is_last = i == last_notes_changed_index
            new_value = (task.get("notes") or "") if is_last else ""
            if is_first:
                events.append({
                    "event_type": "task_description_added", "actor_email": actor, "created_at": created_at,
                    "data": {"value": new_value},
                })
            else:
                events.append({
                    "event_type": "task_description_updated", "actor_email": actor, "created_at": created_at,
                    "data": {"old_value": "", "new_value": new_value},
                })
        elif subtype == "due_date_changed":
            if "removed the due date" in text:
                due_date = None
            else:
                m = _DUE_DATE_SET_RE.search(text)
                due_date = _parse_due_date_text(m.group(1), created_at) if m else None
            events.append({
                "event_type": "task_due_date_updated", "actor_email": actor, "created_at": created_at,
                "data": {"due_date": due_date},
            })
        elif subtype == "added_to_project":
            m = _ADDED_TO_PROJECT_RE.search(text)
            events.append({
                "event_type": "task_added_to_board", "actor_email": actor, "created_at": created_at,
                "data": {"board_name": m.group(1) if m else None},
            })
        elif subtype == "removed_from_project":
            m = _REMOVED_FROM_PROJECT_RE.search(text)
            events.append({
                "event_type": "task_removed_from_board", "actor_email": actor, "created_at": created_at,
                "data": {"board_name": m.group(1) if m else None},
            })
        elif subtype == "section_changed":
            m = _SECTION_CHANGED_RE.search(text)
            events.append({
                "event_type": "task_moved_to_section", "actor_email": actor, "created_at": created_at,
                "data": {
                    "previous_section": m.group(1) if m else None,
                    "current_section": m.group(2) if m else None,
                    "board_name": m.group(3) if m else None,
                },
            })
        # else: no padmasana event type for this story subtype - left out
        # (DESIGN.md §5.8's final paragraph).

    for comment in comments:
        events.append({
            "event_type": "task_comment_added",
            "actor_email": comment.get("author_email"),
            "created_at": comment.get("created_at"),
            "data": {"comment_uuid": comment["uuid"]},
        })

    events.sort(key=lambda e: e.get("created_at") or "")

    return _collapse_consecutive(events)


def _collapse_consecutive(events: list[dict]) -> list[dict]:
    """Mirrors `TaskActivityLog.record()`'s own behavior (DESIGN.md §5.8):
    consecutive `task_renamed`/`task_description_updated` rows from the same
    actor merge into one (latest `new_value` wins) instead of piling up as
    separate rows."""
    collapsible = {"task_renamed", "task_description_updated"}
    out: list[dict] = []
    for event in events:
        if (
            out
            and event["event_type"] in collapsible
            and out[-1]["event_type"] == event["event_type"]
            and out[-1]["actor_email"] == event["actor_email"]
        ):
            out[-1]["data"]["new_value"] = event["data"]["new_value"]
            out[-1]["created_at"] = event["created_at"]
        else:
            out.append(event)
    return out
