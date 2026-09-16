"""Everything about resolving *who* an Asana person is, from the export.

No script in this package writes or resolves padmasana identity rows - that
happens live, at seed time, against padmasana's own database (DESIGN.md
§5.3, §10). Every reference this package writes is just the person's Asana
`email`, carried straight through.

Two things still need doing entirely offline, from the export alone:

- **Picking a team/board's owner/creator** (DESIGN.md §2, §5.4) - Asana
  names none, so the rule is the lowest-gid member, overridable per team.
- **Resolving the *target* person of a `assigned`/`unassigned` story**
  (DESIGN.md §5.8) - Asana's Stories API gives no structured field for this,
  only an inline profile link buried in `html_text`. Recovering an email
  from that link's gid means already knowing every person's gid -> email in
  this workspace, which is what `build_person_registry` below collects, once,
  by scanning every place the export already carries a full `{gid, name,
  email}` person record.
"""

from __future__ import annotations

import re
from pathlib import Path

from asana_migration.storage import Paths, read_json

PROFILE_GID_RE = re.compile(r'/profile/(\d+)"')


def build_person_registry(data_paths: Paths) -> dict[str, dict]:
    """gid -> {"email":..., "name":...} for every person this export ever
    names with a full record (team/project members and followers, task
    assignees/followers/collaborators, and every comment's/story's
    `created_by`). Read-only, built once up front and never mutated again -
    safe to share across every worker thread without locking."""
    registry: dict[str, dict] = {}

    def add(person: dict | None) -> None:
        if not person or not person.get("gid"):
            return
        gid = person["gid"]
        existing = registry.get(gid)
        if existing is None:
            registry[gid] = {"email": person.get("email"), "name": person.get("name")}
        elif not existing.get("email") and person.get("email"):
            existing["email"] = person["email"]

    if not data_paths.root.exists():
        return registry

    for workspace_dir in data_paths.root.iterdir():
        if not workspace_dir.is_dir():
            continue

        teams_dir = workspace_dir / "teams"
        if teams_dir.exists():
            for team_dir in teams_dir.iterdir():
                for member in read_json(team_dir / "members.json", default=[]) or []:
                    add(member)

        projects_dir = workspace_dir / "projects"
        if projects_dir.exists():
            for project_dir in projects_dir.iterdir():
                project = read_json(project_dir / "project.json", default={}) or {}
                add(project.get("owner"))
                for member in project.get("members", []) or []:
                    add(member)
                for follower in project.get("followers", []) or []:
                    add(follower)
                for member in read_json(project_dir / "members.json", default=[]) or []:
                    add(member)

        tasks_dir = workspace_dir / "tasks"
        if tasks_dir.exists():
            for task_dir in tasks_dir.iterdir():
                task = read_json(task_dir / "task.json", default={}) or {}
                add(task.get("assignee"))
                for follower in task.get("followers", []) or []:
                    add(follower)
                for collaborator in read_json(task_dir / "collaborators.json", default=[]) or []:
                    add(collaborator)
                for comment in read_json(task_dir / "comments.json", default=[]) or []:
                    add(comment.get("created_by"))
                for story in read_json(task_dir / "stories.json", default=[]) or []:
                    add(story.get("created_by"))

    return registry


def email_of(person: dict | None) -> str | None:
    return (person or {}).get("email") or None


def lowest_gid_person(people: list[dict]) -> dict | None:
    """The stable, deterministic default for "no owner named" (DESIGN.md
    §2): the lowest-gid person among a team/project's own members. Asana
    gids are numeric strings, so this compares numerically, not
    lexicographically."""
    with_gid = [p for p in people if p.get("gid")]
    if not with_gid:
        return None
    return min(with_gid, key=lambda p: int(p["gid"]))


def resolve_owner_email(
    members: list[dict],
    registry: dict[str, dict],
    override_user_gid: str | None = None,
) -> tuple[str | None, str | None]:
    """Returns (email, source) where `source` is "override", "lowest_gid" or
    None (nobody resolvable at all - the caller has to decide how to handle
    that, e.g. skip the team/board and log a hard warning, since
    `owner_id`/`created_by` is NOT NULL on the padmasana side)."""
    if override_user_gid:
        person = registry.get(override_user_gid)
        if person and person.get("email"):
            return person["email"], "override"
    lowest = lowest_gid_person(members)
    if lowest and lowest.get("email"):
        return lowest["email"], "lowest_gid"
    if lowest and lowest.get("gid"):
        # Member record on hand has no email (e.g. only gid/name was ever
        # captured) - last resort, check the global registry for the same gid.
        person = registry.get(lowest["gid"])
        if person and person.get("email"):
            return person["email"], "lowest_gid"
    return None, None


def resolve_profile_link_email(html_text: str | None, registry: dict[str, dict]) -> tuple[str | None, str | None]:
    """For an `assigned`/`unassigned` story: the *target* person (the one
    being assigned/unassigned) is the last inline profile link in
    `html_text` (the first is always the actor, same person as the story's
    own `created_by`). Returns (email, gid) - email is None if that gid
    never showed up anywhere else in the export with a real email attached,
    in which case the caller falls back to the plain name in the story's
    `text` (DESIGN.md §5.8 - best-effort, same spirit as the renamed/
    description-updated caveat)."""
    gids = PROFILE_GID_RE.findall(html_text or "")
    if not gids:
        return None, None
    target_gid = gids[-1]
    person = registry.get(target_gid)
    return (person.get("email") if person else None), target_gid
