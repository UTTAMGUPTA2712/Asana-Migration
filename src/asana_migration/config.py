"""Local, gitignored runtime configuration: the Asana token and tunables.

Everything the tool needs at runtime (the personal access token, the data
directory, the job queue, the rate limit) lives under ``var/`` at the
repository root so the whole thing is self-contained and easy to wipe by
deleting one folder. Nothing here is ever committed (see .gitignore).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

REPO_ROOT = Path(__file__).resolve().parents[2]
VAR_DIR = Path(os.environ.get("ASANA_MIGRATION_VAR_DIR", REPO_ROOT / "var"))
DATA_DIR = Path(os.environ.get("ASANA_MIGRATION_DATA_DIR", REPO_ROOT / "data"))
CONFIG_PATH = VAR_DIR / "config.json"
JOBS_PATH = VAR_DIR / "jobs.json"

DEFAULT_RATE_LIMIT_PER_MINUTE = 100  # conservative; Asana free tier is ~150/min

_lock = Lock()


@dataclass
class Config:
    token: str | None = None
    rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE
    max_subtask_depth: int = 6
    extra: dict = field(default_factory=dict)

    @property
    def has_token(self) -> bool:
        return bool(self.token)


def _ensure_dirs() -> None:
    VAR_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> Config:
    _ensure_dirs()
    if not CONFIG_PATH.exists():
        return Config()
    with _lock:
        raw = json.loads(CONFIG_PATH.read_text())
    return Config(
        token=raw.get("token"),
        rate_limit_per_minute=raw.get("rate_limit_per_minute", DEFAULT_RATE_LIMIT_PER_MINUTE),
        max_subtask_depth=raw.get("max_subtask_depth", 6),
        extra=raw.get("extra", {}),
    )


def save_config(cfg: Config) -> None:
    _ensure_dirs()
    payload = {
        "token": cfg.token,
        "rate_limit_per_minute": cfg.rate_limit_per_minute,
        "max_subtask_depth": cfg.max_subtask_depth,
        "extra": cfg.extra,
    }
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with _lock:
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(CONFIG_PATH)
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass


def set_token(token: str) -> Config:
    cfg = load_config()
    cfg.token = token.strip()
    save_config(cfg)
    return cfg
