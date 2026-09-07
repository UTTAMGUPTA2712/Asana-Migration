"""On-disk layout for the exported Asana data.

The folder tree mirrors Asana's own hierarchy so it reads naturally with a
plain file browser::

    data/
      <workspace_gid>_<slug>/
        workspace.json
        teams/
          <team_gid>_<slug>/
            team.json
            projects/
              <project_gid>_<slug>/
                project.json        # project fields
                members.json        # project members (full list)
                sections.json       # ordered list of sections
                _meta.json          # import progress/status for this project
                tasks/
                  <task_gid>_<slug>/
                    task.json
                    comments.json       # stories of type "comment"
                    collaborators.json  # followers, resolved
                    subtasks/
                      <subtask_gid>_<slug>/   # same shape, recursively

Every directory that represents an importable "thing" also gets a
``_meta.json`` recording status/progress/timestamps, which is what the web UI
reads to show "Imported", "Importing... 12/40 tasks", etc. without ever
calling Asana itself.
"""

from __future__ import annotations

import json
import re
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


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False))
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
    """Flat gid -> summary map for every task/subtask imported under a project.

    Lets the web UI render the whole task tree of a project from one small
    file instead of walking (and reading) every task.json on disk.
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
    """Resolves directory paths for every level of the hierarchy."""

    def __init__(self, root: Path = DATA_DIR):
        self.root = root

    def workspace_dir(self, workspace_gid: str, name: str | None = None) -> Path:
        existing = self._find_existing(self.root, workspace_gid)
        return existing or gid_dir(self.root, workspace_gid, name)

    def team_dir(self, workspace_dir: Path, team_gid: str, name: str | None = None) -> Path:
        base = workspace_dir / "teams"
        existing = self._find_existing(base, team_gid)
        return existing or gid_dir(base, team_gid, name)

    def project_dir(self, team_dir: Path, project_gid: str, name: str | None = None) -> Path:
        base = team_dir / "projects"
        existing = self._find_existing(base, project_gid)
        return existing or gid_dir(base, project_gid, name)

    def task_dir(self, parent_tasks_dir: Path, task_gid: str, name: str | None = None) -> Path:
        existing = self._find_existing(parent_tasks_dir, task_gid)
        return existing or gid_dir(parent_tasks_dir, task_gid, name)

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
            teams_dir = ws / "teams"
            if not teams_dir.exists():
                continue
            for team in teams_dir.iterdir():
                found = self._find_existing(team / "projects", project_gid)
                if found:
                    return found
        return None
