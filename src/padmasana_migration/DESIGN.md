# padmasana_migration — Architecture & Data Flow

Implementation status: scripts 1–3 (§6–§8) implemented and run end-to-end
against a real export. `import_users.py` (§11) was added afterward, once
it became clear a local `email → user_reference_code` mapping was worth
having for offline sanity-checking before §10's live seed run.

## 1. Purpose

`padmasana_migration` turns the Asana export already sitting in `data/`
into finished, ready-to-seed JSON in `build/` — exactly the data
padmasana's own bulk-import seeders need, shaped, keyed, and validated so
that loading it (§10) is a single clean run: no missing identity rows, no
dangling cross-schema references, no constraint violations (`NOT NULL`
columns, enum values, unique keys), no manual patch-up afterward.

This package's job ends at "the JSON in `build/` is correct and
complete." Loading it into a real padmasana instance is a separate, later
step (§10) that runs inside `padmasana-service`, not here.

## 2. Assumptions

This design is built on the following as fixed facts, not open questions:

- **`padmasana-service`'s own `import-users` CLI command has already been
  run for this workspace.** Every Asana person referenced anywhere in the
  export already has a real identity row in padmasana —
  `workspace_member` (team-management), `member` (board-management),
  `assignee` (task-management), and `collaborator` (task-collaboration) —
  matched by `email`. This package never creates identity rows; it only
  ever reads/validates them (at seed time, see §5 and §10). Don't confuse
  that command with this package's own same-named `padmasana-import-users`
  (§11) — same upstream call, but this one only reads and saves a local
  file, it writes nothing to padmasana's database.
- **Asana names no owner for a team and no creator for a project.**
  `team.json`/`project.json` carry only `gid`/`name`/`description`. Where
  padmasana requires one (`team.owner_id`, a migrated board's
  `created_by`), the default is **the lowest-gid Asana member** of that
  team/project — stable and deterministic across re-runs — overridable
  per team via `--team-owner <team_gid>=<user_gid>`.
- **The file-service's auth requirement is not enforced in practice.**
  `openapi.yml` declares `security: Bearer` on every path, but this
  instance does not require it. Auth is therefore optional at the
  transport level: a token is sent if provided, omitted otherwise.
- **padmasana's schema, as read from `padmasana-service` and
  `padmasana_erd.png`, is the source of truth** — not the project's own
  `padmasana_design.md` HLD/LLD, which describes an earlier shape
  (`project-management` instead of today's `board-management`, no
  `task_activity_log`/`comment` tables) the running code has moved past.
  Every schema fact below cites its source file so it can be re-verified
  if `padmasana-service` changes.

## 3. System boundaries

```
asana-migration/
  data/                       # Asana export — read-only from here on
  build/                      # everything the scripts below produce (gitignored, like data/ and var/)
  src/
    asana_migration/          # existing: Asana -> data/
    padmasana_migration/      # this package: data/ -> build/
```

`padmasana_migration` lives at `src/padmasana_migration/`, a second
top-level package next to `src/asana_migration/` in the same repo, one
`pyproject.toml`, one `uv sync`. The two packages never import from each
other — `asana_migration` only ever talks to Asana, `padmasana_migration`
only ever reads what `asana_migration` already exported. The one
exception is `import_users.py` (§11), which talks to padmasana's own
identity source (the Firebase Authorization service) directly, not
through `asana_migration` at all.

Rules that hold across the three core scripts (§6–§8; §11's
`import_users.py` is a separate, later addition — see its own section for
how it differs):

- **`data/` is never modified.** Every output goes into a brand-new
  top-level folder, `build/`. If a script gets something wrong, `build/`
  is deleted and the script re-run — re-downloading the Asana export is
  the only step that can't be redone cheaply.
- **No script in this package writes to a real database.** All three
  produce finished JSON in `build/` only. Loading it into padmasana is
  §10, and runs inside `padmasana-service`.
- **Every eventual write on the padmasana side is an upsert, never a
  blind insert**, matched on whatever unique key the target table has
  (`email` / `user_reference_code` / `uuid` / a composite unique
  constraint) — this is what makes a re-run after a partial failure safe,
  and what makes it safe to run against a padmasana instance where some
  rows already exist. The one exception: identity rows (`workspace_
  member`/`member`/`assignee`/`collaborator`) are never written by this
  project at all — only read and validated (§2, §5, §10).

## 4. Execution model

