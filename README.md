# Asana Migration

Exports an Asana account — teams, projects, sections, tasks, subtasks (nested,
any depth), comments, collaborators, and attachments — to a local, browsable
JSON folder tree, without ever exceeding Asana's API rate limit. Attachment
metadata (links, size, host) is captured during the main import; the actual
file bytes are a separate, optional pass — see `download-attachments` below.

It's built to run **slowly and resumably**: everything heavier than "list a
team's projects" goes through a persistent job queue that a single background
worker drains one request at a time, paced by a token-bucket rate limiter. If
you stop the server and start it again later, the queue and all previously
imported data are exactly where you left them.

A local web UI walks you through it, or you can run the entire thing
unattended from the terminal with one command (`import-all`, below).

## Getting started

### 1. Prerequisites

- [`uv`](https://docs.astral.sh/uv/) installed. Python itself is pinned by
  `.python-version` (3.13) and `uv` will fetch it for you — nothing else to
  install.

### 2. Get an Asana personal access token

1. In Asana, click your profile photo (top right) → **My Settings**.
2. Go to the **Apps** tab → **Manage Developer Apps**.
3. Under **Personal Access Tokens**, click **Create New Token**, give it a
   name (e.g. "local export"), and copy it — Asana only shows it once.

This token acts as *you*: the export will only see teams/projects you
personally have access to.

### 3. Install and launch

```bash
uv sync
uv run serve
```

This opens `http://127.0.0.1:5050` in your browser (use `--port`, `--host`,
`--no-browser` to customize).

### 4. Walk through the UI

1. **Paste the token** from step 2 and click **Connect**. It's checked
   against Asana immediately, then saved to `var/config.json` on your machine.
2. Click **Import teams** — a couple of cheap calls list your workspaces and
   teams and cache them locally. This is instant.
3. **Click a team.** Its projects load from local cache instantly if you've
   fetched them before; otherwise one lightweight call lists them, each
   tagged "not imported" / "importing…" / "imported".
4. Click **Import** on a project (or **Import all projects** for the whole
   team). This queues the deep crawl — sections → tasks → subtasks →
   comments → collaborators — and runs it in the background, paced by the
   rate limiter. The card updates live ("12/40 tasks imported") until it
   flips to "imported ✓"; you can navigate away and come back, or close the
   tab and reopen it later — nothing is lost.
5. Click into an **imported project** to see its section/task tree; click
   any task to see its notes, assignee, collaborators, and comments in a
   side panel.
6. Open **⚙ Settings** (top right) to change the requests/minute rate limit,
   or to swap in a different token.

Once something is marked "imported", every screen showing it loads from the
local `data/` folder — no more Asana calls until you explicitly re-import.

### 5. Find your data

Everything lands under `./data` as plain JSON files you can grep, script
against, or hand to another tool — see **Where things live** below for the
exact layout.

### Troubleshooting

- **"Asana rejected the personal access token (401)"** — the token is wrong,
  revoked, or expired; generate a new one (step 2) and reconnect via
  Settings → *Change token*.
- **A project is stuck on "importing…"** — it isn't stuck, it's throttled;
  check the queue badge in the header (top right) for how many requests are
  still pending. A large project can legitimately take a while at the
  default 100 req/min.
- **Jobs show as "failed"** — each job retries automatically (with backoff)
  up to 5 times before giving up; re-opening the project or re-running
  `import-all` retries only what's left.
- **Start over completely** — stop the server and `rm -rf var data`, then
  relaunch and reconnect.

## Or: run the whole export in one go, from the terminal

```bash
uv run import-all
```

Prompts for a token if none is saved yet, and (in an interactive terminal)
prompts for the rate limit on every run too — pre-filled with the current
saved value, just press Enter to keep it — rather than silently reusing
whatever's saved with no visibility into what that is. Then it discovers
every workspace, team and project and imports all of them — sections,
tasks, subtasks (any depth), comments, collaborators — in one foreground
run, paced by whatever rate limit you just confirmed. It logs what it's
doing as it goes:

```
11:55:08 INFO  asana_migration.importer: Team team1: found 6 project(s)
11:55:08 INFO  asana_migration.importer: Project 'Website Revamp' (proj1): metadata saved, 4 member(s)
11:55:08 INFO  asana_migration.importer: Section 'Backlog' (sec1): fetching tasks...
11:55:08 INFO  asana_migration.importer: Section 'Backlog': found 238 task(s)
11:55:09 INFO  asana_migration.importer: Task 't123' ('Design homepage'): fetching detail...
11:55:09 INFO  asana_migration.import_all: Progress: 340 job(s) completed so far | queue: 812 queued, 1 running, 0 failed
```

Useful flags: `--token …` (skip the prompt), `--rate-limit N` (override the
saved requests/minute), `--force` (re-crawl projects already marked
complete instead of skipping them), `-v` (also log every individual HTTP
request/page fetch, not just the narrative summary).

It's safe to `Ctrl+C` at any point — the job queue is persisted after every
single job, so re-running `import-all` (or opening the web UI) picks up
exactly where it stopped. It shares the same `var/`/`data/` directories as
`serve`, so work started one way can be finished the other.

## Downloading the actual attachment files

`import-all` (and the web UI) only ever save attachment *metadata* — name,
size, host, and Asana's links (`attachments.json` per task). They never fetch
the file bytes, on purpose: Asana's `download_url`/`view_url` are short-lived
signed URLs (good for roughly 30 minutes from when they're listed), so saving
the actual file has to happen right when a fresh URL is minted, not
whenever the metadata happened to be crawled — often hours or days earlier.

```bash
uv run download-attachments
```

This is a separate, independent pass: it doesn't touch Asana's project/task
tree at all, it just walks every `attachments.json` already on disk under
`./data`. For each attachment:

- **Asana-hosted files** (`host: "asana"`, ~93% of attachments in a typical
  export) — re-fetches that one attachment fresh (`GET /attachments/{gid}`)
  to mint a brand-new `download_url`, downloads it immediately, and saves it
  to `<task>/attachments/<attachment_gid>_<original filename>`. The
  attachment's entry in `attachments.json` gains `local_path` and
  `downloaded_at` once it's on disk.
- **`gdrive`/`external` attachments** (a Google Drive file, a Figma link, …)
  — skipped. Asana never hosted bytes for these (`download_url` is `null`
  in the metadata); the saved `view_url` link is already the whole story.

It's safe to `Ctrl+C` and re-run at any time — an attachment already saved to
disk (checked by `local_path` + matching file size) is skipped, not
re-downloaded, so a re-run only ever fetches what's still missing.

**Images inline in a task's description or a comment are already covered.**
Asana returns those as ordinary entries in the same `attachments.json` — an
inline `<img>` isn't a separate kind of thing to track, it's just the same
attachment object also referenced by its HTML. So this one command captures
every file a task has, standalone or inline, with nothing extra to run.

Useful flags: `--concurrency N` (default 12) controls how many files
download at once — this is a bandwidth/disk knob, **not** the Asana API rate
limit (each attachment still costs exactly one real API call, the metadata
re-fetch, which *is* paced by `--rate-limit`/the saved setting as usual; the
file transfer itself isn't an api.asana.com call and doesn't count against
Asana's limit at all — see **Rate limiting** below). `--token` and `-v` work
the same as `import-all`.

## Checking how much disk space attachments need

```bash
uv run estimate-storage
```

Pure local-disk arithmetic - reads what `import-all` already saved under
`./data`, makes zero Asana calls, and needs no token. Useful before running
`download-attachments` (or mid-run, to see what's left) if you want to know
the download is going to fit:

```
Attachments by host:
  asana        2644
  external      151  (link only - never downloaded)
  gdrive         53  (link only - never downloaded)

Downloadable (host=asana):    2644 file(s), 11.8 GB total
Already on disk:              1666 file(s), 7.1 GB
Still to download:             978 file(s), 4.8 GB

Size distribution (all Asana-hosted attachments, downloaded or not):
  smallest: 1.7 KB
  median:   118.9 KB
  p90:      16.7 MB
  p99:      55.3 MB
  largest:  100.0 MB

Free space on ./data's disk: 85.3 GB
  -> enough room - 80.6 GB left over after downloading the rest.
```

Only `host: "asana"` attachments count toward any of these totals -
`gdrive`/`external` ones never had bytes to download in the first place (see
**Downloading the actual attachment files** above), so they're listed but
excluded from the size math. "Already on disk" is exactly the same
`local_path` + file-size check `download-attachments` itself uses to decide
what to skip, so the "still to download" number is a true preview of what a
`download-attachments` run would actually do next.

Also available as a standalone script if you'd rather point it somewhere
other than `./data` without an env var: `uv run python
scripts/estimate_attachment_storage.py --data-dir /path/to/data` - same
numbers, same code underneath.

## Or: run it in Docker

```bash
docker compose up -d --build
```

`serve` starts automatically — open `http://localhost:5050`. It comes back
on its own after a rebuild/recreate (`restart: unless-stopped` + baked into
the image's `CMD`).

For `import-all`, `download-attachments`, `estimate-storage`, or anything
else, open a shell in the same running container - it runs safely alongside
the auto-started `serve`, sharing the same mounted `data/`/`var/` (the job
queue is safe for concurrent access):

```bash
docker compose exec asana-migration bash
import-all
download-attachments
estimate-storage
```

`serve` auto-detects it's running in a container (checks for `/.dockerenv`)
and binds `0.0.0.0` instead of its normal-elsewhere default of `127.0.0.1`
— so plain `serve`, with no `--host` flag, is correct both inside Docker
and out. (`127.0.0.1` inside a container is only reachable from inside that
container's own network namespace - Docker's port mapping can't forward to
it no matter how the mapping itself is set up.)

`./data` and `./var` are bind mounts, not Docker volumes - the container
writes directly into those folders in your project directory on disk, so
they're browsable with a normal file manager and untouched by `docker
compose down` or an image rebuild. `./src` is bind-mounted too, so local
code edits take effect in the running container immediately (it's an
editable install) — only changes to `pyproject.toml`/`uv.lock` (new deps,
new console scripts) need a rebuild.

## Pagination (>100 records per Asana call)

Asana caps every list endpoint at 100 records per page. `AsanaClient.paginate()`
follows the `next_page.offset` cursor Asana returns until it runs out, so
every collection — teams, projects, sections, tasks, subtasks, comments — is
fetched in full however many pages it takes; each page fetch is its own
rate-limited request, and a 429 mid-pagination pauses and resumes from where
it left off, not from page 1.

## Where things live

- `var/config.json` — your token and settings (rate limit, max subtask
  depth). Never commit this.
- `var/jobs.json` — the resumable job queue.
- `data/` — the exported tree:

  ```
  data/<workspace>/
    workspace.json
    projects/<project>/            # every project, fetched once
      project.json  members.json  sections.json  _meta.json  _index.json
    tasks/<task>/                  # every task/subtask, fetched once
      task.json  comments.json  collaborators.json
      attachments.json             # metadata + links for every attachment
                                    #   (standalone or inline in the
                                    #   description/comments) - gains
                                    #   local_path/downloaded_at once
                                    #   `download-attachments` has run
      attachments/                 # the actual files, once downloaded -
                                    #   <attachment_gid>_<original filename>
    teams/<team>/
      team.json
      projects_index.json          # which project gids belong to this team
  ```

  A project can belong to more than one Asana team, and a task can be a
  subtask of one task while also directly belonging to another project - so
  projects and tasks are pooled once per workspace, addressed by gid, rather
  than copied into every team/project/parent that references them. A team's
  `projects_index.json` and a project's `_index.json` are just pointer
  lists into those pools; the same project or task gid can legitimately
  appear in more than one of them without being fetched from Asana or
  written to disk more than once.

  `_meta.json` is the per-project import status/progress the UI reads.
  `_index.json` is a flat gid → summary map used to render the task tree
  without reading every `task.json`.

Both `var/` and `data/` are gitignored.

## Rate limiting

Every Asana call goes through one shared token-bucket limiter (default 100
requests/minute — adjustable in the UI's Settings panel). A `429` response
pauses *all* in-flight work for the `Retry-After` duration Asana asks for,
not just the request that got throttled.

A single worker is bound by request round-trip latency (network + disk +
queue-lock overhead), not by the limiter itself - in practice, one worker
tops out around 50-90 completed jobs/minute regardless of how high the
configured limit is, since it only ever has one request in flight. Raising
the rate limit doesn't raise that ceiling on its own; running more requests
concurrently does. So the import is driven by a **pool of workers**, sized
to the configured rate limit at roughly one worker per 75 requests/minute
(e.g. 150 → 2 workers, 2000 → 27) - it resizes live when you change the
rate limit in Settings, no restart needed, and `import-all` sizes its pool
once at startup from whatever rate limit you confirm.

`download-attachments` is the one exception to all of the above: its jobs
are one small API call plus a multi-second/minute file transfer, not one
fast JSON call, so sizing its worker pool off the rate limit the same way
would badly under-use it (e.g. 1500 rpm → ~20 workers → maybe only ~100
files/minute if each transfer takes ~12s). Its worker count is instead its
own `--concurrency` flag (default 12, see above), independent of
`--rate-limit` - concurrent file transfers aren't Asana API calls and don't
draw from the rate limiter at all, only the one metadata re-fetch per
attachment does.
