"""CLI entry points for the three scripts in DESIGN.md §4, plus
`import-users` (identity, not in DESIGN.md - see `import_users.py`).

    uv run padmasana-import-users --workspace <ws> --organization-unit <ou>
    uv run padmasana-build-teams-and-boards
    uv run padmasana-upload-attachments --file-service-url http://localhost:8080
    uv run padmasana-build-tasks --concurrency 8

(`uv run padmasana-migration <subcommand>` works too - all five console
scripts point at the same subcommands below.) Each is independently
resumable (DESIGN.md §4) - stop and re-run any of them any time.
"""

from __future__ import annotations

import argparse
import logging
import os
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


def _cmd_map_users(args: argparse.Namespace) -> None:
    _configure_logging(args.verbose)

    from . import config
    from .import_users import report_missing_users
    from .people import build_user_map
    from asana_migration.storage import Paths

    data_paths = Paths(root=config.DATA_DIR)
    log.info("Building user-to-asana_gid map from %s and %s ...", config.DATA_DIR, config.BUILD_DIR)
    user_map = build_user_map(data_paths, config.BUILD_DIR)
    log.info("=== Done: %d user(s) mapped in %s/user_to_asana_gid.json ===", len(user_map), config.BUILD_DIR)
    report_missing_users(config.BUILD_DIR)


def _cmd_import_users(args: argparse.Namespace) -> None:
    _configure_logging(args.verbose)

    from . import config
    from .import_users import fetch_workspace_users, report_missing_users, validate_emails, write_users
    from .people import build_user_map
    from asana_migration.storage import Paths, read_json

    pad_cfg = config.get_padmasana_config()
    firebase_auth_api_url = args.firebase_auth_api_url or pad_cfg.get("firebase_auth_api_url") or os.environ.get("FIREBASE_AUTH_API_URL")
    workspace = args.workspace or pad_cfg.get("workspace")
    organization_unit = args.organization_unit or pad_cfg.get("organization_unit")

    if not firebase_auth_api_url:
        raise SystemExit("--firebase-auth-api-url (or env FIREBASE_AUTH_API_URL or config) is required.")
    if not workspace:
        raise SystemExit("--workspace (or config) is required.")
    if not organization_unit:
        raise SystemExit("--organization-unit (or config) is required.")

    if not args.no_save_config:
        config.save_padmasana_config(
            firebase_auth_api_url=firebase_auth_api_url,
            workspace=workspace,
            organization_unit=organization_unit,
        )

    log.info(
        "Fetching workspace users from %s (workspace=%s, organization_unit=%s)...",
        firebase_auth_api_url, workspace, organization_unit,
    )
    users = fetch_workspace_users(firebase_auth_api_url, workspace, organization_unit)
    path = write_users(config.BUILD_DIR, users, merge=not args.no_merge)
    # Validate against the full cumulative file, not just this run's batch -
    # otherwise a second import (a different workspace/org-unit) reports
    # every earlier-imported person as "missing" again, since they're not
    # in `users` (this run's fetch) even though they're already on disk.
    all_users = read_json(path, default=users) or users
    log.info("=== Done: %d user(s) fetched this run, %d total in %s ===", len(users), len(all_users), path)

    # Refresh user map
    data_paths = Paths(root=config.DATA_DIR)
    build_user_map(data_paths, config.BUILD_DIR)

    if not args.skip_validate:
        validate_emails(config.BUILD_DIR, all_users)
        report_missing_users(config.BUILD_DIR, all_users)


