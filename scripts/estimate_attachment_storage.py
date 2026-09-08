#!/usr/bin/env python3
"""Thin standalone wrapper for `asana-migration estimate-storage`.

Kept as a plain script (rather than only the console command) for anyone
who wants to point it at a `data/` directory that isn't `./data` without
juggling `ASANA_MIGRATION_DATA_DIR`, or run it without the package's
console scripts installed at all. The actual logic lives in
`asana_migration.importer.estimate_attachment_storage` - see that
docstring, and the `estimate-storage` CLI command in main.py, for details.

Usage:
    uv run python scripts/estimate_attachment_storage.py
    uv run python scripts/estimate_attachment_storage.py --data-dir /path/to/data
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from asana_migration.importer import estimate_attachment_storage  # noqa: E402
from asana_migration.main import _human_bytes, _percentile  # noqa: E402
from asana_migration.storage import Paths  # noqa: E402


def main() -> None:
    default_data_dir = Path(__file__).resolve().parents[1] / "data"
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=default_data_dir,
                         help=f"Root of the exported data tree (default: {default_data_dir})")
    args = parser.parse_args()

    data_dir: Path = args.data_dir
    if not data_dir.exists():
        print(f"No data directory at {data_dir} - nothing imported yet.")
        raise SystemExit(1)

    stats = estimate_attachment_storage(Paths(root=data_dir))

    print(f"Scanned {stats['tasks_scanned']} task(s) under {data_dir}\n")

    print("Attachments by host:")
    for host, count in sorted(stats["by_host"].items(), key=lambda kv: -kv[1]):
        note = "" if host == "asana" else "  (link only - never downloaded)"
        print(f"  {host:10s} {count:6d}{note}")
    print()

    if stats["missing_size_count"]:
        print(f"Note: {stats['missing_size_count']} Asana-hosted attachment(s) have no reported size yet "
              f"(not counted below - re-fetch their metadata, e.g. via `import-all`, to know for sure).\n")

    print(f"Downloadable (host=asana):  {stats['asana_count']:6d} file(s), {_human_bytes(stats['downloadable_total_bytes'])} total")
    print(f"Already on disk:            {stats['downloaded_count']:6d} file(s), {_human_bytes(stats['downloaded_total_bytes'])}")
    print(f"Still to download:          {stats['remaining_count']:6d} file(s), {_human_bytes(stats['remaining_bytes'])}")
    print()

    sizes = stats["asana_sizes_sorted"]
    if sizes:
        print("Size distribution (all Asana-hosted attachments, downloaded or not):")
        print(f"  smallest: {_human_bytes(sizes[0])}")
        print(f"  median:   {_human_bytes(_percentile(sizes, 0.50))}")
        print(f"  p90:      {_human_bytes(_percentile(sizes, 0.90))}")
        print(f"  p99:      {_human_bytes(_percentile(sizes, 0.99))}")
        print(f"  largest:  {_human_bytes(sizes[-1])}")
        print()

    try:
        usage = shutil.disk_usage(data_dir)
        print(f"Free space on {data_dir}'s disk: {_human_bytes(usage.free)}")
        remaining = stats["remaining_bytes"]
        if remaining:
            if usage.free >= remaining:
                print(f"  -> enough room - {_human_bytes(usage.free - remaining)} left over after downloading the rest.")
            else:
                print(f"  -> NOT enough room - short by {_human_bytes(remaining - usage.free)}.")
        else:
            print("  -> nothing left to download.")
    except OSError as exc:
        print(f"Couldn't check free disk space: {exc}")


if __name__ == "__main__":
    main()
