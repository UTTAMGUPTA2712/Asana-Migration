"""Local, gitignored runtime configuration for padmasana_migration.

Reads the Asana export `asana_migration` already wrote under `data/` (never
modified - see DESIGN.md §3) and writes everything this package produces
into a brand-new `build/` folder, gitignored the same way `data/` and `var/`
already are.

Shares the repo's `var/` directory with `asana_migration` for convenience,
but never its job queue file - each script here gets its own
(`var/padmasana_jobs.json`), so the two tools' progress tracking never
collides (DESIGN.md §4).
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VAR_DIR = Path(os.environ.get("ASANA_MIGRATION_VAR_DIR", REPO_ROOT / "var"))
DATA_DIR = Path(os.environ.get("ASANA_MIGRATION_DATA_DIR", REPO_ROOT / "data"))
BUILD_DIR = Path(os.environ.get("PADMASANA_MIGRATION_BUILD_DIR", REPO_ROOT / "build"))
JOBS_PATH = VAR_DIR / "padmasana_jobs.json"

# Bandwidth/CPU-bound concurrency knob for scripts 1 and 3 (DESIGN.md §4) -
# not derived from any rate limit, there's no Asana call to pace here.
DEFAULT_CONCURRENCY = 8
