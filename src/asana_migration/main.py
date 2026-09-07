"""CLI entry point.

    asana-migration serve            # start the web UI + background importer
    asana-migration serve --port 5050 --no-browser

    asana-migration import-all       # run the ENTIRE export in one foreground
                                      # run: every team, every project, every
                                      # task/subtask/comment - rate-limited,
                                      # resumable, logging every step.
    asana-migration import-all --force --rate-limit 60 -v

Everything else (setting the token from the browser, importing one project at
a time) is done from the web UI this launches -- see webapp.py. `import-all`
is the unattended alternative: point it at a token once and let it run to
completion (or Ctrl+C and re-run later - the job queue on disk picks up
exactly where it left off either way, and both entry points share the same
`var/` and `data/` directories).
"""

from __future__ import annotations

import argparse
import getpass
import logging
import time
import webbrowser
from pathlib import Path

log = logging.getLogger("asana_migration.import_all")


def _cmd_serve(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from .webapp import create_app

    app = create_app()
    url = f"http://{args.host}:{args.port}"
    print(f"Asana migration UI running at {url}")
    print("Data is written under ./data ; token/queue state under ./var")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    app.run(host=args.host, port=args.port, threaded=True, debug=False, use_reloader=False)


def _cmd_import_all(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # Quiet down third-party request/connection noise unless -v was asked for.
    if not args.verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    from . import config as config_mod
    from .client import AsanaAuthError, AsanaClient
    from .importer import (
        HANDLERS,
        ImporterContext,
        ensure_other_projects_index,
        ensure_team_projects_index,
        import_workspaces_and_teams,
        project_view,
    )
    from .jobs import JobQueue
    from .rate_limiter import RateLimiter
    from .storage import Meta, Paths

    cfg = config_mod.load_config()
    token = args.token or cfg.token
    if not token:
        token = getpass.getpass("Asana personal access token: ").strip()
    if not token:
        log.error("No token provided. Pass --token, or run once with the token to save it.")
        raise SystemExit(1)

    if args.rate_limit:
        cfg.rate_limit_per_minute = args.rate_limit
    cfg.token = token
    config_mod.save_config(cfg)

    log.info("Rate limit: %d requests/minute (change with --rate-limit or in the web UI's Settings)",
              cfg.rate_limit_per_minute)

    rate_limiter = RateLimiter(cfg.rate_limit_per_minute)
    client = AsanaClient(token, rate_limiter)
    try:
        client.get_me()
    except AsanaAuthError as exc:
        log.error("%s", exc)
        raise SystemExit(1)

    paths = Paths()
    queue = JobQueue()
    ctx = ImporterContext(client=client, paths=paths, queue=queue, max_subtask_depth=cfg.max_subtask_depth)

    log.info("=== Step 1/2: discovering workspaces, teams and projects ===")
    discovered = import_workspaces_and_teams(ctx)

    considered: list[tuple[Path, str, str]] = []  # (project_dir, project_gid, project_name)
    seen_project_gids: set[str] = set()  # a project can belong to more than one team
    total_projects = 0
    queued_projects = 0
    for ws in discovered:
        workspace_dir = Path(ws["dir"])
        real_teams = [(t["gid"], Path(t["dir"])) for t in ws["teams"] if not t.get("is_virtual")]
        for team in ws["teams"]:
            team_dir = Path(team["dir"])
            if team.get("is_virtual"):
                projects = ensure_other_projects_index(ctx, team_dir, ws["workspace"]["gid"], real_teams, force=args.force)
            else:
                projects = ensure_team_projects_index(ctx, team_dir, team["gid"], force=args.force)
            for p in projects:
                if p["gid"] in seen_project_gids:
                    continue
                seen_project_gids.add(p["gid"])
                total_projects += 1
                project_dir = paths.project_dir(workspace_dir, p["gid"], p.get("name"))
                considered.append((project_dir, p["gid"], p.get("name")))
                meta = Meta(project_dir).read()
                if not args.force and meta.get("status") == "complete":
                    log.info("Project '%s' (%s): already fully imported on %s - skipping (use --force to redo)",
                              p.get("name"), p["gid"], meta.get("completed_at"))
                    continue
                job = queue.push(
                    "import_project",
                    {"team_dir": str(team_dir), "project_gid": p["gid"]},
                    dedupe_key=f"project:{p['gid']}",
                    force=args.force,
                )
                if job:
                    queued_projects += 1

    log.info(
        "Discovery done: %d workspace(s), %d project(s) total, %d newly queued for import.",
        len(discovered), total_projects, queued_projects,
    )

    log.info("=== Step 2/2: draining the import queue (this is the slow, rate-limited part) ===")
    start = time.monotonic()
    processed = 0
    last_report = start
    while True:
        stats = queue.stats()
        if stats["queued"] == 0 and stats["running"] == 0:
            break
        job = queue.pop_next()
        if job is None:
            # Nothing ready right now (jobs are waiting out a retry backoff) - wait a bit.
            time.sleep(1)
            continue
        handler = HANDLERS.get(job.type)
        try:
            handler(ctx, job.payload)
            queue.complete(job.id)
            processed += 1
        except Exception as exc:  # noqa: BLE001 - keep the run alive; queue.fail() handles retry/backoff
            log.warning("job #%d (%s) failed: %s", job.id, job.type, exc)
            try:
                queue.fail(job.id, str(exc))
            except Exception as record_exc:  # noqa: BLE001 - never let recording a failure crash the run
                log.warning("job #%d: also failed to record that failure: %s", job.id, record_exc)

        now = time.monotonic()
        if now - last_report >= 15:
            stats = queue.stats()
            log.info(
                "Progress: %d job(s) completed so far | queue: %d queued, %d running, %d failed permanently",
                processed, stats["queued"], stats["running"], stats["error"],
            )
            last_report = now

    elapsed = time.monotonic() - start
    stats = queue.stats()

    # Finalize each touched project's status (complete/error) now that its work is done.
    tasks_imported = comments_imported = 0
    finished = errored = 0
    for project_dir, project_gid, project_name in considered:
        view = project_view(queue, project_dir, project_gid)
        meta = view.get("meta") or {}
        tasks_imported += meta.get("tasks_imported", 0)
        comments_imported += meta.get("comments_imported", 0)
        if view["status"] == "complete":
            finished += 1
        elif view.get("error_jobs"):
            errored += 1
            log.warning("Project '%s' (%s) finished with %d failed job(s) - see above for errors.",
                        project_name, project_gid, view["error_jobs"])

    log.info("=== Done in %.1fs ===", elapsed)
    log.info(
        "%d/%d project(s) fully imported, %d task/subtask(s) fetched, %d task(s)' comments fetched, "
        "%d job(s) failed permanently.",
        finished, total_projects, tasks_imported, comments_imported, stats["error"],
    )
    if stats["error"]:
        log.warning("Some jobs failed permanently after retries. Re-run `import-all` to retry just those.")
    log.info("Data written under ./data - browse it with `asana-migration serve`.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asana-migration")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Start the local web UI and background importer.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=5050)
    serve.add_argument("--no-browser", action="store_true", help="Don't auto-open a browser tab.")
    serve.set_defaults(func=_cmd_serve)

    import_all = sub.add_parser(
        "import-all",
        help="Run the whole export (every team/project/task/subtask/comment) in one foreground run.",
    )
    import_all.add_argument("--token", help="Asana personal access token (else uses the saved one, or prompts).")
    import_all.add_argument("--rate-limit", type=int, metavar="RPM",
                             help="Requests per minute (overrides the saved setting).")
    import_all.add_argument("--force", action="store_true",
                             help="Re-import projects that were already marked complete.")
    import_all.add_argument("-v", "--verbose", action="store_true",
                             help="Log every HTTP request/page fetch, not just the narrative summary.")
    import_all.set_defaults(func=_cmd_import_all)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "command", None):
        args = parser.parse_args(["serve"])
    args.func(args)


if __name__ == "__main__":
    main()
