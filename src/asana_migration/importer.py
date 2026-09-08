"""Job handlers: each one fetches one Asana collection (paginating through
all of it if there's more than 100 records - see AsanaClient.paginate),
writes local JSON, and enqueues whatever follow-up work it discovered. The
worker (see worker.py) pops jobs one at a time through the shared rate
limiter, so this is also where the "go slowly" fan-out is bounded -- a
handler never loops around calling the API for *more* of the tree itself;
it always pushes a new job for that instead. Every handler logs what it's
about to fetch and what it found, so a run's terminal output narrates the
crawl in real time.

Projects and tasks are pooled per workspace (see storage.py's module
docstring) because Asana lets a project belong to more than one team, and a
task be a subtask of one task while also directly belonging to another
project. Every handler that touches a project or task gid checks the shared
pool first and only calls Asana if that gid has genuinely never been fetched
before in this workspace - so something shared across teams/projects/parents
is fetched exactly once no matter how many places reference it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .client import AsanaClient
from .jobs import JobQueue
from .storage import AttachmentsFile, Meta, Paths, TaskIndex, now_iso, read_json, safe_filename, write_bytes, write_json

log = logging.getLogger("asana_migration.importer")


@dataclass
class ImporterContext:
    client: AsanaClient
    paths: Paths
    queue: JobQueue
    max_subtask_depth: int = 6


# Cheap, synchronous, top-of-tree discovery (not queued: it's 1-2 calls and
# the browser is waiting on it directly).

OTHER_PROJECTS_TEAM_GID_PREFIX = "other-projects-"
OTHER_PROJECTS_TEAM_NAME = "Other projects (not on any of your teams)"


def other_projects_team_gid(workspace_gid: str) -> str:
    return f"{OTHER_PROJECTS_TEAM_GID_PREFIX}{workspace_gid}"


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

        log.info("Workspace '%s': fetching tags...", ws.get("name"))
        tags = list(ctx.client.get_tags_for_workspace(ws["gid"]))
        write_json(ws_dir / "tags.json", tags)
        log.info("Workspace '%s': found %d tag(s)", ws.get("name"), len(tags))

        # A synthetic "team" for projects you can see in this workspace but
        # that aren't on any team you belong to (org-public projects, or
        # ones you were added to individually) - GET /teams/{gid}/projects
        # would never surface these no matter how many real teams we walk.
        # It's a real directory built the same way as any other team's, so
        # every existing team-scoped route/UI works on it unmodified.
        other_gid = other_projects_team_gid(ws["gid"])
        other_dir = ctx.paths.team_dir(ws_dir, other_gid, OTHER_PROJECTS_TEAM_NAME)
        write_json(other_dir / "team.json", {"gid": other_gid, "name": OTHER_PROJECTS_TEAM_NAME, "is_virtual": True})
        teams_out.append({"gid": other_gid, "name": OTHER_PROJECTS_TEAM_NAME, "dir": str(other_dir), "is_virtual": True})

        result.append({"workspace": ws, "dir": str(ws_dir), "teams": teams_out})
    return result


def ensure_team_projects_index(ctx: ImporterContext, team_dir: Path, team_gid: str, force: bool = False) -> list[dict]:
    """Lightweight project listing for a team, cached to disk (paginated, so
    teams with more than 100 projects are still listed in full). This is
    just a pointer list - {gid, name, archived} - into the workspace's
    shared projects/ pool; the same project gid can legitimately show up in
    more than one team's list here if it belongs to more than one team."""
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


def ensure_team_members(ctx: ImporterContext, team_dir: Path, team_gid: str, force: bool = False) -> list[dict]:
    """Who's actually on this team, cached to disk - distinct from a
    project's own `members` (which is a different Asana relationship).
    Skipped for the synthetic "other projects" team, which has no members."""
    if team_gid.startswith(OTHER_PROJECTS_TEAM_GID_PREFIX):
        return []
    members_path = team_dir / "members.json"
    if not force:
        cached = read_json(members_path)
        if cached is not None:
            return cached
    log.info("Team %s: listing members...", team_gid)
    members = list(ctx.client.get_users_for_team(team_gid))
    log.info("Team %s: found %d member(s)", team_gid, len(members))
    write_json(members_path, members)
    return members


