"""CLI entry points for the three scripts in DESIGN.md §4.

    uv run padmasana-build-teams-and-boards
    uv run padmasana-upload-attachments --file-service-url http://localhost:8080
    uv run padmasana-build-tasks --concurrency 8

(`uv run padmasana-migration <subcommand>` works too - all four console
scripts point at the same subcommands below.) Each is independently
resumable (DESIGN.md §4) - stop and re-run any of them any time.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

log = logging.getLogger("padmasana_migration.main")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
    )
    if not verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)


def _parse_team_owner_overrides(raw: list[str] | None) -> dict[str, str]:
    """`--team-owner <team_gid>=<user_gid>`, repeatable (DESIGN.md §2)."""
    overrides: dict[str, str] = {}
    for item in raw or []:
        if "=" not in item:
            raise SystemExit(f"--team-owner must be <team_gid>=<user_gid>, got: {item!r}")
        team_gid, user_gid = item.split("=", 1)
        overrides[team_gid.strip()] = user_gid.strip()
    return overrides


def _cmd_build_teams_and_boards(args: argparse.Namespace) -> None:
    _configure_logging(args.verbose)

    from . import config
    from .build_teams_and_boards import build, write_output
    from asana_migration.storage import Paths

    overrides = _parse_team_owner_overrides(args.team_owner)
    data_paths = Paths(root=config.DATA_DIR)

    log.info("Building teams/boards/sections/tags from %s ...", config.DATA_DIR)
    start = time.monotonic()
    result = build(data_paths, team_owner_overrides=overrides, build_dir=config.BUILD_DIR)
    write_output(config.BUILD_DIR, result)
    elapsed = time.monotonic() - start

    log.info(
        "=== Done in %.1fs: %d tag(s), %d team(s), %d board(s), %d section(s), "
        "%d board_team(s), %d board_member(s), %d team_member(s) ===",
        elapsed, len(result["tags"]), len(result["teams"]), len(result["boards"]), len(result["sections"]),
        len(result["board_teams"]), len(result["board_members"]), len(result["team_members"]),
    )
    log.info("Written under %s", config.BUILD_DIR)


def _cmd_upload_attachments(args: argparse.Namespace) -> None:
    _configure_logging(args.verbose)

    from . import config
    from .file_service_client import FileServiceClient
    from .upload_attachments import HANDLERS, UploadContext, queue_pending_uploads
    from asana_migration.jobs import JobQueue
    from asana_migration.storage import Paths
    from asana_migration.worker import WorkerPool

    data_paths = Paths(root=config.DATA_DIR)
    queue = JobQueue(path=config.JOBS_PATH)
    file_client = FileServiceClient(args.file_service_url, token=args.file_service_token)
    ctx = UploadContext(file_client=file_client, data_paths=data_paths, build_dir=config.BUILD_DIR, queue=queue)

    log.info("Scanning %s for attachments not uploaded yet...", config.DATA_DIR)
    queued, already_done = queue_pending_uploads(data_paths, config.BUILD_DIR, queue)
    is_upload_job = lambda j: j.type == "upload_attachment"  # noqa: E731
    outstanding_stats = queue.stats_for(is_upload_job)
    outstanding = outstanding_stats["queued"] + outstanding_stats["running"]
    log.info(
        "%d newly queued, %d already uploaded, %d total still outstanding.",
        queued, already_done, outstanding,
    )
    if not outstanding:
        log.info("Nothing to do.")
        return

    log.info("Uploading with %d worker(s)...", args.concurrency)
    start = time.monotonic()
    done_at_start = outstanding_stats["done"]
    pool = WorkerPool(ctx, handlers=HANDLERS)
    pool.start(worker_count=args.concurrency)
    last_report = start
    try:
        while True:
            stats = queue.stats_for(is_upload_job)
            if stats["queued"] == 0 and stats["running"] == 0:
                break
            now = time.monotonic()
            if now - last_report >= 15:
                log.info("Progress: %d uploaded so far | %d failed permanently",
                          stats["done"] - done_at_start, stats["error"])
                last_report = now
            time.sleep(1)
    finally:
        pool.stop()

    stats = queue.stats_for(is_upload_job)
    elapsed = time.monotonic() - start
    log.info("=== Done in %.1fs: %d uploaded, %d failed permanently ===",
              elapsed, stats["done"] - done_at_start, stats["error"])
    if stats["error"]:
        log.warning("Some uploads failed permanently after retries. Re-run to retry just those.")
    log.info("Written under %s/tasks/<task_gid>/attachments.json", config.BUILD_DIR)


def _cmd_build_tasks(args: argparse.Namespace) -> None:
    _configure_logging(args.verbose)

    from . import config
    from .build_tasks import HANDLERS, BuildContext, compile_build, load_reference_data, queue_pass1_jobs, run_pass2
    from .people import build_person_registry
    from asana_migration.jobs import JobQueue
    from asana_migration.storage import Paths
    from asana_migration.worker import WorkerPool

    data_paths = Paths(root=config.DATA_DIR)
    queue = JobQueue(path=config.JOBS_PATH)

    if args.compile_only:
        log.info("Compiling %s/tasks/*/ into the flat build/ files (no (re)building)...", config.BUILD_DIR)
        counts = compile_build(config.BUILD_DIR)
        log.info("=== Done: %s ===", ", ".join(f"{k}={v}" for k, v in counts.items()))
        return

    for required in ("boards.json", "sections.json", "tags.json"):
        if not (config.BUILD_DIR / required).exists():
            log.error("%s/%s missing - run `padmasana-build-teams-and-boards` first.", config.BUILD_DIR, required)
            raise SystemExit(1)

    log.info("Scanning workspace export to build the person registry (for assignee/prev_assignee resolution)...")
    registry = build_person_registry(data_paths)
    ref = load_reference_data(config.BUILD_DIR, registry)
    ctx = BuildContext(data_paths=data_paths, build_dir=config.BUILD_DIR, queue=queue, ref=ref)

    log.info("=== Pass 1/2: building every task (%d worker(s)) ===", args.concurrency)
    queued = queue_pass1_jobs(data_paths, queue)
    is_pass1_job = lambda j: j.type == "build_task_pass1"  # noqa: E731
    outstanding_stats = queue.stats_for(is_pass1_job)
    outstanding = outstanding_stats["queued"] + outstanding_stats["running"]
    log.info("%d newly queued, %d total still outstanding.", queued, outstanding)

    if outstanding:
        start = time.monotonic()
        done_at_start = outstanding_stats["done"]
        pool = WorkerPool(ctx, handlers=HANDLERS)
        pool.start(worker_count=args.concurrency)
        last_report = start
        try:
            while True:
                stats = queue.stats_for(is_pass1_job)
                if stats["queued"] == 0 and stats["running"] == 0:
                    break
                now = time.monotonic()
                if now - last_report >= 15:
                    log.info("Progress: %d built so far | %d failed permanently",
                              stats["done"] - done_at_start, stats["error"])
                    last_report = now
                time.sleep(1)
        finally:
            pool.stop()
        stats = queue.stats_for(is_pass1_job)
        log.info("Pass 1 done in %.1fs: %d built, %d failed permanently.",
                  time.monotonic() - start, stats["done"] - done_at_start, stats["error"])
        if stats["error"]:
            log.warning("Some tasks failed permanently after retries - re-run to retry just those "
                        "before trusting pass 2/compile below.")

    log.info("=== Pass 2/2: patching parent_task_uuid ===")
    patched, missing = run_pass2(config.BUILD_DIR)
    log.info("Patched %d task(s), %d with an unresolvable parent (left null).", patched, missing)

    if not args.no_compile:
        log.info("=== Compiling into the flat build/ files ===")
        counts = compile_build(config.BUILD_DIR)
        log.info("=== Done: %s ===", ", ".join(f"{k}={v}" for k, v in counts.items()))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="padmasana-migration")
    sub = parser.add_subparsers(dest="command")

    build_teams_and_boards = sub.add_parser(
        "build-teams-and-boards",
        help="Script 2: build teams.json/boards.json/sections.json/tags.json and their pivots from data/.",
    )
    build_teams_and_boards.add_argument(
        "--team-owner", action="append", metavar="TEAM_GID=USER_GID",
        help="Override the default lowest-gid-member owner for one team. Repeatable.",
    )
    build_teams_and_boards.add_argument("-v", "--verbose", action="store_true")
    build_teams_and_boards.set_defaults(func=_cmd_build_teams_and_boards)

    upload_attachments = sub.add_parser(
        "upload-attachments",
        help="Script 1: upload every downloaded attachment to the file-service.",
    )
    upload_attachments.add_argument("--file-service-url", required=True, help="e.g. http://localhost:8080")
    upload_attachments.add_argument("--file-service-token", help="Sent as Authorization: Bearer <token> if given (DESIGN.md §2).")
    upload_attachments.add_argument("--concurrency", type=int, default=8, metavar="N")
    upload_attachments.add_argument("-v", "--verbose", action="store_true")
    upload_attachments.set_defaults(func=_cmd_upload_attachments)

    build_tasks = sub.add_parser(
        "build-tasks",
        help="Script 3: build every task (comments, activity log, attachments split, tags, sections) from data/.",
    )
    build_tasks.add_argument("--concurrency", type=int, default=8, metavar="N")
    build_tasks.add_argument("--no-compile", action="store_true", help="Skip the final compile step.")
    build_tasks.add_argument("--compile", dest="compile_only", action="store_true",
                              help="Only run the compile step (re-concatenate build/tasks/*/ without rebuilding).")
    build_tasks.add_argument("-v", "--verbose", action="store_true")
    build_tasks.set_defaults(func=_cmd_build_tasks)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        raise SystemExit(1)
    args.func(args)


def build_teams_and_boards_main() -> None:
    main(["build-teams-and-boards", *sys.argv[1:]])


def upload_attachments_main() -> None:
    main(["upload-attachments", *sys.argv[1:]])


def build_tasks_main() -> None:
    main(["build-tasks", *sys.argv[1:]])


if __name__ == "__main__":
    main()