| # | Script | Produces | Needs the network? |
|---|---|---|---|
| — | `import_users.py` (§11, optional, run any time) | `padmasana_users.json` | Yes — Firebase Authorization service only |
| 1 | `upload_attachments.py` | `build/tasks/<gid>/attachments.json` (task-level, with file-service metadata) | Yes — file-service only |
| 2 | `build_teams_and_boards.py` | `boards.json`, `teams.json`, `sections.json`, and the board/team pivot files | No |
| 3 | `build_tasks.py` | `build/tasks/<gid>/*.json`, compiled into the flat files padmasana's seeders read | No |

`import_users.py` has no `#` — it doesn't participate in the §8/§10
pipeline (nothing else in `build/` depends on `padmasana_users.json`
existing, and it depends on nothing else in `build/` either beyond
reading whatever's already there to validate against) — see §11.

Each script is independently resumable. Scripts 1 and 3 both process
thousands of small, independent units of work (one attachment, one task)
— the same shape `asana_migration` already solved for its own import —
so both reuse its existing machinery directly:

- **`JobQueue`** (`asana_migration.jobs`) — a persistent, resumable,
  crash-safe queue backed by one JSON file.
- **`WorkerPool`** (`asana_migration.worker`) — a thread pool draining
  that queue, the same way `download-attachments` runs its uploads
  concurrently today.

They get their own queue file, `var/padmasana_jobs.json` — never
`asana_migration`'s own `var/jobs.json`, or the two tools' progress
tracking would collide. Worker count is a plain `--concurrency` flag on
both, not derived from a rate limit (there's no Asana call to pace here,
only file-service uploads and local JSON building).

`build_teams_and_boards.py` (script 2) stays single-threaded — its input
is small enough that parallelizing it would add complexity for no real
speedup.

## 5. padmasana's data model

padmasana is a NestJS/MikroORM app split into five independently-owned
Postgres schemas — `team-management`, `board-management`,
`task-management`, `task-collaboration`, `notification-module` — one
MikroORM "context" per module.

### 5.1 No cross-schema foreign keys

A column that points at a row in another module's schema is never a real
FK, always a plain `uuid`/`varchar` column (e.g. `task_activity_log.
task_uuid`, `board_team.team_uuid`, `assignee.default_board_uuid`).
Postgres enforces none of these — only the app's own logic does — so the
write order in §10 has to get this right; the database won't catch it if
it doesn't.

### 5.2 Every table's real PK is a serial int; same-schema FKs use it, never the uuid

Almost every table (source: `padmasana_erd.png`, cross-checked against
`TagsSeeder`, `ImportDefaultBoardsHandler`, `AddBoardMemberHandler`,
`AddTeamMemberHandler`, `AddTaskCollaboratorHandler`) has **two** identity
columns: an auto-incrementing `id serial` (the real Postgres primary key)
and, on most tables, a separate app-generated `uuid uuid` business key
(the four identity tables have no `uuid` column at all — just `email`/
`user_reference_code`).

**Same-schema relations always point at the serial `id`, never the
uuid** — `task.assignee_id`, `task.parent_task_id`, `comment.author_id`,
`task_tag.task_id`/`tag_id`, `section_task.task_id`/`board_section_id`,
`board_member.board_id`/`member_id`, `team_member.team_id`/
`workspace_member_id`, `task_collaborator.collaborator_id`, comment-level
`attachment.comment_id`/`uploaded_by` are all plain integers. Only
cross-schema references use the uuid/varchar copy (§5.1).

A serial `id` doesn't exist until Postgres assigns it at insert time, so
**`build/`'s JSON never contains one — only business keys** (uuid, email,
or `user_reference_code`). padmasana's own code never relies on
`upsertMany`'s return value for this (it does come back with `id`
populated, but every call site in `padmasana-service` discards it) —
instead it **re-queries by business key right after upserting**, to get a
hydrated entity whose `.id` MikroORM then uses for the next row's integer
FK. §10 follows the identical two-phase convention per table: upsert
parents by business key → re-query by that same key to build an
in-memory `{business_key → id}` map → use the map to write the next
layer's FK/pivot columns. This applies *within* a module too (e.g. `task`
and `tag` each need their own resolve pass before `task_tag` can be
written).

### 5.3 Identity: four tables, one person, resolved live at seed time

There is no single "users" table padmasana-wide. Each module keeps its
own copy of every person it needs to reference, all four shaped almost
identically (`user_reference_code, email, name, last_name, profile_url,
workspace`) — the exact flat record padmasana's own `import-users` CLI
command produces from Firebase and fans out to each module's own "import"
port, atomically and identically (same person, same `user_reference_code`,
in all four, in one run):

