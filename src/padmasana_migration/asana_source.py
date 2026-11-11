"""Small read-only helpers over the `data/` export shared by more than one
script in this package. `asana_migration.storage.Paths` already gives
`find_project_dir_anywhere`/`find_team_dir_anywhere` (tasks and projects are
pooled per workspace - see that module's docstring) but no task equivalent;
this adds the one that's missing rather than duplicating the scan in every
caller.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from asana_migration.storage import Paths


_TASK_DIR_INDEX: dict[Path, dict[str, Path]] = {}
_TASK_DIR_INDEX_LOCK = threading.Lock()


def _task_dir_index(root: Path) -> dict[str, Path]:
    """`task_gid -> task dir` for the whole export, built once per process.
    Scanning every workspace's `tasks/` for each lookup was O(tasks) per
    call - O(tasks^2) over a full build (~140 ms/call at ~6k tasks)."""
    index = _TASK_DIR_INDEX.get(root)
    if index is not None:
        return index
    with _TASK_DIR_INDEX_LOCK:
        index = _TASK_DIR_INDEX.get(root)
        if index is None:
            index = {}
            for workspace_dir in sorted(p for p in root.iterdir() if p.is_dir()):
                tasks_dir = workspace_dir / "tasks"
                if not tasks_dir.exists():
                    continue
                with os.scandir(tasks_dir) as it:
                    for entry in it:
                        if entry.is_dir():
                            index.setdefault(entry.name.split("_", 1)[0], Path(entry.path))
            _TASK_DIR_INDEX[root] = index
    return index


def find_task_dir(data_paths: Paths, task_gid: str) -> Path | None:
    if not data_paths.root.exists():
        return None
    found = _task_dir_index(data_paths.root).get(task_gid)
    if found is not None and found.exists():
        return found
    # Dir created after the index was built (e.g. a concurrent import) - rescan once.
    _TASK_DIR_INDEX.pop(data_paths.root, None)
    return _task_dir_index(data_paths.root).get(task_gid)


def iter_all_task_dirs(data_paths: Paths):
    if not data_paths.root.exists():
        return
    for workspace_dir in sorted(p for p in data_paths.root.iterdir() if p.is_dir()):
        tasks_dir = workspace_dir / "tasks"
        if not tasks_dir.exists():
            continue
        for task_dir in sorted(tasks_dir.iterdir()):
            if task_dir.is_dir():
                yield task_dir
