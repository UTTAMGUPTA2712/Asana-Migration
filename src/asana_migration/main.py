"""CLI entry point.

    uv run serve                     # start the web UI + background importer
    uv run serve --port 5050 --no-browser

    uv run import-all                # run the ENTIRE export in one foreground
                                      # run: every team, every project, every
                                      # task/subtask/comment - rate-limited,
                                      # resumable, logging every step.
    uv run import-all --force --rate-limit 60 -v

    uv run job-status                # done/queued/running/error counts,
                                      # save rate, ETA and a per-type
                                      # breakdown for the queue an
                                      # `import-all`/`serve` is draining -
                                      # safe to run from another shell while
                                      # one of those is running.
    uv run job-status --window 2

(`uv run asana-migration serve` / `asana-migration import-all` work too, if
installed outside a `uv run` context - all three console scripts point at
the same subcommands below.)

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
import json
import logging
import os
import sys
import time
import webbrowser
from dataclasses import asdict
from pathlib import Path

log = logging.getLogger("asana_migration.import_all")

# How many attachments download-attachments fetches at once by default.
# Unrelated to the Asana API rate limit (see _cmd_download_attachments) -
# this just bounds concurrent outbound connections/local disk writes, so
# it's conservative-but-raisable rather than derived from anything.
DEFAULT_DOWNLOAD_CONCURRENCY = 12

# Defaults for `retry-attachments` - gentler/more forgiving than
# download-attachments' own retry policy on purpose, since these attachments
# already exhausted that one. See `_cmd_retry_failed_attachments`.
DEFAULT_RETRY_CONCURRENCY = 6
DEFAULT_RETRY_ATTEMPTS = 4
DEFAULT_RETRY_TIMEOUT = 120.0


def _default_serve_host() -> str:
    """127.0.0.1 everywhere except inside a container, where that's
    unreachable from the host no matter how the port is published (it's
    Docker's port mapping that can't forward to a loopback-only bind, not
    anything specific to this app) - so default to 0.0.0.0 there instead.
    `/.dockerenv` is the standard marker file every Docker container gets."""
    return "0.0.0.0" if os.path.exists("/.dockerenv") else "127.0.0.1"


def _cmd_serve(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s",
    )
    from .webapp import create_app

    app = create_app()
    url = f"http://{args.host}:{args.port}"
    print(f"Asana migration UI running at {url}")
    if args.host == "0.0.0.0" and os.path.exists("/.dockerenv"):
        print("(auto-selected 0.0.0.0 - running in a container; pass --host to override)")
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
        format="%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
    )
    # Quiet down third-party request/connection noise unless -v was asked for.
    if not args.verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    from . import config as config_mod
    from .client import AsanaAuthError, AsanaClient
    from .importer import (
        ImporterContext,
        ensure_other_projects_index,
        ensure_team_members,
        ensure_team_projects_index,
        import_workspaces_and_teams,
        project_view,
    )
    from .jobs import JobQueue
    from .rate_limiter import RateLimiter
    from .worker import RPM_PER_WORKER, WorkerPool, desired_worker_count
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
    elif sys.stdin.isatty():
        # Ask every interactive run (pre-filled with the current/default
        # value) rather than silently reusing whatever's saved - that
        # silence is exactly what let a previously-set rate limit go
        # unnoticed after it got reset.
        default_rpm = cfg.rate_limit_per_minute or config_mod.DEFAULT_RATE_LIMIT_PER_MINUTE
        raw = input(f"Rate limit in requests/minute [{default_rpm}]: ").strip()
        if raw:
            try:
                cfg.rate_limit_per_minute = int(raw)
            except ValueError:
                log.warning("'%s' isn't a whole number - keeping %d req/min.", raw, default_rpm)
                cfg.rate_limit_per_minute = default_rpm
        else:
            cfg.rate_limit_per_minute = default_rpm
    # else: non-interactive (piped/backgrounded) - keep the saved/default
    # value rather than hang on input().
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
            ensure_team_members(ctx, team_dir, team["gid"], force=args.force)
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

    num_workers = desired_worker_count(cfg.rate_limit_per_minute)
    log.info(
        "=== Step 2/2: draining the import queue with %d worker(s) (~%d req/min each) - "
        "this is the slow, rate-limited part ===",
        num_workers, RPM_PER_WORKER,
    )
    start = time.monotonic()
    done_at_start = queue.stats()["done"]
    pool = WorkerPool(ctx)
    pool.start(cfg.rate_limit_per_minute)
    last_report = start
    try:
        while True:
            stats = queue.stats()
            if stats["queued"] == 0 and stats["running"] == 0:
                break
            now = time.monotonic()
            if now - last_report >= 15:
                log.info(
                    "Progress: %d job(s) completed so far | queue: %d queued, %d running, %d failed permanently",
                    stats["done"] - done_at_start, stats["queued"], stats["running"], stats["error"],
                )
                last_report = now
            time.sleep(1)
    finally:
        pool.stop()

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
    log.info("Data written under ./data - browse it with `uv run serve`.")


def _cmd_download_attachments(args: argparse.Namespace) -> None:
    """Second pass, run independently of `import-all`: downloads the actual
    bytes for every Asana-hosted attachment already listed in some task's
    attachments.json but not yet saved to disk. Doesn't touch Asana's
    project/task tree at all - it only walks ./data, so it's safe to run
    (and re-run) any time after an import, and only ever redoes attachments
    it doesn't already have on disk."""
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
    )
    if not args.verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    from . import config as config_mod
    from .client import AsanaAuthError, AsanaClient
    from .importer import ImporterContext, queue_pending_attachment_downloads
    from .jobs import JobQueue
    from .rate_limiter import RateLimiter
    from .worker import WorkerPool
    from .storage import Paths

    cfg = config_mod.load_config()
    token = args.token or cfg.token
    if not token:
        token = getpass.getpass("Asana personal access token: ").strip()
    if not token:
        log.error("No token provided. Pass --token, or run once with the token to save it.")
        raise SystemExit(1)

    rate_limit = args.rate_limit or cfg.rate_limit_per_minute
    rate_limiter = RateLimiter(rate_limit)
    client = AsanaClient(token, rate_limiter)
    try:
        client.get_me()
    except AsanaAuthError as exc:
        log.error("%s", exc)
        raise SystemExit(1)

    ctx = ImporterContext(client=client, paths=Paths(), queue=JobQueue())
    is_download_job = lambda j: j.type == "download_task_attachment"  # noqa: E731

    log.info("Scanning ./data for attachments not downloaded yet...")
    queued, already_done = queue_pending_attachment_downloads(ctx)
    # `queued` only counts jobs *this scan* newly added - JobQueue.push()'s
    # dedupe (jobs.py) silently skips re-adding one already queued/running/
    # done from an earlier `download-attachments` run, so on a second run
    # `queued` reads 0 even with plenty of earlier-queued work still
    # outstanding. The real "is there anything left" answer is whatever's
    # actually sitting in the shared queue for this job type right now,
    # regardless of which run put it there - that's what decides whether to
    # start a pool, not this scan's own delta.
    outstanding_stats = ctx.queue.stats_for(is_download_job)
    outstanding = outstanding_stats["queued"] + outstanding_stats["running"]
    log.info(
        "%d attachment(s) newly queued this scan, %d already on disk, %d total still outstanding "
        "(including any queued by an earlier run and not yet drained).",
        queued, already_done, outstanding,
    )
    if not outstanding:
        log.info("Nothing to do.")
        return

    # Deliberately NOT desired_worker_count(rate_limit): that formula sizes
    # workers assuming one fast JSON call per job (~75/min/worker), but a
    # download job is one tiny metadata call plus however long a multi-MB
    # file transfer takes - sizing off the API rate limit here starves
    # actual throughput (e.g. 1500 rpm -> 20 workers -> maybe ~100
    # downloads/min if each takes ~12s, nowhere near what 1500 rpm implies).
    # Concurrency is its own knob because it's bandwidth-bound, not
    # request-rate-bound; the metadata call each job makes is still paced
    # by the real rate limiter no matter how many workers there are, so
    # this can't overrun Asana's actual API quota.
    log.info("Downloading with %d worker(s) (bandwidth-bound, independent of the %d req/min API rate limit)...",
              args.concurrency, rate_limit)
    start = time.monotonic()
    done_at_start = outstanding_stats["done"]
    total_for_type = outstanding + done_at_start + outstanding_stats["error"]
    pool = WorkerPool(ctx)
    pool.start(worker_count=args.concurrency)
    last_report = start
    try:
        while True:
            # Scoped to this job type, not ctx.queue.stats()'s global count -
            # `serve` (or another download-attachments run) can share this
            # same queue with other job types alive at the same time, and a
            # global "queued==0 and running==0" would never fire while any
            # of those are still going, or would fire too early/misreport
            # progress if they aren't.
            stats = ctx.queue.stats_for(is_download_job)
            if stats["queued"] == 0 and stats["running"] == 0:
                break
            now = time.monotonic()
            if now - last_report >= 15:
                log.info(
                    "Progress: %d/%d downloaded so far | %d failed permanently",
                    stats["done"] - done_at_start, total_for_type, stats["error"],
                )
                last_report = now
            time.sleep(1)
    finally:
        pool.stop()

    stats = ctx.queue.stats_for(is_download_job)
    elapsed = time.monotonic() - start
    log.info("=== Done in %.1fs: %d downloaded, %d failed permanently ===",
              elapsed, stats["done"] - done_at_start, stats["error"])
    if stats["error"]:
        log.warning("Some downloads failed permanently after retries. Re-run `download-attachments` to retry just those.")
    log.info("Files written under ./data/<workspace>/tasks/<task>/attachments/")


