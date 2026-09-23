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

## Retrying jobs that failed permanently

Any job type can end up permanently `error` after 5 attempts - a project/task/
comment fetch that kept 500ing, or an attachment download that kept timing
out. `download-attachments` in particular gives each attachment up to 5
attempts (with backoff) against a blunt, one-size-fits-all policy - a fixed
30s timeout shared with ordinary JSON API calls, no real backoff logic of its
own beyond the queue's - so a big or slow-to-fetch file can exhaust all 5
attempts the same way every time while everything else downloads fine.

```bash
uv run retry-all
```

This retries *every* permanently-failed job, not just attachments - but not
with one blanket policy, since attachment downloads genuinely need different
treatment (bandwidth-bound file transfers vs. fast JSON calls). It does two
things, back to back:

- Every other failed job type (project/task/section/comment fetches, ...) is
  requeued and drained through the same rate-limited worker pool `import-all`
  uses - equivalent to just re-running `import-all` for its retry-failed-jobs
  side effect, bundled in here for convenience.
- Attachment downloads get their own more forgiving pass: a bigger,
  configurable timeout (120s by default), its own backoff loop (separate
  from, and on top of, the original 5 attempts), and bounded concurrency.
  This also catches ones marked `done` but never actually saved to disk, not
  just ones marked `error`. A successful retry is folded back into the normal
  state exactly like `download-attachments` would (`attachments.json` gets
  its `local_path`/`downloaded_at`, the job is marked `done`).

If nothing has failed at all, it says so and exits without even asking for a
token. Anything still failing afterward gets written to a report so you're
not left grepping logs: `var/job_retry_failures_<timestamp>.json` (id, type,
payload, reason) for non-attachment jobs, and
`var/attachment_retry_failures_<timestamp>.json` (task, attachment, and a
classified reason - deleted/forbidden on Asana, timed out, network error,
local disk error, …) for attachments.