| Table | Schema/module | Extra fields beyond the shared shape | What references it |
|---|---|---|---|
| `workspace_member` | team-management | — | `team.owner_id`, `team_member` pivot |
| `member` | board-management | — | `board_member` pivot, `board.created_by` (their personal board) |
| `assignee` | task-management | `default_section_uuid` **uuid, NOT NULL**, `default_board_uuid` **uuid, NOT NULL** | `task.assignee_id` |
| `collaborator` | task-collaboration | — | `comment.author_id`, `task_activity_log.actor_id`, `task_collaborator.collaborator_id` |
| `users` (notification-module) | notification-module | — | *(deliberately not written to — §9)* |

Source: `modules/team-management/.../workspace-member.entity.ts`,
`modules/board-management/.../member/member.entity.ts` +
`.../domain/member/member.ts`, `modules/task-management/.../assignee.
entity.ts`, `modules/task-collaboration/.../collaborator/collaborator.
entity.ts`; the fan-out itself is `modules/shared/features/import-users/
orchestrator/import-users.orchestrator.ts`.

**No script in this package resolves or validates any of this — that
happens entirely in §10, at seed time, against padmasana's live
database.** Every file `build_teams_and_boards.py` and `build_tasks.py`
write carries a person's Asana `email` and nothing else; no
`user_reference_code` ever appears in `build/`. Because all four identity
tables are guaranteed identical copies from the same `import-users` run,
§10 only ever queries **one of them** — `workspace_member`,
team-management — to get a person's `user_reference_code`, then reuses
that value for `member`, `assignee`, and `collaborator` too. **A live
query against `workspace_member` that finds no row for an Asana person's
email is a hard error at seed time** — the seeder reports the Asana
`gid`/email and halts that module's run.