def _cmd_format_docs(args: argparse.Namespace) -> None:
    _configure_logging(args.verbose)

    from . import config
    from .format_docs import format_all_tasks, load_format_context
    from .import_users import report_missing_users
    from asana_migration.storage import Paths

    pad_cfg = config.get_padmasana_config()
    app_url = args.app_url or pad_cfg.get("app_url") or os.environ.get("PADMASANA_APP_URL") or "http://localhost:3000"
    file_service_url = args.file_service_url or pad_cfg.get("file_service_url") or os.environ.get("FILE_SERVICE_URL") or "http://localhost:8080"

    if not args.no_save_config and not args.dry_run:
        config.save_padmasana_config(app_url=app_url, file_service_url=file_service_url)

    data_paths = Paths(root=config.DATA_DIR)
    log.info("Loading format context (app_url=%s, file_service_url=%s)...", app_url, file_service_url)
    ctx = load_format_context(config.BUILD_DIR, app_url=app_url, file_service_url=file_service_url, data_paths=data_paths)

    log.info("Formatting HTML in tasks and comments (filter=%s, dry_run=%s)...", args.task_gid or "all", args.dry_run)
    start = time.monotonic()
    tasks_count, comments_count = format_all_tasks(config.BUILD_DIR, ctx, task_gid_filter=args.task_gid, dry_run=args.dry_run)
    elapsed = time.monotonic() - start

    log.info("=== Done in %.1fs: %d task(s) and %d comment(s) formatted ===", elapsed, tasks_count, comments_count)
    report_missing_users(config.BUILD_DIR)


def _cmd_inspect_task(args: argparse.Namespace) -> None:
    _configure_logging(args.verbose)

    from . import config
    from .format_docs import load_format_context
    from .inspect_task import find_richest_tasks, inspect_task, print_task_inspection
    from asana_migration.storage import Paths

    if args.list_candidates:
        candidates = find_richest_tasks(config.BUILD_DIR, limit=args.limit)
        print("\n" + "=" * 90)
        print(f"TOP {len(candidates)} CANDIDATE TASKS BY FIELD RICHNESS")
        print("=" * 90)
        print(f"{'GID':<18} {'Score':<6} {'Atts':<5} {'CommAtts':<8} {'Comms':<6} {'Mentions(D/C)':<14} {'Name'}")
        print("-" * 90)
        for c in candidates:
            m_str = f"{'Y' if c['has_desc_mention'] else '-'}/{'Y' if c['has_comm_mention'] else '-'}"
            print(f"{c['gid']:<18} {c['score']:<6} {c['attachments_count']:<5} {c['comment_attachments_count']:<8} {c['comments_count']:<6} {m_str:<14} {c['name'][:30]}")
        print("=" * 90 + "\n")
        return

    pad_cfg = config.get_padmasana_config()
    app_url = args.app_url or pad_cfg.get("app_url") or os.environ.get("PADMASANA_APP_URL") or "http://localhost:3000"
    file_service_url = args.file_service_url or pad_cfg.get("file_service_url") or os.environ.get("FILE_SERVICE_URL") or "http://localhost:8080"

    data_paths = Paths(root=config.DATA_DIR)
    ctx = load_format_context(config.BUILD_DIR, app_url=app_url, file_service_url=file_service_url, data_paths=data_paths)
    data = inspect_task(config.BUILD_DIR, task_gid=args.task_gid, ctx=ctx)
    print_task_inspection(data)



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
    from asana_migration.worker import STATUS_POLL_SECONDS, WorkerPool

    # Saved so `format-docs`/`inspect-task` build image/download URLs against
    # the same file service the files actually went to, instead of silently
    # falling back to their localhost default when the flag is left off.
    # The token is deliberately never saved.
    config.save_padmasana_config(file_service_url=args.file_service_url.rstrip("/"))

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
            time.sleep(STATUS_POLL_SECONDS)
    finally:
        pool.stop()

    stats = queue.stats_for(is_upload_job)
    elapsed = time.monotonic() - start
    log.info("=== Done in %.1fs: %d uploaded, %d failed permanently ===",
              elapsed, stats["done"] - done_at_start, stats["error"])
    if stats["error"]:
        log.warning("Some uploads failed permanently after retries. Re-run to retry just those.")
    log.info("Written under %s/tasks/<task_gid>/uploaded_attachments.json", config.BUILD_DIR)


