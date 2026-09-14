# padmasana_migration — how the seed data gets built

Status: draft — describes the plan, no code yet.

This lives at `src/padmasana_migration/`, a second top-level package right
next to `src/asana_migration/` in this same repo — not a separate project.
One codebase, one `pyproject.toml`, one `uv sync`; the two packages just
don't import from each other (`asana_migration` only ever talks to Asana,
`padmasana_migration` only ever reads what it already exported):

```
asana-migration/
  data/                       # Asana export — read-only from here on
  build/                      # everything the 3 scripts below produce (gitignored, like data/ and var/)
  src/
    asana_migration/          # existing: Asana -> data/
    padmasana_migration/      # this package: data/ -> build/
```

## Ground rules

- **Never touch `data/`.** Everything this package builds goes into a
  brand new top-level folder, `build/`, same level as `data/`/`var/`.
  If a script gets something wrong, we just delete `build/` and run it
  again — re-downloading the Asana export is the thing we can't redo, not
  re-running our own script.
- **Nothing here writes to a real database yet.** These three scripts only
  produce finished, ready-to-seed JSON files in `build/`. Actually loading
  that data into padmasana is a separate, later step (see §5).

## The three scripts, in order

| # | Script | What it does | Needs the network? |
|---|---|---|---|
| 1 | `upload_attachments.py` | Uploads every attachment file to the file-service, one at a time | Yes — file-service only |
| 2 | `build_teams_and_boards.py` | Builds users, teams, and boards (small enough to do entirely in memory) | No |
| 3 | `build_tasks.py` | Builds tasks, comments, tags, and everything task-related (the big one, goes through the export task by task) | No |

Each one is its own command, safe to stop and re-run — same idea as
`asana-migration`'s `import-all` / `download-attachments` split.

## Running 1 and 3 in parallel — reusing what `import-all` already built

Both `upload_attachments.py` and `build_tasks.py` process thousands of
small, independent units of work — one attachment, one task — which is
exactly the shape `asana_migration` already solved for its own Asana
import. No need to reinvent it: reuse the same two pieces directly, by
importing them —

- **`JobQueue`** (`asana_migration.jobs`) — a persistent, resumable,
  crash-safe queue backed by one JSON file.
- **`WorkerPool`** (`asana_migration.worker`) — a pool of threads draining
  that queue, exactly how `download-attachments` already runs its uploads
  concurrently today.

**The one thing that has to change: the queue file.** `asana_migration`'s
own queue already lives at `var/jobs.json` — this project must never point
at that file, or the two tools' progress tracking would collide. `JobQueue`
already takes a `path` argument for exactly this reason, so
`padmasana_migration` just gives it its own: `var/padmasana_jobs.json`,
sitting right next to it, never shared.

**Worker count:** like `download-attachments`, sized directly by a
`--concurrency` flag, not derived from a rate limit — there's no Asana call
to pace here, only file-service uploads (script 1) and local JSON building
(script 3).

`build_teams_and_boards.py` (script 2) stays single-threaded — the data is
small enough that parallelizing it would add complexity for no real
speedup.

## 1. `upload_attachments.py`

Goes through every task's `attachments.json`, uploading one attachment per
job (`--concurrency N` of them in flight at once, via the shared
`WorkerPool`):

- If it's a real file (`host: "asana"`, already downloaded to
  `local_path`) — upload it to the file-service, get back its new id/url.
- If it's a link only (`gdrive`/`external`) — nothing to upload, just carry
  it over as-is.

Either way, the result is written to a **new** file under
`build/.../attachments.json` — same shape as the original, plus the new
file-service id/url and whatever else padmasana's attachment table needs.
The original file in `data/` is never edited.

Resumable: before queuing a job, check whether `build/` already has a
result for that attachment — if so, skip it. That check, plus `JobQueue`'s
own crash-safety, means stopping halfway through and restarting never
re-uploads what's already done, even across many worker threads.

## 2. `build_teams_and_boards.py`

Everything here is small enough (compared to tasks) to build in memory in
one pass and write out at the end:

1. **Users.** padmasana already has a way to bring people in —
   `import-users`, which pulls everyone from Firebase and reshapes them into
   one flat record: `user_reference_code, email, name, last_name,
   profile_url, workspace`. That exact same shape is what every table that
   stores a person expects. So: take Asana's `{gid, email, name}` for every
   person we see, match them to that shape by **email**, and build our user
   records to look exactly like the ones `import-users` would produce —
   not something foreign to the rest of the app.
2. **Teams** — one per Asana team.
3. **Boards** — one per Asana project.
4. **Board ↔ team** — which boards belong to which teams.
5. **Board ↔ member** — who's on each board.
6. **Team ↔ member** — who's on each team.

Output: one JSON file per piece in `build/` (`users.json`, `teams.json`,
`boards.json`, `board_teams.json`, `board_members.json`,
`team_members.json`).