**Every `assignee` row needs a real board to point at** —
`default_board_uuid`/`default_section_uuid` are `NOT NULL`. padmasana's
own onboarding satisfies this by giving every person a personal,
auto-created board: `ImportDefaultBoardsHandler`
(`modules/board-management/src/features/import-default-boards/
import-default-boards.handler.ts`) looks for an existing `board` row with
`board_privacy = PERSONAL` and `created_by = <their user_reference_code>`;
if there isn't one, it creates `Board.create({ name: 'My Tasks',
default_section_name: 'Recently Assigned', created_by: user_reference_
code, board_privacy: BoardPrivacy.PERSONAL })`. §10's board-management
seeder does the identical check-then-create, live, immediately after
resolving each person's identity — mirroring `ImportDefaultBoardsHandler`
exactly rather than approximating it offline.

### 5.4 Boards and teams

`board.board_privacy` and `team.privacy` are both a two-value enum —
**`PERSONAL | PRIVATE`, there is no `PUBLIC`**
(`modules/board-management/src/domain/board/enums/board-privacy.enum.ts`,
`modules/team-management/src/domain/team/enums/team-privacy.enum.ts`).
`PERSONAL` is reserved for the auto-generated "My Tasks" board (§5.3);
every real, migrated Asana project/team gets `PRIVATE`, full stop,
regardless of Asana's own `project.json.public` boolean (padmasana has no
"public" option, so that field is never carried over).

`team.owner_id` is `nullable: false`, and `AddTeamHandler`
(`modules/team-management/src/features/add-team/`) requires a validated
`owner_id` at creation time — `Team.create()` auto-adds the owner as a
member too. Per §2's assumption, the owner is the team's lowest-gid Asana
member (or a `--team-owner` override); `build/`'s `teams.json` carries
that person's `email`, and §10 resolves it to `owner_id` live, same as
every other same-schema FK (§5.2).

`board.created_by` for a real, migrated board (not a personal one)
follows the identical rule and mechanism: the lowest-gid person among the
Asana project's own members, carried in `build/`'s `boards.json` as
`created_by_email`, resolved live by §10. Personal "My Tasks" boards are
unaffected — their `created_by` is always the board's own owner, resolved
live in §5.3/§10.

### 5.5 Tags: free text, no enum

`tag.color` is `varchar(255)`, genuinely free text — no `@IsEnum`, no
fixed list anywhere in the codebase (`modules/task-management/src/domain/
tag/tag.ts`, migration `20260330090003-create-tag.ts`). Asana's own tag
colors (`tags.json`, e.g. `"orange"`, `"yellow-green"`) carry over as-is,
verbatim. `tag.name` is `jsonb` — a per-locale map, e.g. `{"en": "..."}`
— so Asana's tag name is wrapped as `{"en": name}`. There is no "add tag"
API in padmasana at all — tags only reach the database via a seeder
(`modules/task-management/.../seeders/20260423064218-tags-seeder.ts`),
the exact shape this project's own tag seeder follows (§10).

### 5.6 Tasks have no direct board column — sections are two-tier

`task` (task-management schema) has no `board_uuid`/`project_uuid` column
and no followers/collaborators collection
(`modules/task-management/.../task/task.entity.ts`). A task's board
membership is entirely indirect, through two join tables:

- `section_task` — links one `task` to one `board_section` (plus a
  fractional `order` for drag-and-drop position).
- `board_section` — task-management's own local mirror of a section:
  `section_uuid` + `board_uuid`, plain uuid columns, no relation.

The real section object lives in board-management's own `section` table
(`modules/board-management/.../section/section.entity.ts`, one row per
`board`, with its own `order`). Placing a migrated task into a section
therefore creates rows in two schemas: the real `section` row under
board-management (script 2, once per Asana section), and a
`board_section` mirror row under task-management with the same
`uuid`/`board_uuid` (script 3, since `section_task` points at
`board_section`, not `section`). `build_teams_and_boards.py` writes the
real sections and hands their uuids to `build_tasks.py` via
`build/sections.json`; `build_tasks.py` creates the `board_section`
mirror rows and the `section_task` links.

Tags attach to a task via a plain `m:n` pivot, `task_tag` (`task_id`,
`tag_id`) — no extra fields, no per-task color override. Followers/
collaborators live entirely in task-collaboration's own `task_collaborator`
table (`collaborator_id`, `task_uuid` string, `unique(collaborator,
task_uuid)`) — one row per Asana follower per task.

### 5.7 Attachments split in two: task-level vs. comment-level

padmasana has two separate attachment tables:

- `attachment` (task-management schema) — `task_id`, `name`, `metadata`
  (json, unconstrained), `uploaded_by` (plain varchar, not a relation),
  `created_at`. Attached directly to a task.
- `attachment` (task-collaboration schema, exported as `CommentAttachment`
  to avoid the name clash) — `comment_id`, `name`, `metadata`,
  `uploaded_by` (a real `m:1` to `collaborator`), `created_at`. Attached
  to one specific comment.

Asana makes no such distinction — `attachments.json` is one flat list per
task, and a comment's `html_text` can embed one of those same attachments
inline (`<img ... data-asana-type="attachment" data-asana-gid="..." .../>`
— the gid matches an entry's `gid` in the same task's `attachments.json`).
`build_tasks.py` derives the split itself: scan every comment's
`html_text` for `data-asana-type="attachment" data-asana-gid="(\d+)"`; any
attachment gid found there becomes a `CommentAttachment` row on that
comment; every attachment gid *not* found in any comment's html becomes a
plain task-level `attachment` row. If the same gid is embedded in more
than one comment, the first one chronologically wins and the rest log a
warning (Asana's export gives no way to represent reusing one upload
across two comments as two separate rows).

`uploaded_by` has no direct source on the attachment itself — Asana's
`attachments.json` entries carry no creator. It's recovered from
`stories.json`: every `attachment_added` story carries `created_by`, and
its `html_text` contains `asset_id=<gid>` matching the attachment's gid.
Each attachment is matched to its `attachment_added` story by that
`asset_id`, and `uploaded_by`/`created_at` taken from there (this story
type is read only to enrich the attachment record — it is not turned into
an activity-log row, §5.8). If no matching story exists, `uploaded_by`
falls back to the task's own creator.

`metadata`'s shape is not constrained anywhere in padmasana (`Record<
string, unknown>`/`any`, `@IsObject()` only at the API boundary —
`modules/task-management/.../add-attachment.validator.ts`, `modules/
task-collaboration/.../add-task-comment.validator.ts`). This project's
contract: for a real file, `metadata` is exactly the JSON the
file-service's `POST /files` hands back (`FileAggregateRootSchema`, see
`openapi.yml`) — `upload_attachments.py` stores that response verbatim,
`build_tasks.py` copies it through unchanged, no reshaping. The upload
sends no `FileStoreRequest.metadata` (the deployed file service answers
500 to it, and padmasana-app never sends it either), so traceability back
to the export is the record's own top-level `asana_gid`. A link-only attachment (`gdrive`/`external`,
nothing ever uploaded) has no file-service response to store, so it keeps
its own small shape instead:

```jsonc
// host == "asana" (a real file, uploaded via POST /files — this *is* that response body, verbatim)
{
  "id": 123,
  "uuid": "<file-service's returned uuid>",
  "disk": "local",
  "path": "/",
  "name": "image.png",
  "checksum": "2aae6c35c94fcfb415dbe95f408b9ce91ee846ed",
  "metadata": null,
  "created_at": "2024-01-25T16:03:35.000000Z",
  "updated_at": "2024-01-25T16:03:35.000000Z"
}
// host == "gdrive" / "external" (link only, nothing uploaded)
{
  "url": "<the original external/gdrive url, carried over as-is>",
  "original_name": "image.png",
  "source": "gdrive",               // or "external"
  "asana_gid": "1211656639227236"
}
```

### 5.8 Comments and activity, built together

A comment *is* one of the fixed event types padmasana's activity log
understands, not a separate feed bolted on. `task_activity_log`
(`modules/task-collaboration/.../task-activity-log/task-activity-log.
entity.ts`, `event_type` backed by the enum in `.../enums/
event-type.enum.ts`) only ever stores one of 13 fixed event types, each
with its own small `data` payload, enforced at write time by
`ActivityDataConstraint` against `ACTIVITY_DATA_MAPPER` in
`record-task-activity-log-data.mapper.ts`:

| padmasana event type | `data` keys required | Where it comes from |
|---|---|---|
| `task_created` | *(none)* | Synthesized once per task, from its own `created_at`; its `actor_id` is the same resolved value as the task's own `created_by` (§7) — one lookup, reused in both places |
| `task_completed` / `task_in_completed` | *(none)* | Asana story `marked_complete` / `marked_incomplete` |
| `task_assigned` | `assignee` | Asana story `assigned` |
| `task_unassigned` | `prev_assignee` | Asana story `unassigned` |
| `task_renamed` | `old_value`, `new_value` | Asana story `name_changed` — best-effort, Asana's text doesn't always give both values cleanly |
| `task_description_added` | `value` | Asana story `notes_changed` (first one for a task) |
| `task_description_updated` | `old_value`, `new_value` | Asana story `notes_changed` (subsequent ones) — same best-effort caveat |
| `task_due_date_updated` | `due_date` | Asana story `due_date_changed` |
| `task_added_to_board` / `task_removed_from_board` | `board_name` | Asana story `added_to_project` / `removed_from_project` |
| `task_moved_to_section` | `previous_section`, `current_section`, `board_name` | Asana story `section_changed` — section names parsed from story text, best-effort |
| `task_comment_added` | `comment_uuid` | Every comment built gets exactly one of these, pointing at it — not read from `stories.json` at all |

(Enum string values are lower-snake-case — `task_created`, not
`TASK_CREATED` — matching the enum's declared values, not the constant
names.)

`TaskActivityLog.record()` merges `new_value` into the existing latest row
instead of inserting a new one when consecutive `task_renamed` or
`task_description_updated` events come from the same actor. This
project's own insert of these rows collapses consecutive same-actor
rename/description edits into one row the same way, so a rebuild never
produces more rows than the real app would from the same input.

**Comments specifically:** each entry in `comments.json` becomes a
`comment` row (task-collaboration schema; `author_id` resolved from the
Asana commenter's email) *and* one `task_comment_added` activity row
referencing its `uuid` — the same pairing padmasana's own
`ListTaskActivityHandler` reconstructs on read (`modules/
task-collaboration/.../list-task-activity/list-task-activity.handler.ts`:
filters activity rows by `event_type === TASK_COMMENT_ADDED`, batch-loads
matching `comment`s by `data.comment_uuid`, and `TaskActivityLogTransformer.
enrichWithComment()` splices the comment's body into the activity row at
read time). Asana's `stories.json` also contains its own `comment_added`
entries duplicating the same events — those are skipped in favor of
`comments.json`, the cleaner, purpose-built source.

Everything else in `stories.json` not in the table above — subtask-added
notices (redundant: the relationship is already captured via
`parent_task_id`, §7), attachment-added notices (mined for `uploaded_by`
only, §5.7 — never turned into an activity row), reactions, custom
fields, tags, dependencies, mentions, rule automations, and other
Asana-only story types — has no padmasana event type to map onto and is
left out rather than force-fit. In this export, excluding `comment_added`
(fully covered via `comments.json`), close to 30% of remaining story
entries fall into this "no mapping" bucket; the activity tab in padmasana
is still a complete history, told entirely in padmasana's own vocabulary.

## 6. Script 1 — `upload_attachments.py`

Walks every task's `attachments.json`, one upload per job (`--concurrency
N` in flight at once, via `WorkerPool`):

- **Real file** (`host: "asana"`, already downloaded to `local_path`) —
  `POST <file_service_url>/files` as `multipart/form-data`, per
  `openapi.yml`'s `FileStoreRequest`: `file` (the binary), `path`
  (`--file-service-path`, else `NEXT_PUBLIC_FILES_STORAGE_PATH`, else `/`),
  `name` (the original filename) - the same
  three fields padmasana-app's own upload sends. Whatever JSON comes
  back (`FileAggregateRootSchema`) is saved verbatim as this attachment's
  `metadata` (§5.7).
- **Link only** (`gdrive`/`external`) — nothing to upload; carried over as
  `{url, original_name, source, asana_gid}` (no file-service response to
  store).

Command-line surface:

- `--file-service-url` (required) — the file service's base URL (e.g.
  `http://localhost:8080`, per `openapi.yml`'s `servers`). Every upload
  job POSTs to `<file_service_url>/files`.
