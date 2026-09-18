#!/usr/bin/env python3
"""Thin standalone wrapper for the attachment-only retry pass (a subset of
what `asana-migration retry-all` does).

Kept as a plain script (like `estimate_attachment_storage.py`) for anyone
who wants to point it at a `data/`/`var/` pair that isn't `./data`/`./var`
without juggling env vars by hand, or run it without the package's console
scripts installed at all. The actual logic lives in
`asana_migration.importer.retry_failed_attachment_downloads` - see that
docstring, and the `retry-all` CLI command in main.py (specifically
`_cmd_retry_failed_attachments`, its attachment-only building block), for
details on why this needs its own pass instead of just re-running
`download-attachments` again.

Usage:
    uv run python scripts/retry_failed_attachments.py
    uv run python scripts/retry_failed_attachments.py --retries 6 --timeout 180 --concurrency 4
    uv run python scripts/retry_failed_attachments.py --data-dir /path/to/data --var-dir /path/to/var
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from asana_migration.main import (  # noqa: E402
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_CONCURRENCY,
    DEFAULT_RETRY_TIMEOUT,
    _cmd_retry_failed_attachments,
)


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=repo_root / "data",
                         help="Root of the exported data tree (default: ./data)")
    parser.add_argument("--var-dir", type=Path, default=repo_root / "var",
                         help="Root of var/ holding jobs.json and config.json (default: ./var)")
    parser.add_argument("--token", help="Asana personal access token (else uses the saved one, or prompts).")
    parser.add_argument("--rate-limit", type=int, default=None, metavar="RPM",
                         help="Requests/minute for the one metadata call per attachment - overrides the saved setting.")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRY_ATTEMPTS, metavar="N",
                         help=f"Attempts per attachment before giving up again (default {DEFAULT_RETRY_ATTEMPTS}).")
    parser.add_argument("--timeout", type=float, default=DEFAULT_RETRY_TIMEOUT, metavar="SECONDS",
                         help=f"Per-request timeout for the metadata call and the file download "
                              f"(default {DEFAULT_RETRY_TIMEOUT:.0f}s).")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_RETRY_CONCURRENCY, metavar="N",
                         help=f"How many failed attachments to retry at once (default {DEFAULT_RETRY_CONCURRENCY}).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log every attempt, not just the summary.")
    args = parser.parse_args()

    # asana_migration.config/.storage resolve their paths from these env vars
    # the first time either module is imported, which _cmd_retry_failed_
    # attachments (called below) only does lazily on its first line - so
    # setting them here, right before that call, is enough.
    os.environ["ASANA_MIGRATION_DATA_DIR"] = str(args.data_dir)
    os.environ["ASANA_MIGRATION_VAR_DIR"] = str(args.var_dir)

    _cmd_retry_failed_attachments(args)


if __name__ == "__main__":
    main()