def _cmd_retry_failed_attachments(args: argparse.Namespace) -> None:
    """Retries only the attachment downloads `download-attachments` already
    gave up on (queue status `error`). That command's own retry policy is
    blunt - 5 attempts, a fixed 30s timeout shared with ordinary JSON API
    calls, no real backoff of its own beyond the queue's - so a big or
    slow-to-fetch file fails the same way every time and exhausts it. This
    gives each one a fresh, more forgiving shot instead: a bigger timeout,
    its own backoff loop, bounded concurrency - see
    `importer.retry_failed_attachment_downloads`. Anything that still fails
    gets written to a dated JSON report under var/ with a classified reason
    per attachment, instead of leaving you to grep logs."""
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
    )
    if not args.verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    from . import config as config_mod
    from .client import AsanaAuthError, AsanaClient
    from .importer import ImporterContext, find_failed_attachment_downloads, retry_failed_attachment_downloads
    from .jobs import JobQueue
    from .rate_limiter import RateLimiter
    from .storage import Paths

    # Checked before asking for a token at all: this only reads jobs.json,
    # and there's no point prompting for credentials for a no-op.
    queue = JobQueue()
    failed_jobs = find_failed_attachment_downloads(queue)
    if not failed_jobs:
        log.info("No permanently-failed attachment downloads - nothing to retry.")
        return

    cfg = config_mod.load_config()
    token = args.token or cfg.token
    if not token:
        token = getpass.getpass("Asana personal access token: ").strip()
    if not token:
        log.error("No token provided. Pass --token, or run once with the token to save it.")
        raise SystemExit(1)

    rate_limiter = RateLimiter(args.rate_limit or cfg.rate_limit_per_minute)
    client = AsanaClient(token, rate_limiter, timeout=args.timeout)
    try:
        client.get_me()
    except AsanaAuthError as exc:
        log.error("%s", exc)
        raise SystemExit(1)

    ctx = ImporterContext(client=client, paths=Paths(), queue=queue)
    log.info("Retrying %d permanently-failed attachment download(s): up to %d attempt(s) each, "
              "%.0fs timeout, %d at once...", len(failed_jobs), args.retries, args.timeout, args.concurrency)

    start = time.monotonic()
    succeeded, failures = retry_failed_attachment_downloads(
        ctx, failed_jobs, retries=args.retries, timeout=args.timeout, concurrency=args.concurrency,
    )
    elapsed = time.monotonic() - start
    log.info("=== Done in %.1fs: %d/%d recovered, %d still failing ===",
              elapsed, succeeded, len(failed_jobs), len(failures))

    if failures:
        out_path = config_mod.VAR_DIR / f"attachment_retry_failures_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
        out_path.write_text(json.dumps([asdict(f) for f in failures], indent=2))
        log.warning("%d attachment(s) still failing after retry - see %s for gid/task/reason per file.",
                    len(failures), out_path)
        for f in failures:
            log.warning("  task %s (%s): attachment %s (%s) - %s",
                        f.task_gid, f.task_name or "?", f.attachment_gid, f.attachment_name or "?", f.reason)
    else:
        log.info("Everything that had failed before is now downloaded.")


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _percentile(sorted_vals: list[int], p: float) -> int:
    if not sorted_vals:
        return 0
    k = max(0, min(len(sorted_vals) - 1, round(p * (len(sorted_vals) - 1))))
    return sorted_vals[k]