- `--file-service-token` (optional) — per §2's assumption, sent as
  `Authorization: Bearer <token>` when given; omitted entirely (no
  placeholder) when not.

Either way, the result is written to a new file under
`build/tasks/<task_gid>/attachments.json` — one entry per Asana
attachment, already carrying the exact `metadata` object script 3 copies
straight into whichever `attachment`/`comment_attachment` row it becomes.
`data/` is never edited.

Resumable: before queuing a job, check whether `build/` already has a
result for that attachment — if so, skip it. That check, plus `JobQueue`'s
own crash-safety, means stopping halfway through and restarting never
re-uploads what's already done, even across many worker threads.

## 7. Script 2 — `build_teams_and_boards.py`

Everything here is small enough to build in memory in one pass and write
out at the end. This script never touches identity (§5.3 — that work is
entirely §10's, at seed time, against padmasana's live database). Every
reference to a person below is just their Asana `email`, carried straight
through. Steps run in dependency order, since nothing here is a real
foreign key (§5.1):

1. **Teams** — one per Asana team, `privacy: PRIVATE`, `owner_email` per
   §5.4's rule (a business key, resolved live by §10, not `owner_id`).
2. **Boards** — one per Asana project, `privacy: PRIVATE` always,
   `created_by_email` from the same rule.
