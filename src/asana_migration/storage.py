"""On-disk layout for the exported Asana data.

Asana lets both a **project** belong to more than one team, and a **task**
be simultaneously a subtask of one task and a top-level member of another
project — so "which team owns this project" and "which project/parent owns
this task" aren't 1:1. To avoid fetching (and duplicating on disk) the same
project or task once per team/parent that references it, both live in a
single shared pool per workspace, addressed only by gid; teams and projects
just hold lightweight indexes pointing into those pools::

    data/
      <workspace_gid>_<slug>/
        workspace.json
        projects/                        # every project, fetched once
          <project_gid>_<slug>/
            project.json    # project fields
            members.json    # project members (full list)
            sections.json   # ordered list of sections
            _meta.json      # import progress/status for this project
            _index.json     # this project's task tree: gid -> {depth, parent,
                             #   section, ...} - the actual task content lives
                             #   in tasks/ below, referenced by gid
        tasks/                            # every task/subtask, fetched once
          <task_gid>_<slug>/
            task.json
            comments.json       # stories of type "comment"
            collaborators.json  # followers, resolved
            attachments.json    # attachment metadata (all hosts)
            attachments/        # downloaded file bytes, Asana-hosted only -
                                 #   externally-hosted (Dropbox/Drive/Box/...)
                                 #   attachments are link-only in attachments.json
        teams/
          <team_gid>_<slug>/
            team.json
            projects_index.json   # [{gid, name, archived, ...}, ...] from
                                   # GET /teams/{gid}/projects - a pointer
                                   # list into data/<workspace>/projects/,
                                   # not a copy of the project data itself

Every directory that represents an importable "thing" also gets a
``_meta.json`` recording status/progress/timestamps, which is what the web UI
reads to show "Imported", "Importing... 12/40 tasks", etc. without ever
calling Asana itself.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from .config import DATA_DIR

_slug_re = re.compile(r"[^a-z0-9]+")


def slugify(name: str | None, fallback: str = "untitled") -> str:
    if not name:
        return fallback
    slug = _slug_re.sub("-", name.strip().lower()).strip("-")
    return slug[:60] or fallback


def gid_dir(base: Path, gid: str, name: str | None) -> Path:
    return base / f"{gid}_{slugify(name)}"


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default


def _unique_tmp_path(path: Path) -> Path:
    """A temp-file name nobody else's writer could also be using right now.

    Two processes (or two threads) writing the *same* target path at the
    same time used to both write through the identical `file.json.tmp` name;
    whichever called os.replace() first would silently consume the other's
    temp file out from under it, crashing the loser with FileNotFoundError.
    Including the pid, thread id and a nanosecond timestamp makes collision
    practically impossible - each writer gets its own temp file, and the
    last replace() to run simply wins (as intended), instead of one writer
    finding its temp file already gone.
    """
    return path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp_path(path)
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False))
    tmp.replace(path)


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp_path(path)
    tmp.write_bytes(data)
    tmp.replace(path)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Meta:
    """Read/update the `_meta.json` progress marker of one directory."""

    def __init__(self, dir_path: Path):
        self.path = dir_path / "_meta.json"

    def read(self) -> dict:
        return read_json(self.path, default={}) or {}

    def update(self, **fields) -> dict:
        data = self.read()
        data.update(fields)
        data["updated_at"] = now_iso()
        write_json(self.path, data)
        return data

    def set_status(self, status: str, **extra) -> dict:
        return self.update(status=status, **extra)

    def increment(self, field: str, amount: int = 1) -> dict:
        data = self.read()
        data[field] = data.get(field, 0) + amount
        data["updated_at"] = now_iso()
        write_json(self.path, data)
        return data


class TaskIndex:
    """Flat gid -> summary map of every task/subtask in one project's tree.

    Lets the web UI render a project's whole task tree from one small file
    instead of walking (and reading) every task.json on disk. The actual
    task content lives in the workspace's shared `tasks/` pool (see
    `Paths.task_dir`) - this only records how that gid fits into *this*
    project's hierarchy (depth, parent, section).
    """

    def __init__(self, project_dir: Path):
        self.path = project_dir / "_index.json"

    def read(self) -> dict:
        return read_json(self.path, default={}) or {}

    def update_entry(self, task_gid: str, **fields) -> None:
        data = self.read()
        entry = data.get(task_gid, {"gid": task_gid})
        entry.update(fields)
        data[task_gid] = entry
        write_json(self.path, data)


class Paths:
    """Resolves directory paths for every level of the hierarchy.

    `project_dir` and `task_dir` are keyed by gid under a shared per-workspace
    pool (see module docstring): calling them again for a gid that already
    has a directory returns that same directory rather than minting a new
    one, which is what lets a project/task shared across teams or parents
    be stored - and fetched from Asana - exactly once.
    """

    def __init__(self, root: Path = DATA_DIR):
        self.root = root

    def workspace_dir(self, workspace_gid: str, name: str | None = None) -> Path:
        existing = self._find_existing(self.root, workspace_gid)
        return existing or gid_dir(self.root, workspace_gid, name)

    def team_dir(self, workspace_dir: Path, team_gid: str, name: str | None = None) -> Path:
        base = workspace_dir / "teams"
        existing = self._find_existing(base, team_gid)
        return existing or gid_dir(base, team_gid, name)

    def project_dir(self, workspace_dir: Path, project_gid: str, name: str | None = None) -> Path:
        base = workspace_dir / "projects"
        existing = self._find_existing(base, project_gid)
        return existing or gid_dir(base, project_gid, name)

    def task_dir(self, workspace_dir: Path, task_gid: str, name: str | None = None) -> Path:
        base = workspace_dir / "tasks"
        existing = self._find_existing(base, task_gid)
        return existing or gid_dir(base, task_gid, name)

    @staticmethod
    def workspace_dir_of_team(team_dir: Path) -> Path:
        """team_dir is always <workspace_dir>/teams/<team>; undo that."""
        return team_dir.parent.parent

    @staticmethod
    def _find_existing(base: Path, gid: str) -> Path | None:
        if not base.exists():
            return None
        prefix = f"{gid}_"
        for child in base.iterdir():
            if child.is_dir() and (child.name == gid or child.name.startswith(prefix)):
                return child
        return None

    def find_workspace_dir_anywhere(self, workspace_gid: str) -> Path | None:
        return self._find_existing(self.root, workspace_gid)

    def find_team_dir_anywhere(self, team_gid: str) -> Path | None:
        if not self.root.exists():
            return None
        for ws in self.root.iterdir():
            if not ws.is_dir():
                continue
            found = self._find_existing(ws / "teams", team_gid)
            if found:
                return found
        return None

    def find_project_dir_anywhere(self, project_gid: str) -> Path | None:
        if not self.root.exists():
            return None
        for ws in self.root.iterdir():
            if not ws.is_dir():
                continue
            found = self._find_existing(ws / "projects", project_gid)
            if found:
                return found
        return None
