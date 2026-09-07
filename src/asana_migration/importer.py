"""Job handlers: each one fetches one Asana collection (paginating through
all of it if there's more than 100 records - see AsanaClient.paginate),
writes local JSON, and enqueues whatever follow-up work it discovered. The
worker (see worker.py) pops jobs one at a time through the shared rate
limiter, so this is also where the "go slowly" fan-out is bounded -- a
handler never loops around calling the API for *more* of the tree itself;
it always pushes a new job for that instead. Every handler logs what it's
about to fetch and what it found, so a run's terminal output narrates the
crawl in real time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .client import AsanaClient
from .jobs import JobQueue
from .storage import Meta, Paths, TaskIndex, now_iso, read_json, write_json

log = logging.getLogger("asana_migration.importer")


@dataclass
class ImporterContext:
    client: AsanaClient
    paths: Paths
    queue: JobQueue
    max_subtask_depth: int = 6


# ---------------------------------------------------------------------------
# Cheap, synchronous, top-of-tree discovery (not queued: it's 1-2 calls and
# the browser is waiting on it directly).
# ---------------------------------------------------------------------------

def import_workspaces_and_teams(ctx: ImporterContext) -> list[dict]:
    log.info("Authenticating and listing workspaces...")
    me = ctx.client.get_me()
    log.info("Authenticated as %s (%s)", me.get("name"), me.get("email"))
    result = []
    for ws in ctx.client.get_workspaces():
        log.info("Workspace '%s' (%s): listing teams...", ws.get("name"), ws["gid"])
        ws_dir = ctx.paths.workspace_dir(ws["gid"], ws.get("name"))
        write_json(ws_dir / "workspace.json", ws)
        teams_out = []
        for team in ctx.client.get_teams_for_workspace(me["gid"], ws["gid"]):
            team_dir = ctx.paths.team_dir(ws_dir, team["gid"], team.get("name"))
            write_json(team_dir / "team.json", team)
            teams_out.append({"gid": team["gid"], "name": team.get("name"), "dir": str(team_dir)})
        log.info("Workspace '%s': found %d team(s)", ws.get("name"), len(teams_out))
        result.append({"workspace": ws, "dir": str(ws_dir), "teams": teams_out})
    return result


def ensure_team_projects_index(ctx: ImporterContext, team_dir: Path, team_gid: str, force: bool = False) -> list[dict]:
    """Lightweight project listing for a team, cached to disk (paginated, so
    teams with more than 100 projects are still listed in full)."""
    index_path = team_dir / "projects_index.json"
    if not force:
        cached = read_json(index_path)
        if cached is not None:
            return cached
    log.info("Team %s: listing projects...", team_gid)
    projects = list(ctx.client.get_projects_for_team(team_gid))
    log.info("Team %s: found %d project(s)", team_gid, len(projects))
    write_json(index_path, projects)
    return projects


# ---------------------------------------------------------------------------
# Queued deep-import handlers
# ---------------------------------------------------------------------------

def h_import_project(ctx: ImporterContext, payload: dict) -> None:
    team_dir = Path(payload["team_dir"])
    project_gid = payload["project_gid"]
    log.info("Project %s: fetching metadata...", project_gid)
    project = ctx.client.get_project(project_gid)
    project_dir = ctx.paths.project_dir(team_dir, project_gid, project.get("name"))
    write_json(project_dir / "project.json", project)
    write_json(project_dir / "members.json", project.get("members", []))
    log.info(
        "Project '%s' (%s): metadata saved, %d member(s)",
        project.get("name"), project_gid, len(project.get("members", [])),
    )

    meta = Meta(project_dir)
    existing = meta.read()
    if existing.get("status") != "importing":
        meta.update(
            status="importing",
            project_gid=project_gid,
            project_name=project.get("name"),
            started_at=existing.get("started_at") or now_iso(),
            sections_total=None,
            tasks_discovered=0,
            tasks_imported=0,
            comments_imported=0,
        )

    ctx.queue.push(
        "import_sections",
        {"project_dir": str(project_dir), "project_gid": project_gid},
        dedupe_key=f"sections:{project_gid}",
    )


def h_import_sections(ctx: ImporterContext, payload: dict) -> None:
    project_dir = Path(payload["project_dir"])
    project_gid = payload["project_gid"]
    log.info("Project %s: fetching sections...", project_gid)
    sections = list(ctx.client.get_sections_for_project(project_gid))
    write_json(project_dir / "sections.json", sections)
    Meta(project_dir).update(sections_total=len(sections))
    log.info("Project %s: found %d section(s)", project_gid, len(sections))

    if sections:
        for section in sections:
            ctx.queue.push(
                "import_section_tasks",
                {
                    "project_dir": str(project_dir),
                    "project_gid": project_gid,
                    "section_gid": section["gid"],
                    "section_name": section.get("name"),
                },
                dedupe_key=f"sectiontasks:{section['gid']}",
            )
    else:
        ctx.queue.push(
            "import_project_tasks",
            {"project_dir": str(project_dir), "project_gid": project_gid},
            dedupe_key=f"projecttasks:{project_gid}",
        )


def _enqueue_task_stubs(ctx: ImporterContext, project_dir: Path, project_gid: str, stubs: list[dict],
                        section_gid: str | None, section_name: str | None) -> None:
    if stubs:
        Meta(project_dir).increment("tasks_discovered", len(stubs))
    tasks_dir = project_dir / "tasks"
    for stub in stubs:
        ctx.queue.push(
            "import_task",
            {
                "project_dir": str(project_dir),
                "project_gid": project_gid,
                "tasks_dir": str(tasks_dir),
                "task_gid": stub["gid"],
                "task_name": stub.get("name"),
                "depth": 0,
                "parent_task_gid": None,
                "section_gid": section_gid,
                "section_name": section_name,
            },
            dedupe_key=f"task:{project_gid}:{stub['gid']}",
        )


def h_import_section_tasks(ctx: ImporterContext, payload: dict) -> None:
    project_dir = Path(payload["project_dir"])
    section_name = payload.get("section_name")
    log.info("Section '%s' (%s): fetching tasks...", section_name, payload["section_gid"])
    stubs = list(ctx.client.get_tasks_for_section(payload["section_gid"]))
    log.info("Section '%s': found %d task(s)", section_name, len(stubs))
    _enqueue_task_stubs(
        ctx, project_dir, payload["project_gid"], stubs,
        payload["section_gid"], section_name,
    )


def h_import_project_tasks(ctx: ImporterContext, payload: dict) -> None:
    project_dir = Path(payload["project_dir"])
    log.info("Project %s: fetching tasks (no sections)...", payload["project_gid"])
    stubs = list(ctx.client.get_tasks_for_project(payload["project_gid"]))
    log.info("Project %s: found %d task(s)", payload["project_gid"], len(stubs))
    _enqueue_task_stubs(ctx, project_dir, payload["project_gid"], stubs, None, None)


def h_import_task(ctx: ImporterContext, payload: dict) -> None:
    project_dir = Path(payload["project_dir"])
    project_gid = payload["project_gid"]
    tasks_dir = Path(payload["tasks_dir"])
    task_gid = payload["task_gid"]

    log.info("Task %s ('%s'): fetching detail...", task_gid, payload.get("task_name"))
    task = ctx.client.get_task(task_gid)
    task_dir = ctx.paths.task_dir(tasks_dir, task_gid, task.get("name"))
    write_json(task_dir / "task.json", task)
    write_json(task_dir / "collaborators.json", task.get("followers", []))
    log.info(
        "Task '%s' (%s): saved, %d collaborator(s), %d subtask(s)",
        task.get("name"), task_gid, len(task.get("followers", [])), task.get("num_subtasks", 0),
    )

    TaskIndex(project_dir).update_entry(
        task_gid,
        name=task.get("name"),
        completed=task.get("completed", False),
        depth=payload["depth"],
        parent_task_gid=payload.get("parent_task_gid"),
        section_gid=payload.get("section_gid"),
        section_name=payload.get("section_name"),
        path=str(task_dir.relative_to(project_dir)),
        num_subtasks=task.get("num_subtasks", 0),
        assignee=(task.get("assignee") or {}).get("name"),
        status="imported",
    )
    Meta(project_dir).increment("tasks_imported", 1)

    ctx.queue.push(
        "import_task_comments",
        {"project_dir": str(project_dir), "task_dir": str(task_dir), "task_gid": task_gid},
        dedupe_key=f"comments:{task_gid}",
    )

    if task.get("num_subtasks", 0) and payload["depth"] < ctx.max_subtask_depth:
        ctx.queue.push(
            "import_subtasks",
            {
                "project_dir": str(project_dir),
                "project_gid": project_gid,
                "task_dir": str(task_dir),
                "task_gid": task_gid,
                "depth": payload["depth"],
            },
            dedupe_key=f"subtasks:{task_gid}",
        )


def h_import_task_comments(ctx: ImporterContext, payload: dict) -> None:
    project_dir = Path(payload["project_dir"])
    task_dir = Path(payload["task_dir"])
    task_gid = payload["task_gid"]
    log.info("Task %s: fetching comments...", task_gid)
    stories = list(ctx.client.get_stories_for_task(task_gid))
    comments = [s for s in stories if s.get("type") == "comment"]
    write_json(task_dir / "comments.json", comments)
    Meta(project_dir).increment("comments_imported", 1)
    TaskIndex(project_dir).update_entry(task_gid, comments_count=len(comments))
    log.info("Task %s: saved %d comment(s)", task_gid, len(comments))


def h_import_subtasks(ctx: ImporterContext, payload: dict) -> None:
    project_dir = Path(payload["project_dir"])
    project_gid = payload["project_gid"]
    parent_task_gid = payload["task_gid"]
    parent_depth = payload["depth"]
    task_dir = Path(payload["task_dir"])
    subtasks_dir = task_dir / "subtasks"

    log.info("Task %s: fetching subtasks (depth %d)...", parent_task_gid, parent_depth)
    stubs = list(ctx.client.get_subtasks_for_task(parent_task_gid))
    log.info("Task %s: found %d subtask(s)", parent_task_gid, len(stubs))
    if stubs:
        Meta(project_dir).increment("tasks_discovered", len(stubs))
    for stub in stubs:
        ctx.queue.push(
            "import_task",
            {
                "project_dir": str(project_dir),
                "project_gid": project_gid,
                "tasks_dir": str(subtasks_dir),
                "task_gid": stub["gid"],
                "task_name": stub.get("name"),
                "depth": parent_depth + 1,
                "parent_task_gid": parent_task_gid,
                "section_gid": None,
                "section_name": None,
            },
            dedupe_key=f"task:{project_gid}:{stub['gid']}",
        )


HANDLERS = {
    "import_project": h_import_project,
    "import_sections": h_import_sections,
    "import_section_tasks": h_import_section_tasks,
    "import_project_tasks": h_import_project_tasks,
    "import_task": h_import_task,
    "import_task_comments": h_import_task_comments,
    "import_subtasks": h_import_subtasks,
}


# ---------------------------------------------------------------------------
# Read-side helpers for the web layer: derive a live status without ever
# calling Asana, by combining the on-disk _meta.json with the queue's own
# view of what's still pending for that project.
# ---------------------------------------------------------------------------

def project_view(queue: JobQueue, project_dir: Path, project_gid: str | None = None) -> dict:
    def matches(job) -> bool:
        payload = job.payload
        return payload.get("project_dir") == str(project_dir) or (
            project_gid is not None and payload.get("project_gid") == project_gid
        )

    meta = Meta(project_dir).read()
    project = read_json(project_dir / "project.json", default={}) or {}
    if not meta:
        pending = queue.pending_count_for(matches)
        status = "importing" if pending else "not_imported"
        return {"status": status, "project": project, "pending_jobs": pending, "error_jobs": 0}

    pending = queue.pending_count_for(matches)
    status = meta.get("status", "not_imported")
    if status == "importing" and pending == 0:
        meta = Meta(project_dir).update(status="complete", completed_at=now_iso())
        status = "complete"
    errors = queue.errors_for(matches)
    return {
        "status": status,
        "meta": meta,
        "project": project,
        "pending_jobs": pending,
        "error_jobs": len(errors),
    }


def build_task_tree(project_dir: Path) -> list[dict]:
    """Group the flat task index into a per-section tree of tasks/subtasks."""
    index = TaskIndex(project_dir).read()
    sections = read_json(project_dir / "sections.json", default=[]) or []

    children_by_parent: dict[str | None, list[dict]] = {}
    for gid, entry in index.items():
        parent = entry.get("parent_task_gid")
        children_by_parent.setdefault(parent, []).append(entry)

    def attach_children(node: dict) -> dict:
        kids = sorted(children_by_parent.get(node["gid"], []), key=lambda e: e.get("name") or "")
        node = dict(node)
        node["subtasks"] = [attach_children(k) for k in kids]
        return node

    top_level = children_by_parent.get(None, [])
    by_section: dict[str | None, list[dict]] = {}
    for entry in top_level:
        by_section.setdefault(entry.get("section_gid"), []).append(entry)

    tree = []
    seen_sections = set()
    for section in sections:
        gid = section["gid"]
        seen_sections.add(gid)
        tasks = sorted(by_section.get(gid, []), key=lambda e: e.get("name") or "")
        tree.append({
            "section": section,
            "tasks": [attach_children(t) for t in tasks],
        })
    # tasks with no section (list/board projects, or section not yet imported)
    leftover = by_section.get(None, [])
    if leftover:
        tree.append({
            "section": None,
            "tasks": [attach_children(t) for t in sorted(leftover, key=lambda e: e.get("name") or "")],
        })
    return tree