def _cmd_build_tasks(args: argparse.Namespace) -> None:
    _configure_logging(args.verbose)

    from . import config
    from .build_tasks import HANDLERS, BuildContext, compile_build, load_reference_data, queue_pass1_jobs, run_pass2
    from .people import build_person_registry
    from asana_migration.jobs import JobQueue
    from asana_migration.storage import Paths
    from asana_migration.worker import STATUS_POLL_SECONDS, WorkerPool

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
                time.sleep(STATUS_POLL_SECONDS)
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

    from .import_users import report_missing_users
    report_missing_users(config.BUILD_DIR)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="padmasana-migration")
    sub = parser.add_subparsers(dest="command")

    map_users = sub.add_parser(
        "map-users",
        help="Scan Asana export and comments/mentions to build build/user_to_asana_gid.json and check against padmasana_users.json.",
    )
    map_users.add_argument("-v", "--verbose", action="store_true")
    map_users.set_defaults(func=_cmd_map_users)

    import_users = sub.add_parser(
        "import-users",
        help="Fetch padmasana's own workspace users - the exact same call padmasana-service's own "
             "`import-users` CLI command makes to the Firebase Authorization service - and save them "
             "to build/padmasana_users.json, then validate every email build/ references against it.",
    )
    import_users.add_argument(
        "--firebase-auth-api-url", default=os.environ.get("FIREBASE_AUTH_API_URL"),
        help="Base URL of the Firebase Authorization service (env: FIREBASE_AUTH_API_URL or config).",
    )
    import_users.add_argument("--workspace", help="Target workspace (as padmasana-service's own --workspace or config).")
    import_users.add_argument("--organization-unit", help="Target organization unit (as padmasana-service's own --organization-unit or config).")
    import_users.add_argument("--no-merge", action="store_true", help="Overwrite padmasana_users.json instead of merging.")
    import_users.add_argument("--no-save-config", action="store_true", help="Do not save options to var/config.json.")
    import_users.add_argument("--skip-validate", action="store_true", help="Only fetch/save - skip cross-checking build/ emails against the fetched list.")
    import_users.add_argument("-v", "--verbose", action="store_true")
    import_users.set_defaults(func=_cmd_import_users)

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

    format_docs = sub.add_parser(
        "format-docs",
        help="Transform HTML content in task descriptions and comments into Padmasana/Tiptap format (mentions, attachments, task/board links).",
    )
    format_docs.add_argument("--app-url", help="Padmasana App base URL (env: PADMASANA_APP_URL or config, default: http://localhost:3000).")
    format_docs.add_argument("--file-service-url", help="File service base URL (env: FILE_SERVICE_URL or config, default: http://localhost:8080).")
    format_docs.add_argument("--task-gid", help="Format only this specific task.")
    format_docs.add_argument("--dry-run", action="store_true", help="Preview transformations without writing to disk.")
    format_docs.add_argument("--no-save-config", action="store_true", help="Do not save URL arguments into var/config.json.")
    format_docs.add_argument("-v", "--verbose", action="store_true")
    format_docs.set_defaults(func=_cmd_format_docs)

    inspect_task_parser = sub.add_parser(
        "inspect-task",
        help="Find and inspect task(s) containing mentions, attachments, and links to verify formatting.",
    )
    inspect_task_parser.add_argument("--task-gid", help="Task GID to inspect (default: finds richest task automatically).")
    inspect_task_parser.add_argument("--list-candidates", action="store_true", help="List top candidate tasks ranked by field richness.")
    inspect_task_parser.add_argument("--limit", type=int, default=10, help="Number of candidates to list (default: 10).")
    inspect_task_parser.add_argument("--app-url", help="Padmasana App base URL for preview.")
    inspect_task_parser.add_argument("--file-service-url", help="File service base URL for preview.")
    inspect_task_parser.add_argument("-v", "--verbose", action="store_true")
    inspect_task_parser.set_defaults(func=_cmd_inspect_task)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        raise SystemExit(1)
    args.func(args)


def map_users_main() -> None:
    main(["map-users", *sys.argv[1:]])


def import_users_main() -> None:
    main(["import-users", *sys.argv[1:]])


def build_teams_and_boards_main() -> None:
    main(["build-teams-and-boards", *sys.argv[1:]])


def upload_attachments_main() -> None:
    main(["upload-attachments", *sys.argv[1:]])


def build_tasks_main() -> None:
    main(["build-tasks", *sys.argv[1:]])


def format_docs_main() -> None:
    main(["format-docs", *sys.argv[1:]])


def inspect_task_main() -> None:
    main(["inspect-task", *sys.argv[1:]])


if __name__ == "__main__":
    main()