def ensure_other_projects_index(
    ctx: ImporterContext, other_team_dir: Path, workspace_gid: str,
    real_teams: list[tuple[str, Path]], force: bool = False,
) -> list[dict]:
    """Projects visible in this workspace that aren't on any team you
    belong to. Unlike a single real team's own listing, computing this
    accurately means first knowing every one of *your* teams' project sets
    (each cheap/cached already, or one list call if not) plus one workspace-
    wide list call - still small, just not quite as free as opening one
    team's own page, which is why it's fetched lazily on demand rather than
    eagerly for every workspace up front."""
    index_path = other_team_dir / "projects_index.json"
    if not force:
        cached = read_json(index_path)
        if cached is not None:
            return cached

    known_gids: set[str] = set()
    for team_gid, team_dir in real_teams:
        for p in ensure_team_projects_index(ctx, team_dir, team_gid):
            known_gids.add(p["gid"])

    log.info("Workspace %s: listing all visible projects to find ones outside your teams...", workspace_gid)
    all_projects = list(ctx.client.get_projects_for_workspace(workspace_gid))
    other = [p for p in all_projects if p["gid"] not in known_gids]
    log.info("Workspace %s: %d project(s) visible to you but not on any of your teams", workspace_gid, len(other))
    write_json(index_path, other)
    return other


def h_import_project(ctx: ImporterContext, payload: dict) -> None:
    team_dir = Path(payload["team_dir"])
    workspace_dir = ctx.paths.workspace_dir_of_team(team_dir)
    project_gid = payload["project_gid"]

    project_dir = ctx.paths.project_dir(workspace_dir, project_gid)
    if (project_dir / "project.json").exists():
        project = read_json(project_dir / "project.json", default={})
        log.info(
            "Project '%s' (%s): already imported in this workspace (shared across teams) - reusing it, 0 API calls",
            project.get("name"), project_gid,
        )
    else:
        log.info("Project %s: fetching metadata...", project_gid)
        project = ctx.client.get_project(project_gid)
        project_dir = ctx.paths.project_dir(workspace_dir, project_gid, project.get("name"))
        write_json(project_dir / "project.json", project)
        write_json(project_dir / "members.json", project.get("members", []))
        log.info(
            "Project '%s' (%s): metadata saved, %d member(s)",
            project.get("name"), project_gid, len(project.get("members", [])),
        )

    meta = Meta(project_dir)
    existing = meta.read()
    if existing.get("status") == "complete":
        log.info("Project '%s' (%s): already fully imported - nothing more to do.", project.get("name"), project_gid)
        return
    if existing.get("status") != "importing":
        meta.update(
            status="importing",
            project_gid=project_gid,
            project_name=project.get("name"),
            started_at=existing.get("started_at") or now_iso(),
            sections_total=existing.get("sections_total"),
            tasks_discovered=existing.get("tasks_discovered", 0),
            tasks_imported=existing.get("tasks_imported", 0),
            comments_imported=existing.get("comments_imported", 0),
        )

    ctx.queue.push(
        "import_sections",
        {"workspace_dir": str(workspace_dir), "project_dir": str(project_dir), "project_gid": project_gid},
        dedupe_key=f"sections:{project_gid}",
    )


def h_import_sections(ctx: ImporterContext, payload: dict) -> None:
    project_dir = Path(payload["project_dir"])
    workspace_dir = Path(payload["workspace_dir"])
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
                    "workspace_dir": str(workspace_dir),
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
            {"workspace_dir": str(workspace_dir), "project_dir": str(project_dir), "project_gid": project_gid},
            dedupe_key=f"projecttasks:{project_gid}",
        )