def _cmd_estimate_storage(args: argparse.Namespace) -> None:
    """Pure local-disk arithmetic, no Asana API calls and no token needed -
    reads what `import-all` already saved under ./data and reports how much
    space `download-attachments` needs, in total and still remaining."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from .importer import estimate_attachment_storage
    from .storage import Paths

    paths = Paths()
    if not paths.root.exists():
        log.error("No data directory at %s - nothing imported yet.", paths.root)
        raise SystemExit(1)

    stats = estimate_attachment_storage(paths)

    log.info("Scanned %d task(s) under %s\n", stats["tasks_scanned"], paths.root)

    log.info("Attachments by host:")
    for host, count in sorted(stats["by_host"].items(), key=lambda kv: -kv[1]):
        note = "" if host == "asana" else "  (link only - never downloaded)"
        log.info("  %-10s %6d%s", host, count, note)
    log.info("")

    if stats["missing_size_count"]:
        log.info(
            "Note: %d Asana-hosted attachment(s) have no reported size yet "
            "(not counted below - re-fetch their metadata, e.g. via `import-all`, to know for sure).\n",
            stats["missing_size_count"],
        )

    log.info("Downloadable (host=asana):  %6d file(s), %s total",
              stats["asana_count"], _human_bytes(stats["downloadable_total_bytes"]))
    log.info("Already on disk:            %6d file(s), %s",
              stats["downloaded_count"], _human_bytes(stats["downloaded_total_bytes"]))
    log.info("Still to download:          %6d file(s), %s\n",
              stats["remaining_count"], _human_bytes(stats["remaining_bytes"]))

    sizes = stats["asana_sizes_sorted"]
    if sizes:
        log.info("Size distribution (all Asana-hosted attachments, downloaded or not):")
        log.info("  smallest: %s", _human_bytes(sizes[0]))
        log.info("  median:   %s", _human_bytes(_percentile(sizes, 0.50)))
        log.info("  p90:      %s", _human_bytes(_percentile(sizes, 0.90)))
        log.info("  p99:      %s", _human_bytes(_percentile(sizes, 0.99)))
        log.info("  largest:  %s\n", _human_bytes(sizes[-1]))

    try:
        import shutil
        usage = shutil.disk_usage(paths.root)
        log.info("Free space on %s's disk: %s", paths.root, _human_bytes(usage.free))
        remaining = stats["remaining_bytes"]
        if remaining:
            if usage.free >= remaining:
                log.info("  -> enough room - %s left over after downloading the rest.",
                          _human_bytes(usage.free - remaining))
            else:
                log.warning("  -> NOT enough room - short by %s.", _human_bytes(remaining - usage.free))
        else:
            log.info("  -> nothing left to download.")
    except OSError as exc:
        log.warning("Couldn't check free disk space: %s", exc)


def _progress_bar(done: int, total: int, width: int = 30) -> str:
    if total <= 0:
        return "[" + " " * width + "]   0%"
    frac = min(1.0, done / total)
    filled = round(width * frac)
    return f"[{'#' * filled}{'-' * (width - filled)}] {frac * 100:5.1f}%"


def _cmd_job_status(args: argparse.Namespace) -> None:
    """Point-in-time report on a work queue: how much is done/queued/running/
    failed overall and per job type, how fast `done` jobs are landing, and a
    rough ETA for the rest - the same numbers `import-all`'s own 15s progress
    log prints, but on demand and without needing a foreground
    `import-all`/`serve` of your own running.

    Defaults to asana_migration's own queue (./var/jobs.json). Pass
    `--padmasana` to report on padmasana_migration's queue
    (./var/padmasana_jobs.json) instead - the two never share a queue file
    (see padmasana_migration/config.py), so this is a straight either/or."""
    import collections

    if args.padmasana:
        from padmasana_migration.config import JOBS_PATH
    else:
        from .config import JOBS_PATH
    from .jobs import JobQueue

    queue = JobQueue(path=JOBS_PATH)
    jobs = queue.all_jobs()
    if not jobs:
        hint = ("run one of padmasana_migration's scripts (e.g. `padmasana-build-tasks`) first"
                 if args.padmasana else "run `import-all` or `serve` first")
        print(f"No jobs in {JOBS_PATH} yet - nothing has been queued ({hint}).")
        return

    now = time.time()
    total = len(jobs)
    by_status = collections.Counter(j.status for j in jobs)
    done_jobs = [j for j in jobs if j.status == "done"]
    done_n = by_status["done"]
    queued_n = by_status["queued"]
    running_n = by_status["running"]
    error_n = by_status["error"]
    remaining = queued_n + running_n

    started = [j.started_at for j in jobs if j.started_at]
    elapsed_min = (now - min(started)) / 60 if started else 0.0
    overall_rate = done_n / elapsed_min if elapsed_min > 0 else 0.0

    window_min = max(0.1, args.window)
    window_sec = window_min * 60
    recent_done_n = sum(1 for j in done_jobs if j.started_at and now - j.started_at <= window_sec)
    recent_rate = recent_done_n / window_min

    eta_rate = recent_rate or overall_rate
    eta_min = remaining / eta_rate if eta_rate > 0 else None

    print(f"Job queue: {JOBS_PATH}  (checked {time.strftime('%Y-%m-%d %H:%M:%S %Z')})")
    print(f"Running for: {elapsed_min:.1f} min\n")

    print(_progress_bar(done_n, total), f" {done_n}/{total} done, {remaining} left")
    print(f"  done: {done_n:>6}   queued: {queued_n:>6}   running: {running_n:>6}   error: {error_n:>6}\n")

    print("Save rate:")
    print(f"  overall:        {overall_rate:7.1f} jobs/min  ({overall_rate / 60:.2f}/sec)  since it started")
    print(f"  last {window_min:g} min:    {recent_rate:7.1f} jobs/min  ({recent_done_n} job(s) in the window)")
    if eta_min is not None:
        eta_when = time.strftime("%H:%M:%S", time.localtime(now + eta_min * 60))
        print(f"\nETA: ~{eta_min:.1f} min left at the {'recent' if recent_rate else 'overall'} rate "
              f"(finish around {eta_when})")
    else:
        print("\nETA: n/a (no completed jobs yet)")

    print("\nBy type:")
    types = sorted({j.type for j in jobs})
    header = f"  {'type':<26}{'done':>8}{'queued':>8}{'running':>9}{'error':>8}{'total':>8}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for t in types:
        t_jobs = [j for j in jobs if j.type == t]
        c = collections.Counter(j.status for j in t_jobs)
        print(f"  {t:<26}{c['done']:>8}{c['queued']:>8}{c['running']:>9}{c['error']:>8}{len(t_jobs):>8}")

    if error_n:
        print(f"\n{error_n} job(s) failed permanently. See `retry-attachments` for attachment downloads, "
              "or re-run `import-all`/`import` for the rest.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asana-migration")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Start the local web UI and background importer.")
    serve.add_argument("--host", default=_default_serve_host())
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

    download_attachments = sub.add_parser(
        "download-attachments",
        help="Download the actual file bytes for every already-imported, Asana-hosted attachment not yet on disk.",
    )
    download_attachments.add_argument("--token", help="Asana personal access token (else uses the saved one, or prompts).")
    download_attachments.add_argument("--rate-limit", type=int, metavar="RPM",
                                       help="Requests/minute for the one real Asana API call per attachment "
                                            "(metadata fetch) - overrides the saved setting. Does NOT bound "
                                            "the file transfers themselves; see --concurrency for that.")
    download_attachments.add_argument("--concurrency", type=int, default=DEFAULT_DOWNLOAD_CONCURRENCY, metavar="N",
                                       help=f"How many attachments to download at once (default {DEFAULT_DOWNLOAD_CONCURRENCY}). "
                                            "Bandwidth-bound, not request-rate-bound - unrelated to --rate-limit, "
                                            "raise it freely to use more of your network/disk throughput.")
    download_attachments.add_argument("-v", "--verbose", action="store_true",
                                       help="Log every HTTP request/download, not just the narrative summary.")
    download_attachments.set_defaults(func=_cmd_download_attachments)

    retry_attachments = sub.add_parser(
        "retry-attachments",
        help="Retry only the attachment downloads download-attachments already gave up on permanently, "
             "with a bigger timeout and its own backoff.",
    )
    retry_attachments.add_argument("--token", help="Asana personal access token (else uses the saved one, or prompts).")
    retry_attachments.add_argument("--rate-limit", type=int, metavar="RPM",
                                    help="Requests/minute for the one real Asana API call per attachment "
                                         "(metadata fetch) - overrides the saved setting.")
    retry_attachments.add_argument("--retries", type=int, default=DEFAULT_RETRY_ATTEMPTS, metavar="N",
                                    help=f"Attempts per attachment before giving up again (default {DEFAULT_RETRY_ATTEMPTS}).")
    retry_attachments.add_argument("--timeout", type=float, default=DEFAULT_RETRY_TIMEOUT, metavar="SECONDS",
                                    help=f"Per-request timeout for the metadata call and the file download "
                                         f"(default {DEFAULT_RETRY_TIMEOUT:.0f}s, vs. download-attachments' fixed 30s).")
    retry_attachments.add_argument("--concurrency", type=int, default=DEFAULT_RETRY_CONCURRENCY, metavar="N",
                                    help=f"How many failed attachments to retry at once (default {DEFAULT_RETRY_CONCURRENCY}).")
    retry_attachments.add_argument("-v", "--verbose", action="store_true",
                                    help="Log every attempt, not just the summary.")
    retry_attachments.set_defaults(func=_cmd_retry_failed_attachments)

    estimate_storage = sub.add_parser(
        "estimate-storage",
        help="Report how much disk space downloading every attachment needs (total, done, remaining) - "
             "reads ./data only, no Asana call, no token.",
    )
    estimate_storage.set_defaults(func=_cmd_estimate_storage)

    job_status = sub.add_parser(
        "job-status",
        help="Report on a job queue: done/queued/running/error counts, save rate, ETA, and a "
             "per-type breakdown - reads the queue file only, no Asana call, no token.",
    )
    job_status.add_argument("--window", type=float, default=5.0, metavar="MINUTES",
                             help="Window (in minutes) for the recent-rate/ETA calculation (default 5).")
    job_status.add_argument("--padmasana", action="store_true",
                             help="Report on padmasana_migration's queue (./var/padmasana_jobs.json) "
                                  "instead of asana_migration's own (./var/jobs.json, the default).")
    job_status.set_defaults(func=_cmd_job_status)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        args = parser.parse_args(["serve"])
    args.func(args)


def serve_main() -> None:
    """Console-script entry point for `serve` (and `uv run serve`) - same as
    `asana-migration serve`, just without the prefix."""
    main(["serve", *sys.argv[1:]])


def import_all_main() -> None:
    """Console-script entry point for `import-all` (and `uv run import-all`)."""
    main(["import-all", *sys.argv[1:]])


def download_attachments_main() -> None:
    """Console-script entry point for `download-attachments` (and `uv run download-attachments`)."""
    main(["download-attachments", *sys.argv[1:]])


def estimate_storage_main() -> None:
    """Console-script entry point for `estimate-storage` (and `uv run estimate-storage`)."""
    main(["estimate-storage", *sys.argv[1:]])


def retry_attachments_main() -> None:
    """Console-script entry point for `retry-attachments` (and `uv run retry-attachments`)."""
    main(["retry-attachments", *sys.argv[1:]])


def job_status_main() -> None:
    """Console-script entry point for `job-status` (and `uv run job-status`)."""
    main(["job-status", *sys.argv[1:]])


if __name__ == "__main__":
    main()
