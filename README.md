# asana-migration

Exports an Asana account — teams, projects, sections, tasks, subtasks (nested,
any depth), comments, and collaborators — to a local, browsable JSON folder
tree, without ever exceeding Asana's API rate limit.

It's built to run **slowly and resumably**: everything heavier than "list a
team's projects" goes through a persistent job queue that a single background
worker drains one request at a time, paced by a token-bucket rate limiter. If
you stop the server and start it again later, the queue and all previously
imported data are exactly where you left them.

A local web UI walks you through it:

1. Paste an Asana **personal access token**.
2. **Import teams** — a couple of cheap calls list your workspaces/teams.
3. Open a team → its projects load from local cache instantly if you've
   fetched them before; otherwise one lightweight call lists them.
4. **Import** a project (or **import all** for the team) — this queues the
   deep crawl (sections → tasks → subtasks → comments → collaborators) and
   runs it in the background. Progress ("12/40 tasks imported") updates live
   and is always visible per project, so it's clear what's been imported and
   what hasn't.

## Run it

```bash
uv sync
uv run asana-migration serve
```

This opens `http://127.0.0.1:5050`. Use `--port`, `--host`, `--no-browser` to
customize.

### Or: run the whole export in one go, from the terminal

```bash
uv run asana-migration import-all
```

Prompts for a token if none is saved yet, then discovers every workspace,
team and project and imports all of them — sections, tasks, subtasks (any
depth), comments, collaborators — in one foreground run, still paced by the
same rate limiter. It logs what it's doing as it goes:

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

### Pagination (>100 records per Asana call)

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
  data/<workspace>/teams/<team>/projects/<project>/
    project.json  members.json  sections.json  _meta.json  _index.json
    tasks/<task>/
      task.json  comments.json  collaborators.json
      subtasks/<subtask>/        # same shape, recursively
  ```

  `_meta.json` is the per-project import status/progress the UI reads.
  `_index.json` is a flat gid → summary map used to render the task tree
  without reading every `task.json`.

Both `var/` and `data/` are gitignored.

## Rate limiting

Every Asana call goes through one shared token-bucket limiter (default 100
requests/minute — adjustable in the UI's Settings panel). A `429` response
pauses *all* in-flight work for the `Retry-After` duration Asana asks for,
not just the request that got throttled.