3. **Sections** — one per Asana section, under board-management's real
   `section` table, `order` from Asana's own section ordering. These
   uuids are what `build_tasks.py` mirrors into `board_section` rows
   (§5.6).
4. **Board ↔ team** (`board_team`: `board_uuid`, `team_uuid`).
5. **Board ↔ member** (`board_member`: `board_uuid`, `member_email`).
6. **Team ↔ member** (`team_member`: `team_uuid`, `workspace_member_
   email`).

Output: `boards.json`, `teams.json`, `sections.json`, `board_teams.json`,
`board_members.json`, `team_members.json`. Personal "My Tasks" boards and
every identity table are §10's responsibility, not this script's.

## 8. Script 3 — `build_tasks.py`

The big one — walks the export task by task (including subtasks), one job
per task via the same `JobQueue`/`WorkerPool` as script 1 (`--concurrency
N`, its own `var/padmasana_jobs.json`), rather than holding everything in
memory at once.

`build_tasks.py` generates a fresh `uuid` for every task it builds
(padmasana's `task.uuid` column) — the value every other file in `build/`
joins against (`section_task`, `task_tag`, `task_collaborator`,
`comments`, `attachments`, `activity_log`, and a task's own
`parent_task_gid`, below), never the eventual serial `id` (§5.2).

**Task field mapping**, Asana `task.json` → padmasana `task`:

| Asana field | padmasana column | Notes |
|---|---|---|
| `name` | `name` | |
| `notes` (plain) | `description` | |
| `html_notes` | `description_html` | carried through alongside plain `description`, same as a comment's `text`/`html_text` pair (§5.8) |
| `assignee.email` | `assignee_id` | a serial int FK (§5.2); build JSON stores the assignee's `email`, §10 resolves `email → id`; `null` stays `null` |
| `created_at` | `created_at` | |
| `due_on` / `due_at` | `due_date` | `due_date` is a bare `date` column — `due_at`'s time-of-day is dropped if only it's set |
| `completed_at` | `completed_at` | |
| `assignee` set at all | `assigned_at` | best-effort: the `assigned` story's `created_at` if one exists, else the task's own `created_at` |
| `parent_task_gid` | `parent_task_id` | also a serial int FK; build JSON stores the parent's own build-generated task `uuid` (pass 2, below), §10's level-by-level pass resolves it |
| *(synthesized)* | `created_by` | resolved once, reused for the synthesized `task_created` activity row's `actor_id` too (§5.8): the task's own creator if recoverable from `stories.json`'s first entry, else the containing board's `created_by` (§5.4 — always resolvable) |

**The subtask problem:** a task can be a subtask of another task, and
tasks aren't processed in any guaranteed order — the parent might come
before or after the child. Fix: two passes, both driven by the same
`_index.json` Asana already wrote per project (which already records
`parent_task_gid` for every task):

- **Pass 1** — one job per task, `parent_task_id` left `null`. Every task
  row can be created safely regardless of order, since nothing points at
  anything yet. Also builds, in the same job: tags (`task_tag`), section
  placement (`board_section` mirror row + `section_task`, §5.6),
  collaborators (`task_collaborator`, one per Asana follower), attachments
  (task-level vs. comment-level split, §5.7 — this step only looks up the
  file-service id/url `upload_attachments.py` already produced), comments,
  and the activity log (§5.8).
- **Pass 2** — once every task exists, a second, cheap pass walks every
  task that had a parent in Asana and patches in its `parent_task_id`.
  Nothing ever points at a task that isn't there yet, so this can't break
  regardless of pass-1 ordering.

Output, per task, under `build/tasks/<task_gid>/`: `task.json`,
`tags.json`, `board_section.json` + `section_task.json`,
`task_collaborators.json`, `attachments.json` (task-level),
`comment_attachments.json`, `comments.json`, `activity_log.json`. A final,
single-threaded compile step (once the queue drains, either `--compile` or
automatic at the end of the run) walks `build/tasks/*/` and concatenates
each of those into the flat files padmasana's seeders actually read:
`tasks.json`, `task_tags.json`, `board_sections.json`, `section_tasks.
json`, `task_collaborators.json`, `attachments.json`, `comment_
attachments.json`, `comments.json`, `task_activity_log.json`.

## 9. Deliberately out of scope

- **`notification-module`'s `users` table is never written to** — that
  table exists purely to drive email sends, and real people should not
  get notified about years-old Asana activity.
- **No real-time events, no `outbox_message`/`inbox_message` rows** —
  this is a bulk backfill straight into each module's own tables, not a
  simulated run through padmasana's normal outbox/RabbitMQ pipeline
  (`modules/shared/infrastructure/database/... outbox_message` /
  `inbox_message`, §2.4 of padmasana's own HLD).
- **`import_users.py` (§11) does not create identity rows either** —
  despite hitting the same Firebase Authorization endpoint
  padmasana-service's own `import-users` command does, it only reads the
  response and saves it to a local file. Populating `workspace_member`
  and the other identity tables stays exclusively padmasana-service's
  job, per §2's assumption.

## 10. Loading `build/` into padmasana

Once `build/` is complete, nothing is left to compute — loading it is a
separate step, inside `padmasana-service` itself, using the pattern it
already has for `TagsSeeder`.

**Seeder structure.** MikroORM seeders are per-module —
`mikro-orm.config.ts`'s `seeder.path` is `dist/${moduleName}/src/
infrastructure/database/seeders`, and `npm run seed:run` takes a
`--context <module-name>` (`task-management`, `team-management`,
`board-management`, `task-collaboration`). This project adds one seeder
class per module, each under that module's own `.../infrastructure/
database/seeders/`, registered into that module's `DatabaseSeeder.run()`
the way `TagsSeeder` is registered today (`this.call(entityManager,
[TagsSeeder, PadmasanaMigrationSeeder])`). A seeder is not run inside
NestJS's DI container — `TagsSeeder` simply `new`s up its own
dependencies inside `run(entityManager)`; this project's seeders do the
same, reading `build/*.json` directly (a plain path, or an env var
pointing at the copied `build/` folder) and persisting via `entityManager.
upsertMany(Entity, records, {onConflictFields: [...], onConflictAction:
'merge'})` — matching `ImportDefaultBoardsHandler`'s own use of that call
for `Member`.

**Resolve-then-link, per table (§5.2).** Every table with same-schema
children needs a resolve pass right after its upsert — never the return
value of `upsertMany`; re-query by business key, build a `{business_key →
id}` map, use it to write the next layer. `task` and `tag` are each
upserted and re-queried by `uuid` before `task_tag` can be written;
`assignee` before `task.assignee_id`; `collaborator` before
`task_collaborator`/`comment.author_id`/`task_activity_log.actor_id`.

**Identity and personal boards, resolved live, not read from `build/`**
(§5.3). For every distinct email `build/`'s files reference,
team-management's seeder queries `workspace_member` by email to get
`user_reference_code` — a hard error, halting the run, if it's missing.
That value is reused for `member`, `assignee`, and `collaborator` in the
other three modules, no separate query needed. board-management's seeder
then does `ImportDefaultBoardsHandler`'s own check-then-create for each
person's personal board, live, right after.

**Module run order** (§5.1 — cross-module references are plain
uuid/varchar, Postgres enforces none of it):

```
team-management (identity resolution)
  → board-management (personal boards, sections, teams, boards, board_team/board_member)
  → task-collaboration (collaborators — identity resolution reused from team-management)
  → task-management (assignees — needs board-management's personal boards;
                      then tasks pass 1;
                      then task_collaborator/attachments/comments/activity — needs collaborators)
  → task-management pass 2 (parent_task_id, below)
```

**`parent_task_id`: a level-by-level pass, not a single patch.** It's the
one genuinely recursive same-schema FK (a task's parent is itself a task,
arbitrarily deep), so a flat "upsert everyone, then resolve" doesn't work
in one step — the resolve step needs the *parent's* freshly-generated
`id`, which doesn't exist until the parent row is written. Insert
breadth-first instead:

1. `upsertMany` every task with no Asana parent (root tasks) — pass 1,
   `parent_task_id` left `null`.
2. Re-query that batch by `uuid` to build `{task_uuid → id}`.
3. `upsertMany` every task whose Asana parent is now in that map, setting
   `parent_task_id` directly from it (no null-then-patch for this batch).
4. Re-query that batch too, merge into the same map, repeat from step 3
   for the next level down, until a pass finds no task left to insert.
5. Anything still unplaced has no resolvable parent in the export — a
   data problem, not an ordering one. Log it and handle separately; §8's
   two-pass build already guarantees every `parent_task_gid` points at a
   real task in the export, so this should never actually trigger.

**Deployment.** Copy the finished `build/` folder into the
`padmasana-service` repo (or point the seeders at it via an env var —
either works, the seeder code is the same), then run `npm run seed:run --
context <module>` once per module, in the order above.

## 11. `import_users.py` — a local identity pre-check, not part of §4's pipeline

§2 assumes padmasana already has an identity row for every Asana person
this export names, resolved live at seed time (§10) — this package never
writes those rows itself. `import_users.py` doesn't change that; it exists
because "assume the identity rows are there" was an untestable assumption
until this script gave it something to check against *before* a real §10
seed run hits DESIGN.md §10's "hard error, halting the run" for a missing
one.

**What it calls.** The exact same request padmasana-service's own
`import-users` CLI command makes
(`modules/shared/infrastructure/cli-commands/commands/import-users.
command.ts` → `ImportUsersOrchestrator` →
`FirebaseAuthorizationServiceClient.importWorkspaceUsers`): a plain,
unauthenticated `POST {FIREBASE_AUTH_API_URL}/workspace-users/import`
against the Firebase Authorization service, body `{workspace,
organization_unit}` — no bearer token, no session/CSRF, matching
`HttpClient`'s own request building (`modules/shared/infrastructure/
http/http-client.ts`), which sends no auth header at all. The response is
normalized into the same shape `ImportUsersOrchestrator` builds from it:
`{user_reference_code (Firebase's own `code_reference`), email, name,
last_name, profile_url, workspace}` — `user_reference_code` is exactly
padmasana's own `workspace_member.user_reference_code` column
(`workspace-member.entity.ts`), the value every other identity table
(`member`, `assignee`, `collaborator`) reuses once team-management
resolves it by email (§10).

**What it writes.**

- `build/padmasana_users.json` — the normalized list above, verbatim.
  Nothing downstream in `build/` reads this file back in — `build_tasks.py`
  and `build_teams_and_boards.py` still only ever write `_email` fields,
  per §5's field mappings, unchanged. It exists purely as a local
  reference and for the validation step below.
- Nothing else. No `workspace_member`/`member`/`assignee`/`collaborator`
  row is created anywhere, on padmasana or otherwise (§9) — this command
  only ever reads.

**Validation.** After fetching, it scans every flat `build/*.json` file
(the per-task `build/tasks/*/` tree is already folded into these by
`compile_build`, §8) for any `email`/`*_email` key, and warns for any
value with no matching `email` in the fetched user list — the exact
condition §10's live seeder hard-errors on, caught here instead, offline,
before that run starts. `--skip-validate` fetches and saves only.

**Why it's not numbered 1/2/3 like the others (§4).** It has no place in
the §4 pipeline's dependency order — nothing in `build/` depends on
`padmasana_users.json` existing (scripts 1–3, §6–§8, all still run fine
without ever calling this), and it depends on nothing scripts 1–3
produce that it doesn't already re-read fresh each time it's run. It's
safe to run before, after, or interleaved with any of them, as often as
useful (e.g. re-run right before a real §10 seed to catch anyone added to
the workspace since the last check).
