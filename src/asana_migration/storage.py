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
        tags.json                        # every tag in the workspace (color,
                                          # notes, ...) - tasks only carry
                                          # {gid, name} refs to these
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
            stories.json         # every story - comments AND the system-
                                  #   generated activity log (status changes,
                                  #   reassignment, section moves, ...)
            comments.json        # just the type=="comment" subset of stories.json
            collaborators.json   # followers, resolved
            attachments.json     # attachment metadata + links (all hosts) -
                                  #   view_url/permanent_url/download_url as
                                  #   listed (the last expires fast, don't
                                  #   trust it later); each entry also gains
                                  #   local_path/downloaded_at once `asana-
                                  #   migration download-attachments` has
                                  #   actually fetched its bytes
            attachments/          # downloaded bytes for this task's Asana-
                                  #   hosted attachments (gdrive/external
                                  #   ones have no bytes to fetch), named
                                  #   <attachment_gid>_<original filename>
        teams/
          <team_gid>_<slug>/
            team.json
            members.json           # who's actually on this team (distinct
                                    #   from a project's own `members`)
            projects_index.json    # [{gid, name, archived, ...}, ...] from
                                    # GET /teams/{gid}/projects - a pointer
                                    # list into data/<workspace>/projects/,
                                    # not a copy of the project data itself

Every directory that represents an importable "thing" also gets a
``_meta.json`` recording status/progress/timestamps, which is what the web UI
reads to show "Imported", "Importing... 12/40 tasks", etc. without ever
calling Asana itself.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from pathlib import Path

from .config import DATA_DIR

try:
    import fcntl
    _HAVE_FLOCK = True
except ImportError:  # pragma: no cover - non-POSIX platform
    _HAVE_FLOCK = False

_slug_re = re.compile(r"[^a-z0-9]+")


@contextlib.contextmanager
def _locked(path: Path):
    """Cross-thread/cross-process exclusive lock for a read-modify-write
    cycle on `path`, via a sibling `.lock` file. Needed for anything more
    than one worker thread/process updates concurrently - `Meta`'s
    per-project counters and `TaskIndex`'s per-project map are both shared
    by every task in that project regardless of which worker processes it,
    so without this, concurrent increments/updates silently lose writes
    (last read-modify-write cycle to finish wins, the rest are clobbered)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not _HAVE_FLOCK:
        yield
        return
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def slugify(name: str | None, fallback: str = "untitled") -> str:
    if not name:
        return fallback
    slug = _slug_re.sub("-", name.strip().lower()).strip("-")
    return slug[:60] or fallback


def gid_dir(base: Path, gid: str, name: str | None) -> Path:
    return base / f"{gid}_{slugify(name)}"


_unsafe_filename_re = re.compile(r"[\\/\x00-\x1f]")


def safe_filename(name: str | None, fallback: str = "file") -> str:
    """Unlike `slugify`, keeps the real name (dots, case, spaces) intact -
    downloaded attachments need their actual extension to stay openable and
    to carry a sane content-type once pushed elsewhere. Only strips path
    separators and control characters, since the result becomes a filename
    on disk."""
    if not name:
        return fallback
    cleaned = _unsafe_filename_re.sub("_", name.strip())
    return cleaned[:200] or fallback


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
        with _locked(self.path):
            data = self.read()
            data.update(fields)
            data["updated_at"] = now_iso()
            write_json(self.path, data)
            return data

    def set_status(self, status: str, **extra) -> dict:
        return self.update(status=status, **extra)

    def increment(self, field: str, amount: int = 1) -> dict:
        with _locked(self.path):
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
        with _locked(self.path):
            data = self.read()
            entry = data.get(task_gid, {"gid": task_gid})
            entry.update(fields)
            data[task_gid] = entry
            write_json(self.path, data)


class AttachmentsFile:
    """Locked read/update of one task's `attachments.json`. Downloading a
    task's attachments can run several at once (one job per attachment, see
    `download_task_attachment` in importer.py), all writing back into this
    same file to record where each one landed on disk - so, like `Meta` and
    `TaskIndex`, every write goes through a read-modify-write under lock
    rather than clobbering whatever another worker just wrote."""

    def __init__(self, task_dir: Path):
        self.path = task_dir / "attachments.json"

    def read(self) -> list[dict]:
        return read_json(self.path, default=[]) or []

    def update_entry(self, attachment_gid: str, **fields) -> None:
        with _locked(self.path):
            data = self.read()
            for entry in data:
                if entry.get("gid") == attachment_gid:
                    entry.update(fields)
                    break
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