## 3. `build_tasks.py`

The big one — this walks the export task by task (including subtasks),
one job per task via the same `JobQueue`/`WorkerPool` as script 1
(`--concurrency N`, its own `var/padmasana_jobs.json`), rather than holding
everything in memory at once.

**The subtask problem:** a task can be a subtask of another task, and we
don't process them in any guaranteed order — the parent might come before
or after the child. Fix: two passes.

- **Pass 1** — write every task with no parent set at all. Every task row
  can be created safely, regardless of order, because nothing points at
  anything yet.
- **Pass 2** — now that every task exists, go back and fill in each task's
  parent. Nothing ever points at a task that isn't there yet, so this can't
  break.

Alongside each task, this script also builds its assignee, tags, section
placement, collaborators, and attachments — the attachments step just
looks up the new file-service id/url that script 1 already produced, it
doesn't upload anything itself. **Comments and activity are built too,
completely, matched to how padmasana itself records them** — see below.

### Comments and activity — built together, the way padmasana does it

padmasana doesn't treat "the comment feed" and "the activity feed" as two
separate things bolted together — a comment *is* one of the fixed set of
event types the activity log understands. Reading the actual code:
padmasana's `task_activity_log` table only ever stores one of **13 fixed
event types**, each with its own small, specific `data` payload — not a
freeform log:

| padmasana event type | `data` it stores | Where it comes from |
|---|---|---|
| `TASK_CREATED` | *(none)* | Synthesized once per task, from its own `created_at` |
| `TASK_COMPLETED` / `TASK_IN_COMPLETED` | *(none)* | Asana story `marked_complete` / `marked_incomplete` |
| `TASK_ASSIGNED` | `assignee` | Asana story `assigned` |
| `TASK_UNASSIGNED` | `prev_assignee` | Asana story `unassigned` |
| `TASK_RENAMED` | `old_value`, `new_value` | Asana story `name_changed` — Asana's text doesn't always give us both values cleanly, best-effort |
| `TASK_DESCRIPTION_ADDED` / `TASK_DESCRIPTION_UPDATED` | `value` / `old_value`+`new_value` | Asana story `notes_changed` — same best-effort caveat |
| `TASK_DUE_DATE_UPDATED` | `due_date` | Asana story `due_date_changed` |
| `TASK_ADDED_TO_BOARD` / `TASK_REMOVED_FROM_BOARD` | `board_name` | Asana story `added_to_project` / `removed_from_project` |
| `TASK_MOVED_TO_SECTION` | `previous_section`, `current_section`, `board_name` | Asana story `section_changed` — section names parsed from the story text, best-effort |
| `TASK_COMMENT_ADDED` | `comment_uuid` | **Every comment we build gets exactly one of these, pointing at it** — not read from `stories.json` at all |

**Comments specifically:** each entry in `comments.json` becomes a
`task_collaboration.comment` row *and* one `TASK_COMMENT_ADDED` activity
row referencing its `uuid` — that pairing is exactly what padmasana's own
comment-transformer code does (it reads the activity row, then fetches the
real comment by that `comment_uuid` to fill in the content). Asana's
`stories.json` also contains its own `comment_added` entries duplicating
the same events — those are skipped in favor of `comments.json`, which is
already the cleaner, purpose-built source.

**Everything else in `stories.json` that isn't in the table above —
subtask-added notices (redundant anyway: the same relationship is already
captured via `parent`, see the two-pass fix above), attachment-added
notices, reactions, custom fields, tags, dependencies, mentions, rule
automations, and a handful of other Asana-only story types — has no
padmasana event type to map onto, so it's left out rather than force-fit.**
Checked against this export: excluding `comment_added` (fully covered via
`comments.json` instead), close to 30% of remaining story entries fall into
this "no mapping" bucket. The activity tab in padmasana will still be a
genuine, complete history — just told entirely in padmasana's own
vocabulary, not Asana's.

## 4. What's still deliberately left out

- Nothing gets written to padmasana's notification tables — we don't want
  real people getting notified about years-old Asana activity.
- No real-time events get fired — this is a bulk backfill, not live usage.

## 5. After `build/` is complete

At that point `build/` holds everything padmasana needs, fully formed —
nothing left to compute. The actual loading happens as a separate step,
later: write small seed scripts **inside `padmasana-service` itself**
(it already has this exact pattern — `seed:run`, its MikroORM seeders).
Copy the finished `build/` folder into that repo, and let a seeder there
read the JSON and save it using padmasana's own code, so its normal
checks/relationships still apply.

## Still to confirm before writing code

1. Exact shape of `attachment.metadata` padmasana expects — need one real
   example from the running app.
2. File-service auth — does uploading need a token we don't have yet?
3. Whether `board_privacy` / `tag.color` have a fixed list of allowed
   values, so Asana's free-text equivalents can be mapped correctly.
4. Who becomes a team's `owner` when a team has no single obvious lead.