def _enqueue_task_stubs(ctx: ImporterContext, workspace_dir: Path, project_dir: Path, project_gid: str,
                        stubs: list[dict], section_gid: str | None, section_name: str | None) -> None:
    if stubs:
        Meta(project_dir).increment("tasks_discovered", len(stubs))
    for stub in stubs:
        # dedupe_key is scoped by *this* project: even though the task's own
        # content is only ever fetched once workspace-wide (see h_import_task),
        # each project that has the task in its tree still needs its own
        # pass here to record it in that project's _index.json.
        ctx.queue.push(
            "import_task",
            {
                "workspace_dir": str(workspace_dir),
                "project_dir": str(project_dir),
                "project_gid": project_gid,
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
    workspace_dir = Path(payload["workspace_dir"])
    section_name = payload.get("section_name")
    log.info("Section '%s' (%s): fetching tasks...", section_name, payload["section_gid"])
    stubs = list(ctx.client.get_tasks_for_section(payload["section_gid"]))
    log.info("Section '%s': found %d task(s)", section_name, len(stubs))
    _enqueue_task_stubs(
        ctx, workspace_dir, project_dir, payload["project_gid"], stubs,
        payload["section_gid"], section_name,
    )


def h_import_project_tasks(ctx: ImporterContext, payload: dict) -> None:
    project_dir = Path(payload["project_dir"])
    workspace_dir = Path(payload["workspace_dir"])
    log.info("Project %s: fetching tasks (no sections)...", payload["project_gid"])
    stubs = list(ctx.client.get_tasks_for_project(payload["project_gid"]))
    log.info("Project %s: found %d task(s)", payload["project_gid"], len(stubs))
    _enqueue_task_stubs(ctx, workspace_dir, project_dir, payload["project_gid"], stubs, None, None)


def h_import_task(ctx: ImporterContext, payload: dict) -> None:
    project_dir = Path(payload["project_dir"])
    workspace_dir = Path(payload["workspace_dir"])
    project_gid = payload["project_gid"]
    task_gid = payload["task_gid"]
    depth = payload["depth"]

    task_dir = ctx.paths.task_dir(workspace_dir, task_gid)
    if (task_dir / "task.json").exists():
        task = read_json(task_dir / "task.json", default={})
        log.info(
            "Task '%s' (%s): already imported elsewhere in this workspace (shared) - reusing it, 0 API calls",
            task.get("name"), task_gid,
        )
    else:
        log.info("Task %s ('%s'): fetching detail...", task_gid, payload.get("task_name"))
        task = ctx.client.get_task(task_gid)
        task_dir = ctx.paths.task_dir(workspace_dir, task_gid, task.get("name"))
        write_json(task_dir / "task.json", task)
        write_json(task_dir / "collaborators.json", task.get("followers", []))
        log.info(
            "Task '%s' (%s): saved, %d collaborator(s), %d subtask(s)",
            task.get("name"), task_gid, len(task.get("followers", [])), task.get("num_subtasks", 0),
        )

    comments = read_json(task_dir / "comments.json", default=None)
    attachments = read_json(task_dir / "attachments.json", default=None)
    index_fields = dict(
        name=task.get("name"),
        completed=task.get("completed", False),
        depth=depth,
        parent_task_gid=payload.get("parent_task_gid"),
        section_gid=payload.get("section_gid"),
        section_name=payload.get("section_name"),
        num_subtasks=task.get("num_subtasks", 0),
        assignee=(task.get("assignee") or {}).get("name"),
        status="imported",
    )
    # Only set *_count once genuinely known - not "0 meaning not fetched
    # yet" - so project_view() can tell "fetched, turned out to be empty"
    # apart from "not fetched yet" purely from whether the key is present.
    if comments is not None:
        index_fields["comments_count"] = len(comments)
    if attachments is not None:
        index_fields["attachments_count"] = len(attachments)
    TaskIndex(project_dir).update_entry(task_gid, **index_fields)
    # tasks_imported is derived from the index itself in project_view(), not
    # tracked here - see the comment there for why.

    if comments is None:
        ctx.queue.push(
            "import_task_comments",
            {"workspace_dir": str(workspace_dir), "project_dir": str(project_dir), "task_gid": task_gid},
            dedupe_key=f"comments:{task_gid}",
        )

    if attachments is None:
        # No cheap "has attachments" flag on the task object, so this is
        # pushed unconditionally per task - like comments, fetched once
        # ever per gid regardless of how many projects reference it.
        ctx.queue.push(
            "import_task_attachments",
            {"workspace_dir": str(workspace_dir), "project_dir": str(project_dir), "task_gid": task_gid},
            dedupe_key=f"attachments:{task_gid}",
        )

    if task.get("num_subtasks", 0) and depth < ctx.max_subtask_depth:
        ctx.queue.push(
            "import_subtasks",
            {
                "workspace_dir": str(workspace_dir),
                "project_dir": str(project_dir),
                "project_gid": project_gid,
                "task_gid": task_gid,
                "depth": depth,
            },
            dedupe_key=f"subtasks:{task_gid}",
        )


def h_import_task_comments(ctx: ImporterContext, payload: dict) -> None:
    """Saves both `stories.json` (everything - comments and the full
    system-generated activity log) and `comments.json` (just the
    `type == "comment"` subset, kept separately since that's what the UI's
    Comments panel and the `comments_imported` progress counter use)."""
    workspace_dir = Path(payload["workspace_dir"])
    project_dir = Path(payload["project_dir"])
    task_gid = payload["task_gid"]
    task_dir = ctx.paths.task_dir(workspace_dir, task_gid)
    log.info("Task %s: fetching stories (comments + activity)...", task_gid)
    stories = list(ctx.client.get_stories_for_task(task_gid))
    comments = [s for s in stories if s.get("type") == "comment"]
    write_json(task_dir / "stories.json", stories)
    write_json(task_dir / "comments.json", comments)
    TaskIndex(project_dir).update_entry(task_gid, comments_count=len(comments))
    # comments_imported is derived from the index itself in project_view()
    log.info(
        "Task %s: saved %d stor(y/ies) (%d comment(s), %d other activity)",
        task_gid, len(stories), len(comments), len(stories) - len(comments),
    )


def h_import_task_attachments(ctx: ImporterContext, payload: dict) -> None:
    """Metadata + links only - never downloads the file. `download_url` is a
    short-lived signed URL (expires quickly), so it's saved for convenience
    but don't rely on it later; `permanent_url` is the durable reference, and
    re-fetching `GET /attachments/{gid}` at download time gets a fresh
    `download_url` when one is actually needed."""
    workspace_dir = Path(payload["workspace_dir"])
    project_dir = Path(payload["project_dir"])
    task_gid = payload["task_gid"]
    task_dir = ctx.paths.task_dir(workspace_dir, task_gid)
    log.info("Task %s: fetching attachments...", task_gid)
    attachments = list(ctx.client.get_attachments_for_task(task_gid))

    write_json(task_dir / "attachments.json", attachments)
    TaskIndex(project_dir).update_entry(task_gid, attachments_count=len(attachments))
    log.info("Task %s: saved %d attachment link(s) (no download)", task_gid, len(attachments))


def h_download_task_attachment(ctx: ImporterContext, payload: dict) -> None:
    """Downloads one attachment's actual bytes to
    `<task_dir>/attachments/<gid>_<name>`. Deliberately its own job (queued
    by `queue_pending_attachment_downloads`, run separately via the
    `download-attachments` CLI command) rather than folded into
    `h_import_task_attachments` above: that handler only ever lists
    metadata, run once per task during the main crawl; downloading can be
    re-run independently, any time later, without re-walking the tree.

    Always re-fetches the attachment fresh via `get_attachment` rather than
    trusting whatever `download_url` is sitting in attachments.json - that
    one was minted whenever the metadata was last listed and is very likely
    already expired by the time this job actually runs. The fresh URL from
    this call is used within the same request/response cycle, well inside
    its ~30 minute lifetime."""
    workspace_dir = Path(payload["workspace_dir"])
    task_gid = payload["task_gid"]
    attachment_gid = payload["attachment_gid"]
    task_dir = ctx.paths.task_dir(workspace_dir, task_gid)

    attachment = ctx.client.get_attachment(attachment_gid)
    if attachment.get("host") != "asana" or not attachment.get("download_url"):
        # gdrive/external attachments are links Asana never hosted bytes
        # for (download_url is null) - the saved view_url is already the
        # whole story, nothing to fetch.
        log.info("Attachment %s: host=%s, no bytes to download - skipping", attachment_gid, attachment.get("host"))
        return

    filename = f"{attachment_gid}_{safe_filename(attachment.get('name'))}"
    dest = task_dir / "attachments" / filename
    expected_size = attachment.get("size")
    if dest.exists() and (expected_size is None or dest.stat().st_size == expected_size):
        log.info("Task %s: attachment %s already downloaded (%s) - skipping", task_gid, attachment_gid, filename)
    else:
        log.info("Task %s: downloading attachment %s '%s' (%s bytes)...",
                  task_gid, attachment_gid, attachment.get("name"), expected_size)
        # pace=False: this is a CloudFront/S3 fetch, not an Asana API call -
        # see download_file's docstring. Concurrency (how many of these can
        # ever run at once) is bounded by the worker pool's --concurrency
        # instead of the API rate limiter.
        content = ctx.client.download_file(attachment["download_url"], pace=False)
        write_bytes(dest, content)
        log.info("Task %s: attachment %s saved (%d bytes) to %s", task_gid, attachment_gid, len(content), dest)

    AttachmentsFile(task_dir).update_entry(
        attachment_gid,
        local_path=str(dest.relative_to(task_dir)),
        downloaded_at=now_iso(),
    )


def queue_pending_attachment_downloads(ctx: ImporterContext) -> tuple[int, int]:
    """Walks every already-imported task's attachments.json on disk and
    queues a `download_task_attachment` job for each Asana-hosted
    attachment that isn't saved to disk yet (gdrive/external attachments -
    `host != "asana"` - are skipped, they have no bytes to fetch). Driven
    entirely from local files, not from Asana's project/task tree, so this
    can run as its own pass any time after (or during, on a re-run) the
    main import - see the `download-attachments` CLI command.

    Returns (queued, already_done) purely for the caller's log line."""
    queued = already_done = 0
    for attachments_path in ctx.paths.root.glob("*/tasks/*/attachments.json"):
        task_dir = attachments_path.parent
        workspace_dir = task_dir.parent.parent
        task_gid = task_dir.name.split("_", 1)[0]
        attachments = read_json(attachments_path, default=[]) or []
        for att in attachments:
            if att.get("host") != "asana" or not att.get("gid"):
                continue
            local_path = att.get("local_path")
            if local_path and (task_dir / local_path).exists():
                already_done += 1
                continue
            job = ctx.queue.push(
                "download_task_attachment",
                {"workspace_dir": str(workspace_dir), "task_gid": task_gid, "attachment_gid": att["gid"]},
                dedupe_key=f"download_attachment:{att['gid']}",
            )
            if job:
                queued += 1
    return queued, already_done


def h_import_subtasks(ctx: ImporterContext, payload: dict) -> None:
    workspace_dir = Path(payload["workspace_dir"])
    project_dir = Path(payload["project_dir"])
    project_gid = payload["project_gid"]
    parent_task_gid = payload["task_gid"]
    parent_depth = payload["depth"]

    log.info("Task %s: fetching subtasks (depth %d)...", parent_task_gid, parent_depth)
    stubs = list(ctx.client.get_subtasks_for_task(parent_task_gid))
    log.info("Task %s: found %d subtask(s)", parent_task_gid, len(stubs))
    if stubs:
        Meta(project_dir).increment("tasks_discovered", len(stubs))
    for stub in stubs:
        ctx.queue.push(
            "import_task",
            {
                "workspace_dir": str(workspace_dir),
                "project_dir": str(project_dir),
                "project_gid": project_gid,
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
    "import_task_attachments": h_import_task_attachments,
    "import_subtasks": h_import_subtasks,
    "download_task_attachment": h_download_task_attachment,
}


# Read-side helpers for the web layer: derive a live status without ever
# calling Asana, by combining the on-disk _meta.json with the queue's own
# view of what's still pending for that project.

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

    # tasks_imported/comments_imported come from the actual index, not the
    # separately-incremented counters in _meta.json: a job that gets reclaimed
    # as stale (its owning process died or was killed mid-run - see jobs.py)
    # and reprocessed can call increment() twice for the same completion if
    # the original execution wasn't actually dead yet, silently inflating the
    # counter. The index itself can't be double-counted this way - writing
    # the same gid's entry twice just overwrites it, so counting its entries
    # is always accurate regardless of how many times a job ran.
    index = TaskIndex(project_dir).read()
    accurate_imported = len(index)
    accurate_comments = sum(1 for e in index.values() if "comments_count" in e)
    if meta.get("tasks_imported") != accurate_imported or meta.get("comments_imported") != accurate_comments:
        meta = Meta(project_dir).update(tasks_imported=accurate_imported, comments_imported=accurate_comments)

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
    for section in sections:
        gid = section["gid"]
        tasks = sorted(by_section.get(gid, []), key=lambda e: e.get("name") or "")
        tree.append({
            "section": section,
            "tasks": [attach_children(t) for t in tasks],
        })
    leftover = by_section.get(None, [])  # list/board projects have no sections
    if leftover:
        tree.append({
            "section": None,
            "tasks": [attach_children(t) for t in sorted(leftover, key=lambda e: e.get("name") or "")],
        })
    return tree
