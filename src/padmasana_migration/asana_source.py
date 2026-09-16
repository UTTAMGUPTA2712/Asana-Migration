"""Small read-only helpers over the `data/` export shared by more than one
script in this package. `asana_migration.storage.Paths` already gives
`find_project_dir_anywhere`/`find_team_dir_anywhere` (tasks and projects are
pooled per workspace - see that module's docstring) but no task equivalent;
this adds the one that's missing rather than duplicating the scan in every
caller.
"""

from __future__ import annotations

from pathlib import Path

from asana_migration.storage import Paths


def find_task_dir(data_paths: Paths, task_gid: str) -> Path | None:
    if not data_paths.root.exists():
        return None
    prefix = f"{task_gid}_"
    for workspace_dir in data_paths.root.iterdir():
        tasks_dir = workspace_dir / "tasks"
        if not tasks_dir.exists():
            continue
        for child in tasks_dir.iterdir():
            if child.is_dir() and (child.name == task_gid or child.name.startswith(prefix)):
                return child
    return None


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