Useful flags: `--retries N` (default 4), `--timeout SECONDS` (default 120),
`--concurrency N` (default 6), `--rate-limit`, `--token`, `-v` — the first
three apply to the attachment pass specifically (non-attachment jobs use the
queue's own fixed 5-attempt policy); the rest apply to both passes.

Also available as a standalone script if you'd rather point it at a
`data/`/`var/` pair that isn't `./data`/`./var` without an env var: `uv run
python scripts/retry_failed_attachments.py --data-dir /path/to/data --var-dir
/path/to/var` - same logic underneath.

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

## Checking job queue status

```bash
uv run job-status
```

Reads `./var/jobs.json` only - no Asana call, no token needed. Safe to run
from another shell while `import-all`/`serve`/`download-attachments` is
running:

```
Job queue: /path/to/asana-migration/var/jobs.json  (checked 2026-09-16 13:08:14 IST)
Running for: 78.2 min

[##########################----]  85.1%  12971/15247 done, 2276 left
  done:  12971   queued:   2273   running:      3   error:      0

Save rate:
  overall:          165.9 jobs/min  (2.76/sec)  since it started
  last 5 min:      142.2 jobs/min  (711 job(s) in the window)

ETA: ~16.0 min left at the recent rate (finish around 13:24:15)

By type:
  type                          done  queued  running   error   total
  -------------------------------------------------------------------
  import_project                  50       0        0       0      50
  import_section_tasks           317       0        0       0     317
  import_sections                 50       0        0       0      50
  import_subtasks                796       4        0       0     800
  import_task                   5298     132        0       0    5430
  import_task_attachments       3229    1069        2       0    4300
  import_task_comments          3231    1068        1       0    4300
```

`--window MINUTES` (default 5) controls the "last N min" rate/ETA - shorten
it for a more current-moment read, lengthen it to smooth out bursts.

`--padmasana` reports on `padmasana_migration`'s own queue
(`./var/padmasana_jobs.json`) instead - the two tools never share a queue
file (see `src/padmasana_migration/config.py`), so this is a straight
either/or, not a merge of both.

## Or: run it in Docker

```bash
docker compose up -d --build
```

`serve` starts automatically — open `http://localhost:5050`. It comes back
on its own after a rebuild/recreate (`restart: unless-stopped` + baked into
the image's `CMD`).

For `import-all`, `download-attachments`, `retry-all`, `estimate-storage`,
`job-status`, or anything else, open a shell in the same running container -
it runs safely alongside the auto-started `serve`, sharing the same mounted
`data/`/`var/` (the job queue is safe for concurrent access):

```bash
docker compose exec asana-migration bash
import-all
download-attachments
retry-all
estimate-storage
job-status
```

If the console script isn't found (`No such file or directory` - happens
right after adding a new one to `pyproject.toml`, since only `./src` is
bind-mounted and console scripts are only regenerated by `uv sync` at image
build time), run it as a module instead until you rebuild:
`python -m asana_migration.main job-status`.

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

---

# Part 2: Padmasana Migration (Transform & Seed Prep)

Once your Asana data is exported to `./data`, the `padmasana_migration` toolset transforms it into finished, normalized, and validated JSON in `./build`. This output matches the exact database entities and schemas required by `padmasana-service` and the rich-text Tiptap formatting expected by `padmasana-app`.

### Key Principles
- **`data/` is strictly read-only**: The Asana export is never modified.
- **Resumable and Idempotent**: You can safely interrupt (`Ctrl+C`) and re-run any command at any time.
- **Config Persistence**: CLI parameters like URLs, workspaces, and organization units are automatically saved in `var/config.json` under `extra.padmasana` so you don't need to re-type them on every run.
- **Missing User Detection**: All commands actively verify whether Asana users match Padmasana identities and report missing users.

---

## The Migration Workflow

Follow these steps in order to prepare your data for seeding into Padmasana:

```
[data/ (Asana export)]
        │
        ├── Step 1 ──> padmasana-build-teams-and-boards  ──> build/teams.json, boards.json, sections.json
        │
        ├── Step 2 ──> padmasana-upload-attachments      ──> uploads to file-service, records file UUIDs
        │
        ├── Step 3 ──> padmasana-build-tasks            ──> build/tasks.json, comments.json, activity logs
        │
        ├── Step 4 ──> padmasana-map-users / import-users──> build/user_to_asana_gid.json, padmasana_users.json
        │
        ├── Step 5 ──> padmasana-inspect-task           ──> preview rich tasks (mentions, attachments, links)
        │
        └── Step 6 ──> padmasana-format-docs            ──> converts HTML to Tiptap mentions & links
```

---

### Step 1: Build Teams, Boards, and Sections

Builds Padmasana teams, boards, sections, tags, and their membership/pivot records from your exported Asana workspaces and projects.

```bash
uv run padmasana-build-teams-and-boards
```

**What it does:**
- Generates `build/teams.json`, `build/boards.json`, `build/sections.json`, and `build/tags.json`.
- Generates relational pivots: `build/board_team.json`, `build/board_member.json`, and `build/team_member.json`.
- Assigns stable UUIDs so re-running retains existing IDs.
- By default, assigns team ownership and board creator to the lowest Asana GID member. You can override specific team owners using `--team-owner`:

```bash
uv run padmasana-build-teams-and-boards --team-owner 1205361916477120=1205361916477125
```

---

### Step 2: Upload Attachments to the File Service

Uploads all downloaded Asana files (from `data/<workspace>/tasks/<task_gid>/attachments/`) to the Padmasana file service.

```bash
uv run padmasana-upload-attachments --file-service-url http://localhost:8080
```

**Useful flags:**
- `--file-service-url` (required): URL of your Padmasana file-service instance.
- `--file-service-token`: Optional Bearer token if authentication is required.
- `--concurrency N`: Number of parallel uploads (default: `8`).

**What it does:**
- Reads local files on disk and uploads them using multipart form-data.
- Saves upload metadata (including the file's assigned `uuid` and download URL) to `build/tasks/<task_gid>/uploaded_attachments.json`.
- Uses a background worker queue (`var/padmasana_jobs.json`) so already-uploaded files are never re-uploaded if interrupted.
- Safe at any `--concurrency`: records for the same task are written under a lock, so no upload record is ever lost.
- `uploaded_attachments.json` is the source of truth, not the job queue: re-running queues every attachment that has no record yet, even if an earlier job for it says `done`. Re-run until it reports `0 failed permanently`.
- Link-only attachments (Google Drive/external) get a uuid minted once here and kept across rebuilds.
- Saves `--file-service-url` to `var/config.json`, so `padmasana-format-docs` builds image links against the same file service (the token is never saved).
- If interrupted mid-upload, the file in flight may be uploaded again on the next run; that only leaves an unreferenced copy on the file service, never a duplicate record.

---

### Step 3: Build Tasks, Subtasks, Comments, and Activity Logs

Builds all task entities, comment streams, activity logs, tag associations, and attachment splits.

```bash
uv run padmasana-build-tasks --concurrency 8
```

**What it does:**
- **Pass 1**: Converts raw Asana task JSON into Padmasana task entities, separates task-level attachments from comment-level attachments, parses comment threads, builds `task_activity_log.json`, and maps sections and tags.
- **Pass 2**: Resolves parent/subtask relationships (`parent_task_uuid`).
- **Compile Step**: Combines individual task files from `build/tasks/*/` into root compilation files (`build/tasks.json`, `build/comments.json`, `build/attachments.json`, `build/comment_attachments.json`, `build/board_task.json`, etc.).
- Alerts if any Asana task assignees/creators do not exist in `build/padmasana_users.json`.

**Useful flags:**
- `--concurrency N`: Parallel task workers (default: `8`).
- `--compile`: Skip re-parsing and only recompile `build/tasks/*/` into root `build/` files.
- `--no-compile`: Run passes 1 and 2 without running the compile step.

---

### Step 4: User Mapping and Identity Synchronization

Padmasana references users via a `user_reference_code` / Firebase Auth UUID, whereas Asana references users by GID, name, and email. To ensure all mentions, assignees, and comment authors link correctly:

#### 1. Generate the Asana user map
```bash
uv run padmasana-map-users
```
Scans the entire Asana export (teams, projects, tasks, comments, and inline mentions) and generates `build/user_to_asana_gid.json`. It maps:
- `asana_gid`
- `name`
- `email`
- `user_reference_code` (linked from `padmasana_users.json` if available)
- `in_padmasana` (boolean)

If any Asana users are missing from `build/padmasana_users.json`, the command prints a summary warning listing them.

#### 2. Import Padmasana users from Firebase Auth
```bash
uv run padmasana-import-users \
  --firebase-auth-api-url http://localhost:8081 \
  --workspace my-workspace \
  --organization-unit engineering
```
- Fetches all workspace users from the Firebase Authorization service (identical to `padmasana-service`'s `import-users` CLI command).
- Writes and **merges** user records into `build/padmasana_users.json` (so running for multiple workspaces or organization units accumulates users rather than overwriting).
- Automatically saves `--firebase-auth-api-url`, `--workspace`, and `--organization-unit` to `var/config.json`. After the first run, you can simply execute:
  ```bash
  uv run padmasana-import-users
  ```
- Automatically re-runs user mapping and reports any Asana users who are still missing from Padmasana.

---

### Step 5: Inspect and Verify Rich Tasks

Before running document formatting across all tasks, use `padmasana-inspect-task` to find and inspect tasks with complex formatting (user mentions, comment threads, attachments, and board/task links):

#### List top candidate tasks ranked by field richness:
```bash
uv run padmasana-inspect-task --list-candidates
```
Displays a table of candidate tasks scored by number of attachments, comments, mentions in descriptions, and mentions in comments.

#### Inspect a specific task:
```bash
uv run padmasana-inspect-task --task-gid 1214535540091814
```
*(If `--task-gid` is omitted, it automatically picks the richest task in your build).*

This tool prints:
- Task metadata (name, GID, UUID, board, section, assignee).
- Attachments list (task-level and comment-level with file service URLs).
- Comments list (authors, timestamps, attachments).
- Side-by-side **Before vs. After HTML** showing how Asana mentions, images, and links convert to Padmasana / Tiptap format.

---

### Step 6: Format Descriptions and Comments (`format-docs`)

Converts all Asana-formatted HTML across task descriptions and comments into Padmasana-compliant HTML:

```bash
uv run padmasana-format-docs \
  --app-url http://localhost:3000 \
  --file-service-url http://localhost:8080
```

**HTML Transformations Performed:**
1. **User Mentions**:
   - *From*: `<a data-asana-type="user" data-asana-gid="1205361916477125">@Alice</a>`
   - *To*: `<span class="mention" data-type="mention" data-id="user-ref-uuid" data-label="Alice" data-profile-url="" data-email="alice@company.com">@Alice</span>`
2. **Inline Attachments & Images**:
   - *From*: `<img data-asana-type="attachment" data-asana-gid="1205361916477130" src="..."/>`
   - *To*: `<img src="http://localhost:8080/files/<file_uuid>/download" data-file-uuid="<file_uuid>" alt="screenshot.png" />`
3. **Asana Board & Task Links**:
   - *Board link*: `https://app.asana.com/0/<board_gid>/list` → `http://localhost:3000/my-boards/<board_uuid>`
   - *Task link*: `https://app.asana.com/0/<board_gid>/<task_gid>` → `http://localhost:3000/my-boards/<board_uuid>?taskId=<task_uuid>`

**Features & Flags:**
- Automatically updates both per-task files (`build/tasks/<gid>/task.json`, `comments.json`) and compiled flat files (`build/tasks.json`, `build/comments.json`).
- Automatically populates the `body` field on comments (expected by `padmasana-app`'s comment editor).
- Automatically saves `--app-url` and `--file-service-url` in `var/config.json`.
- `--dry-run`: Test and preview transformations without modifying files on disk.
- `--task-gid <gid>`: Format only a single task.
- Reports any unresolved user mentions at the end.

---

## Quick Command Reference

| Command | Purpose | Input / Flags | Key Outputs |
|---|---|---|---|
| `uv run padmasana-build-teams-and-boards` | Build teams, boards, sections, tags | `--team-owner GID=GID` | `build/teams.json`, `boards.json`, `sections.json` |
| `uv run padmasana-upload-attachments` | Upload local attachments to file service | `--file-service-url`, `--concurrency` | `build/tasks/<gid>/uploaded_attachments.json` |
| `uv run padmasana-build-tasks` | Build tasks, comments, activity logs | `--concurrency`, `--compile` | `build/tasks.json`, `comments.json`, `build/tasks/*/` |
| `uv run padmasana-map-users` | Map Asana users & report missing | `-v` | `build/user_to_asana_gid.json` |
| `uv run padmasana-import-users` | Fetch Firebase users into build | `--firebase-auth-api-url`, `--workspace`, `--organization-unit` | `build/padmasana_users.json` |
| `uv run padmasana-inspect-task` | Preview sample task HTML conversion | `--list-candidates`, `--task-gid` | Console report & side-by-side diff |
| `uv run padmasana-format-docs` | Convert HTML (mentions, files, links) | `--app-url`, `--file-service-url`, `--dry-run` | Formatted `task.json`, `comments.json`, `tasks.json` |

---

## Where Padmasana Build Files Live

Everything produced for Padmasana lands under `./build`:

```
build/
  teams.json                     # Padmasana team entities
  boards.json                    # Padmasana board entities
  sections.json                  # Padmasana board section entities
  tags.json                      # Board tags
  board_team.json                # Board-team pivot associations
  board_member.json              # Board membership
  team_member.json               # Team membership
  padmasana_users.json           # Cached Padmasana Firebase Auth users
  user_to_asana_gid.json         # Asana GID <-> Padmasana user reference mapping
  tasks.json                     # Compiled flat task entities
  board_task.json                # Task <-> Board associations
  board_section.json             # Task <-> Board section associations
  task_parent.json               # Subtask <-> Parent task associations
  task_tag.json                  # Task <-> Tag associations
  comments.json                  # Compiled comments (with Tiptap HTML body)
  attachments.json               # Compiled task-level attachments (with file-service UUIDs)
  comment_attachments.json       # Compiled comment attachments (with file-service UUIDs)
  task_activity_log.json         # Converted history/activity logs
  tasks/<task_gid>/              # Individual task records
    task.json                    # Task record (with Tiptap description_html)
    comments.json                # Comment thread
    attachments.json             # Task-level attachments
    comment_attachments.json     # Comment-level attachments
    task_activity_log.json       # Task activity events
    uploaded_attachments.json    # File-service upload metadata
```

All files in `build/` are ready to be seeded directly into the Padmasana database using `padmasana-service` seeder scripts.

