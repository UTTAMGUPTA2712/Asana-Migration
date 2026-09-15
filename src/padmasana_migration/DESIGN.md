# padmasana_migration — how the seed data gets built

Status: draft — the "what" and "why" are settled, and the "how" below is now
grounded against `padmasana-service`'s actual entities/migrations/seeders (not
just its high-level design doc, which is stale in places — see the callouts
below). No migration code written yet.

## Goal

Produce, in `build/`, exactly the data padmasana's own bulk-import seeders
need — shaped, keyed, and validated so that loading it in (§5) is a clean
run with no issue: no missing identity rows, no dangling cross-schema
references, no constraint violations (NOT NULL columns, enum values,
unique keys), and no manual patch-up afterward. This package's job ends at
"the JSON in `build/` is correct and complete"; actually loading it into a
padmasana instance is a separate, later step (§5) that this package
doesn't perform.

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
  that data into padmasana is a separate, later step (see §5) that runs
  **inside `padmasana-service`**, not here.
- **Every write on the padmasana side must be an upsert, never a blind
  insert.** The one seeder padmasana already has (`TagsSeeder`, see §5)
  writes via `entityManager.upsertMany(Tag, tags)`. Every seeder this
  project adds does the same — matched on whatever unique key that table
  has (`email` / `user_reference_code` / `uuid` / composite unique
  constraint). That's what makes re-running a seed after a partial failure
  safe, and it's also what makes it safe to run against a padmasana
  instance where some of these people **already exist** (see the note in
  §"Padmasana's real schema"). **The one exception: identity rows are
  never written by this project — they're read and validated only, and a
  missing one is a hard error, not an upsert.** See "One person, four
  local identity rows."

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

## Padmasana's real schema — what this migration actually writes to

Everything below was read straight out of `padmasana-service`
(`/home/zenmonk-info/Desktop/Funibr/padmasana-service`) — entity files,
migrations, validators, and the one seeder it already has — not out of the
project's own `padmasana_design.md` HLD/LLD, which describes an earlier
shape (a `project-management` bounded context instead of today's
`board-management`, no `task_activity_log` or `comment` tables at all)
that the running code has since moved past. Where a fact below matters,
its source file is named so it can be re-checked after the app changes
again.

### Five bounded contexts, five schemas, no cross-schema foreign keys

padmasana is a NestJS/MikroORM app split into independently-owned Postgres
schemas — `team-management`, `board-management`, `task-management`,
`task-collaboration`, `notification-module` — one MikroORM "context" per
module. A column that points at a row in another module's schema is
**never a real foreign key**, always a plain `uuid`/`varchar` column (e.g.
`task_activity_log.task_uuid`, `board_team.team_uuid`, `assignee.
default_board_uuid`). Nothing in Postgres enforces these references — only
the app's own logic does — which means **our build scripts have to get the
ordering right ourselves**; the database won't catch it if we don't. See
§5 for the concrete order this project seeds in.

### Every table's real PK is a serial int — same-schema FKs use it, never the uuid

Confirmed against the ERD (`padmasana_erd.png`) and cross-checked against
`padmasana-service`'s own code (read-only — `TagsSeeder`,
`ImportDefaultBoardsHandler`, `AddBoardMemberHandler`,
`AddTeamMemberHandler`, `AddTaskCollaboratorHandler`): almost every table
has **two** identity columns — an auto-incrementing `id serial` (the real
Postgres primary key) and, on most tables, a separate app-generated
`uuid uuid` business key (the four identity tables have no `uuid` column
at all, just `email`/`user_reference_code`). **Same-schema relations
always point at the serial `id`, never the uuid** — `task.assignee_id`,
`task.parent_task_id`, `comment.author_id`, `task_tag.task_id/tag_id`,
`section_task.task_id/board_section_id`, `board_member.board_id/
member_id`, `team_member.team_id/workspace_member_id`,
`task_collaborator.collaborator_id`, comment-level `attachment.comment_id/
uploaded_by` are all plain integers. Only cross-schema references use the
uuid/varchar copy (matches "no cross-schema foreign keys" above).

This matters because **a serial `id` doesn't exist until Postgres assigns
it at insert time** — `build/`'s JSON can never contain one, only business
keys (uuid/email/user_reference_code, exactly as this doc already does
everywhere). padmasana's own code never works around this by trusting
`upsertMany`'s return value (which technically does come back with `id`
populated) — every call site discards it and instead **re-queries by
business key right after upserting**, to get a hydrated entity whose `.id`
MikroORM then uses to fill in the next row's integer FK. Our own §5
seeders need to follow the identical two-phase convention per table:
upsert parents by business key → re-query by that same key to build an
in-memory `{business_key → id}` map → use the map to write the next
layer's FK/pivot columns. This is one more level of sequencing than the
module-to-module order below — it also applies *within* a single module
(e.g. `task` and `tag` each need their own resolve pass before `task_tag`
can be written).

### One person, four local identity rows

There is no single "users" table padmasana-wide. Each module keeps its own
copy of every person it needs to reference, all four shaped almost
identically (`user_reference_code, email, name, last_name, profile_url,
workspace`), because that's the exact flat record padmasana's own
`import-users` CLI command produces from Firebase and fans out to each
module's own "import" port:

| Table | Schema/module | Extra fields beyond the shared shape | What references it |
|---|---|---|---|
| `workspace_member` | team-management | — | `team.owner_id`, `team_member` pivot |
| `member` | board-management | — | `board_member` pivot, `board.created_by` (their personal board) |
| `assignee` | task-management | `default_section_uuid` **uuid, NOT NULL**, `default_board_uuid` **uuid, NOT NULL** | `task.assignee_id` |
| `collaborator` | task-collaboration | — | `comment.author_id`, `task_activity_log.actor_id`, `task_collaborator.collaborator_id` |
| `users` (notification-module) | notification-module | — | *(deliberately not written to — see §4)* |

Source: `modules/team-management/.../workspace-member.entity.ts`,
`modules/board-management/.../member/member.entity.ts` +
`.../domain/member/member.ts`, `modules/task-management/.../assignee.
entity.ts`, `modules/task-collaboration/.../collaborator/collaborator.
entity.ts`; the real fan-out is
`modules/shared/features/import-users/orchestrator/import-users.
orchestrator.ts`.

**This means for every Asana person we see, `build_teams_and_boards.py`
needs to *resolve*, not create, up to four existing rows** — one per
target table (`workspace_member`, `member`, `assignee`, `collaborator`).
We assume the real `import-users` has already been run for every
zenmonk.tech person these Asana exports reference, so these rows already
exist; matching is by `email`. **If any Asana person's email has no
matching row in a given padmasana identity table, that's an error, not a
fallback** — the script reports every missing Asana `gid`/email/table
combination it found and halts before writing anything to `build/`,
rather than silently creating a placeholder row with a null
`user_reference_code`. (`users.json` in `build/` is really four sibling
lookup files — see the output list in §2 — each now an already-resolved
`email → user_reference_code` mapping per table, not a set of
upsert-ready records.)

**Every `assignee` row needs a real board to point at.** `default_board_
uuid`/`default_section_uuid` are NOT NULL — there's no way to create an
assignee without one. padmasana's own onboarding handles this by giving
every person a personal, auto-created board:
`ImportDefaultBoardsHandler` (`modules/board-management/src/features/
import-default-boards/import-default-boards.handler.ts`) looks for an
existing `board` row with `board_privacy = PERSONAL` and `created_by =
<their user_reference_code>`; if there isn't one, it creates `Board.
create({ name: 'My Tasks', default_section_name: 'Recently Assigned',
created_by: user_reference_code, board_privacy: BoardPrivacy.PERSONAL })`.
`build_teams_and_boards.py` has to do the exact same check-then-create for
any Asana person who doesn't already have one, purely so their `assignee`
row has somewhere valid to point — this has nothing to do with Asana data,
it's just satisfying a padmasana invariant.

**Confirmed:** `import-users` has already run for this workspace, so all
step 0's lookup needs is a one-time export/dump of padmasana's existing
identity tables (email + `user_reference_code`, per module), dropped in
as an input file the same way `data/` is — no live DB connection, in
keeping with this phase staying `data/`-in, `build/`-out (see Ground
rules). Getting that export is a small coordination task, not a design
question.

### Boards, teams, and their privacy enums

`board.board_privacy` and `team.privacy` are both a two-value enum —
**`PERSONAL | PRIVATE`, there is no `PUBLIC`**
(`modules/board-management/src/domain/board/enums/board-privacy.enum.ts`,
`modules/team-management/src/domain/team/enums/team-privacy.enum.ts`).
`PERSONAL` is reserved for the auto-generated "My Tasks" board above —
every real, migrated Asana project/team gets `PRIVATE`, full stop,
regardless of what Asana's own `project.json.public` boolean says (there's
no padmasana option that maps to "public" anyway, so that field is simply
not carried over).

**Team ownership is a required, explicit field, not inferred.**
`team.owner_id` is `nullable: false`, and `AddTeamHandler`
(`modules/team-management/src/features/add-team/`) requires a validated
`owner_id` at creation time — `Team.create()` then auto-adds the owner as
a member too. Asana's own `team.json` export carries no owner concept at
all (just `gid`/`name`/`description`), so `build_teams_and_boards.py` has
to pick one. Default rule, overridable: **the team's Asana member with the
lowest gid** (stable and deterministic across re-runs) becomes the
padmasana owner, unless a `--team-owner <team_gid>=<user_gid>` override is
given on the command line for that team.

**`board.created_by` for a real, migrated board (not a personal board)
follows the identical rule: the lowest-gid person among the Asana
project's own members.** Asana's `project.json` carries no creator/owner
concept either, so this is the same best-effort pick as team ownership,
by the same deterministic tiebreak, for the same reason (stable across
re-runs). Personal "My Tasks" boards are unaffected — their `created_by`
is always the board's own owner, already resolved in step 0/1 (see "Every
`assignee` row needs a real board to point at" above).

### Tags: free text, no enum

`tag.color` is `varchar(255)`, genuinely free text — no `@IsEnum`, no
fixed list anywhere in the codebase
(`modules/task-management/src/domain/tag/tag.ts`,
migration `20260330090003-create-tag.ts`). Asana's own tag colors (`tags.
json`, e.g. `"orange"`, `"yellow-green"`) can be carried over as-is,
verbatim, no mapping table needed. (`tag.name` is `jsonb` — a per-locale
map, e.g. `{"en": "..."}` — not a plain string; wrap Asana's tag name as
`{"en": name}`.) There is also no "add tag" API in padmasana at all —
tags only get into the database via a seeder
(`modules/task-management/.../seeders/20260423064218-tags-seeder.ts`),
which is exactly the shape ours will follow (§5).

### Tasks have no direct board column — sections are two-tier

`task` (task-management schema) has no `board_uuid`/`project_uuid` column
and no followers/collaborators collection at all
(`modules/task-management/.../task/task.entity.ts`). A task's board
membership is entirely indirect, through two join tables:

- `section_task` — links one `task` to one `board_section` (plus a
  fractional `order` for drag-and-drop position).
- `board_section` — task-management's own **local mirror** of a section:
  just `section_uuid` + `board_uuid`, plain uuid columns, no relation.

The *real* section object lives over in board-management's own `section`
table (`modules/board-management/.../section/section.entity.ts`, one row
per `board`, with its own `order`). So placing a migrated task into a
section means creating rows in **two** schemas: the real `section` row
under board-management (once per Asana section, part of script 2's board
build), and a `board_section` mirror row under task-management with the
same `uuid`/`board_uuid` (part of script 3, since `board_section` rows are
what `section_task` actually points at). `build_teams_and_boards.py`
writes the real sections and hands their uuids to `build_tasks.py` via
`build/sections.json`; `build_tasks.py` is what creates the `board_section`
mirror rows and the `section_task` links themselves.

Tags attach to a task via a plain `m:n` pivot, `task_tag` (`task_id`,
`tag_id`) — no extra fields, no per-task tag color override.

Followers/collaborators live entirely in task-collaboration's own
`task_collaborator` table (`collaborator_id`, `task_uuid` string,
`unique(collaborator, task_uuid)`) — one row per Asana follower per task.

### Attachments split in two: task-level vs. comment-level

padmasana has **two separate attachment tables**, not one:

- `attachment` (task-management schema) — `task_id`, `name`, `metadata`
  (json, unconstrained shape — see below), `uploaded_by` (plain varchar,
  not a relation), `created_at`. Attached directly to a task.
- `attachment` (task-collaboration schema, exported as `CommentAttachment`
  to avoid the name clash) — `comment_id`, `name`, `metadata`, `uploaded_
  by` (this one **is** a real `m:1` to `collaborator`), `created_at`.
  Attached to one specific comment.

Asana doesn't make this distinction — `attachments.json` is one flat list
per task, and a comment's `html_text` can *embed* one of those same
attachments inline (`<img ... data-asana-type="attachment"
data-asana-gid="1211656639227236" .../>` — confirmed against this export:
that gid matches an entry's `gid` in the same task's `attachments.json`
exactly). So `build_tasks.py` derives the split itself: scan every
comment's `html_text` for `data-asana-type="attachment"
data-asana-gid="(\d+)"`; any attachment gid found there becomes a
`CommentAttachment` row on that comment; every attachment gid *not* found
in any comment's html becomes a plain task-level `attachment` row. (On the
rare chance the same gid is embedded in more than one comment, the first
one chronologically wins and the rest log a warning — reusing the same
image in two comments isn't something Asana's export lets us represent as
two separate rows without duplicating the upload.)

**`uploaded_by` has no direct source on the attachment itself** — Asana's
`attachments.json` entries don't carry a creator. It's recoverable,
though: every `attachment_added` story in `stories.json` does carry
`created_by`, and its `html_text` contains `asset_id=<gid>` matching the
attachment's gid (confirmed against this export). So: match each
attachment to its `attachment_added` story by that `asset_id`, and take
`uploaded_by`/`created_at` from there. (This story type still isn't turned
into an activity-log row — see the mapping table below — it's read purely
to enrich the attachment record, nothing else.) If no matching story
exists (attachment predates story retention, or the story genuinely isn't
there), fall back to the task's own creator.

**`metadata`'s shape is not constrained anywhere in padmasana** — both
attachment tables type it as `Record<string, unknown>`/`any` with only
`@IsObject()` at the API boundary
(`modules/task-management/.../add-attachment.validator.ts`,
`modules/task-collaboration/.../add-task-comment.validator.ts`). Since
there's nothing to match, this project's contract is simple: for a real
file, `metadata` is **exactly the JSON the file-service's `POST /files`
hands back** (its `FileAggregateRootSchema`, see `openapi.yml`) —
`upload_attachments.py` stores that response verbatim, `build_tasks.py`
copies it through unchanged, no reshaping into our own fields.
Traceability back to the export rides along for free: the upload request
sends `{"asana_gid": ..., "source": "asana"}` as `FileStoreRequest.
metadata`, and the file-service's response echoes that same object back
inside its own `metadata` field. A link-only attachment (`gdrive`/
`external`, nothing ever uploaded) has no file-service response to store,
so it keeps its own small shape instead:

```jsonc
// host == "asana" (a real file, uploaded via POST /files — this *is* that response body, verbatim)
{
  "id": 123,
  "uuid": "<file-service's returned uuid>",
  "disk": "local",
  "path": "asana-migration/attachments",
  "name": "image.png",
  "checksum": "2aae6c35c94fcfb415dbe95f408b9ce91ee846ed",
  "metadata": { "asana_gid": "1211656684915713", "source": "asana" },
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

### Comments and activity — built together, the way padmasana does it

padmasana doesn't treat "the comment feed" and "the activity feed" as two
separate things bolted together — a comment *is* one of the fixed set of
event types the activity log understands. padmasana's `task_activity_log`
table (`modules/task-collaboration/.../task-activity-log/task-activity-log.
entity.ts`, `event_type` column backed by the enum in `.../enums/
event-type.enum.ts`) only ever stores one of **13 fixed event types**,
each with its own small, specific `data` payload, enforced at write time
by `ActivityDataConstraint`
(`modules/task-collaboration/.../record-task-activity-log/activity-data.
constraint.ts`) against the `ACTIVITY_DATA_MAPPER` table in
`record-task-activity-log-data.mapper.ts` — not a freeform log:

| padmasana event type | `data` keys required | Where it comes from |
|---|---|---|
| `task_created` | *(none)* | Synthesized once per task, from its own `created_at`; its `actor_id` is the same resolved value as the task's own `created_by` (see §3's field mapping) — one lookup, reused in both places |
| `task_completed` / `task_in_completed` | *(none)* | Asana story `marked_complete` / `marked_incomplete` |
| `task_assigned` | `assignee` | Asana story `assigned` |
| `task_unassigned` | `prev_assignee` | Asana story `unassigned` |
| `task_renamed` | `old_value`, `new_value` | Asana story `name_changed` — Asana's text doesn't always give us both values cleanly, best-effort |
| `task_description_added` | `value` | Asana story `notes_changed` (first one for a task) |
| `task_description_updated` | `old_value`, `new_value` | Asana story `notes_changed` (subsequent ones) — same best-effort caveat |
| `task_due_date_updated` | `due_date` | Asana story `due_date_changed` |
| `task_added_to_board` / `task_removed_from_board` | `board_name` | Asana story `added_to_project` / `removed_from_project` |
| `task_moved_to_section` | `previous_section`, `current_section`, `board_name` | Asana story `section_changed` — section names parsed from the story text, best-effort |
| `task_comment_added` | `comment_uuid` | **Every comment we build gets exactly one of these, pointing at it** — not read from `stories.json` at all |

(Note the exact enum string values are lower-snake-case —
`task_created`, not `TASK_CREATED` — matching the enum's declared values,
not the constant names.)

One extra real-code wrinkle worth building for deliberately, not just
noting: `TaskActivityLog.record()` **merges `new_value` into the existing
latest row instead of inserting a new one** when consecutive `task_renamed`
or `task_description_updated` events come from the same actor. Our own
insert of these rows should collapse consecutive same-actor rename/
description edits into one row the same way, so a rebuild doesn't produce
more rows than the real app ever would from the same input.

**Comments specifically:** each entry in `comments.json` becomes a
`comment` row (task-collaboration schema; `author_id` resolved from the
Asana commenter's email against the `collaborator` rows built in script 2)
*and* one `task_comment_added` activity row referencing its `uuid` — that
pairing is exactly what padmasana's own `ListTaskActivityHandler` does on
read (`modules/task-collaboration/.../list-task-activity/
list-task-activity.handler.ts`: filters activity rows by `event_type ===
TASK_COMMENT_ADDED`, batch-loads matching `comment`s by `data.comment_
uuid`, and `TaskActivityLogTransformer.enrichWithComment()` splices the
comment's own body into the activity row's `data` at read time). Asana's
`stories.json` also contains its own `comment_added` entries duplicating
the same events — those are skipped in favor of `comments.json`, which is
already the cleaner, purpose-built source.

**Everything else in `stories.json` that isn't in the table above —
subtask-added notices (redundant anyway: the same relationship is already
captured via `parent_task_id`, see the two-pass fix below), attachment-
added notices (mined for `uploaded_by` only, see above — never turned into
an activity row), reactions, custom fields, tags, dependencies, mentions,
rule automations, and a handful of other Asana-only story types — has no
padmasana event type to map onto, so it's left out rather than force-fit.**
Checked against this export: excluding `comment_added` (fully covered via
`comments.json` instead), close to 30% of remaining story entries fall into
this "no mapping" bucket. The activity tab in padmasana will still be a
genuine, complete history — just told entirely in padmasana's own
vocabulary, not Asana's.

## 1. `upload_attachments.py`

Goes through every task's `attachments.json`, uploading one attachment per
job (`--concurrency N` of them in flight at once, via the shared
`WorkerPool`):

- If it's a real file (`host: "asana"`, already downloaded to
  `local_path`) — `POST <file_service_url>/files` as `multipart/
  form-data`, per `openapi.yml`'s `FileStoreRequest`: `file` (the binary),
  `path` (fixed, `asana-migration/attachments`), `name` (the original
  filename), `metadata: {"asana_gid": ..., "source": "asana"}` for
  traceability. Whatever JSON comes back (`FileAggregateRootSchema`) is
  saved **verbatim** as this attachment's `metadata` — see the contract
  above.
- If it's a link only (`gdrive`/`external`) — nothing to upload, just
  carry it over as `{url, original_name, source, asana_gid}` (there's no
  file-service response to store).

**`--file-service-url` is a new required command-line flag** — the file
service's base URL (e.g. `http://localhost:8080`, per `openapi.yml`'s
`servers`), so this script never hardcodes an environment; every upload
job POSTs to `<file_service_url>/files`. `openapi.yml` declares
`security: Bearer` (JWT) on every path, but as far as we currently know
this instance doesn't actually enforce it, so auth is **optional**:
`--file-service-token`, also a new flag. If it's given, every request
carries `Authorization: Bearer <token>`; if it's omitted, the request
goes out with no `Authorization` header at all — no dummy/placeholder
value, just skip the header entirely.

Either way, the result is written to a **new** file under
`build/tasks/<task_gid>/attachments.json` — one entry per Asana attachment,
each already carrying the exact `metadata` object script 3 will copy
straight into whichever `attachment`/`comment_attachment` row it becomes.
The original file in `data/` is never edited.

Resumable: before queuing a job, check whether `build/` already has a
result for that attachment — if so, skip it. That check, plus `JobQueue`'s
own crash-safety, means stopping halfway through and restarting never
re-uploads what's already done, even across many worker threads.

## 2. `build_teams_and_boards.py`

Everything here is small enough (compared to tasks) to build in memory in
one pass and write out at the end. In dependency order (each step's output
is needed by the next, since nothing here is a real foreign key — see
"five schemas, no cross-schema foreign keys" above):

0. **Identity resolution gate, before anything else is built.** For every
   distinct `{gid, email, name}` we've seen across every Asana
   team/project/task/story/comment/attachment, look that email up against
   padmasana's existing `workspace_member`/`member`/`collaborator`/
   `assignee` rows. **Any Asana person whose email isn't found in one of
   those four tables is a hard error** — the script reports every missing
   `gid`/email/table combination it found and exits without writing
   anything to `build/`. (This is what makes the doc's usual
   "upsert-safe" language not apply to identity rows specifically — see
   "One person, four local identity rows.")
1. **The four identity lookup files**, written only once step 0 passes
   clean, matched **by email**:
   - `workspace_members.json` → `{user_reference_code, email, name,
     last_name, profile_url, workspace}` — `user_reference_code` filled
     from the real padmasana row found in step 0, never `null`, never a
     placeholder.
   - `members.json` — same shape, for board-management's `member` table.
   - `collaborators.json` — same shape, for task-collaboration's
     `collaborator` table.
   - `assignees.json` — same shape **plus** `default_board_uuid`/
     `default_section_uuid`, filled in only after step 2 below creates
     (or finds) each person's personal board.
   - `name`/`last_name` split from Asana's single `name` field: last
     whitespace-separated token is `last_name`, everything before it is
     `name` — best-effort, same caveat as the other free-text splits in
     this doc. (Display/cross-check only — `email` is the join key, not
     name.)
2. **Personal "My Tasks" boards**, one per person from step 1, **only for
   anyone who doesn't already have one** (mirrors `ImportDefaultBoardsHandler`
   exactly — see above): `board_privacy: PERSONAL`, `created_by:
   <user_reference_code>` (always real, resolved in step 0),
   `default_section_name: "Recently Assigned"`. Their uuids feed back into
   `assignees.json`'s `default_board_uuid`/`default_section_uuid` from
   step 1.
3. **Teams** — one per Asana team, `privacy: PRIVATE`, `owner_id` from the
   rule above (lowest-gid member, or a `--team-owner` override).
4. **Boards** — one per Asana project, `privacy: PRIVATE` always (padmasana
   has no `PUBLIC`; Asana's own `project.json.public` is not carried over).
5. **Sections** — one per Asana section, under board-management's real
   `section` table, `order` assigned from Asana's own section ordering.
   These uuids are what `build_tasks.py` mirrors into `board_section` rows
   later (see "Tasks have no direct board column" above).
6. **Board ↔ team** (`board_team`: `board_id`, `team_uuid`).
7. **Board ↔ member** (`board_member` pivot).
8. **Team ↔ member** (`team_member` pivot).

Output: one JSON file per piece in `build/` — `workspace_members.json`,
`members.json`, `collaborators.json`, `assignees.json`, `boards.json`,
`personal_boards.json`, `teams.json`, `sections.json`, `board_teams.json`,
`board_members.json`, `team_members.json`.

## 3. `build_tasks.py`

The big one — this walks the export task by task (including subtasks),
one job per task via the same `JobQueue`/`WorkerPool` as script 1
(`--concurrency N`, its own `var/padmasana_jobs.json`), rather than holding
everything in memory at once.

`build_tasks.py` generates a fresh `uuid` for every task it builds (padmasana's
`task.uuid` column, `task-management`'s own business key) — that's the value
every other file in `build/` joins against (`section_task`, `task_tag`,
`task_collaborator`, `comments`, `attachments`, `activity_log`, and a
task's own `parent_task_gid`, see below), never the eventual serial `id`
(which doesn't exist until §5's seeder inserts the row).

**Task field mapping**, Asana `task.json` → padmasana `task`:

| Asana field | padmasana column | Notes |
|---|---|---|
| `name` | `name` | |
| `notes` (plain) | `description` | `html_notes` is not used — padmasana's `description` is plain text |
| `assignee.email` | `assignee_id` | `assignee_id` is a serial int FK (see "Every table's real PK is a serial int"); the build JSON stores the assignee's `email` instead, and §5's seeder resolves `email → id` at seed time; `null` stays `null` |
| `created_at` | `created_at` | |
| `due_on` / `due_at` | `due_date` | `due_date` is a bare `date` column — `due_at`'s time-of-day is dropped if only it's set |
| `completed_at` | `completed_at` | |
| `assignee` set at all | `assigned_at` | best-effort: the `assigned` story's `created_at` if one exists, else the task's own `created_at` |
| `parent_task_gid` | `parent_task_id` | also a serial int FK; the build JSON stores the parent's own build-generated task `uuid` (filled in pass 2 below), and §5's level-by-level pass resolves it to the real `parent_task_id` at seed time |
| *(synthesized)* | `created_by` | resolved once and reused for the synthesized `task_created` activity row's `actor_id` too (see the activity table below): the task's own creator if recoverable from `stories.json`'s first entry, else the containing board's `created_by` (itself resolved by the lowest-gid rule above, so always resolvable — no separate placeholder needed) |

**The subtask problem:** a task can be a subtask of another task, and we
don't process them in any guaranteed order — the parent might come before
or after the child. Fix: two passes, both driven by the same `_index.json`
Asana already wrote per project (which already records `parent_task_gid`
for every task):

- **Pass 1** — one job per task, `parent_task_id` left `null`. Every task
  row can be created safely, regardless of order, because nothing points
  at anything yet. Also builds, in the same job: tags (`task_tag`), the
  section placement (`board_section` mirror row + `section_task`,
  see above), collaborators (`task_collaborator`, one per Asana
  follower), attachments (task-level vs. comment-level split, see above —
  this step only looks up the new file-service id/url `upload_attachments.
  py` already produced, it doesn't upload anything itself), comments, and
  the activity log (see below).
- **Pass 2** — now that every task exists, a second, cheap pass walks
  every task that had a `parent` in Asana and patches in its
  `parent_task_id`. Nothing ever points at a task that isn't there yet, so
  this can't break regardless of processing order in pass 1.

Output, per task, under `build/tasks/<task_gid>/`: `task.json`,
`tags.json`, `board_section.json` + `section_task.json`,
`task_collaborators.json`, `attachments.json` (task-level),
`comment_attachments.json`, `comments.json`, `activity_log.json`. A final,
cheap, single-threaded compile step (run once the queue drains, either as
its own `--compile` flag or automatically at the end of the run) walks
`build/tasks/*/` and concatenates each of those into the flat files
padmasana's seeders actually read: `tasks.json`, `task_tags.json`,
`board_sections.json`, `section_tasks.json`, `task_collaborators.json`,
`attachments.json`, `comment_attachments.json`, `comments.json`,
`task_activity_log.json`.

## 4. What's still deliberately left out

- Nothing gets written to `notification-module`'s `users` table — we don't
  want real people getting notified about years-old Asana activity, and
  that table exists purely to drive email sends.
- No real-time events get fired, and no `outbox_message`/`inbox_message`
  rows get written — this is a bulk backfill straight into each module's
  own tables, not a simulated run through padmasana's normal outbox/
  RabbitMQ pipeline (`modules/shared/infrastructure/database/...
  outbox_message` / `inbox_message`, see §2.4 of padmasana's own HLD).

## 5. After `build/` is complete — wiring into padmasana-service's seeders

At that point `build/` holds everything padmasana needs, fully formed —
nothing left to compute. The actual loading happens as a separate step,
**inside `padmasana-service` itself**, using the exact pattern it already
has for `TagsSeeder`:

- MikroORM seeders are **per-module** — `mikro-orm.config.ts`'s
  `seeder.path` is `dist/${moduleName}/src/infrastructure/database/
  seeders`, and `npm run seed:run` takes a `--context <module-name>`
  (`task-management`, `team-management`, `board-management`,
  `task-collaboration`). So this isn't one seeder — it's one seeder class
  per module, each living under that module's own `.../infrastructure/
  database/seeders/`, each registered into that module's `DatabaseSeeder.
  run()` the same way `TagsSeeder` is registered today
  (`this.call(entityManager, [TagsSeeder, PadmasanaMigrationSeeder])`).
- A seeder is **not** run inside NestJS's DI container — `TagsSeeder`
  simply `new`s up its own dependencies (`new ConfigService()`, its own
  HTTP client) inside `run(entityManager)`. Ours does the same: read the
  relevant `build/*.json` file(s) directly (a plain path, or an env var
  pointing at the copied `build/` folder) and persist via `entityManager.
  upsertMany(Entity, records, {onConflictFields: [...], onConflictAction:
  'merge'})` — matching `ImportDefaultBoardsHandler`'s own use of that
  exact call for `Member`.
- **Every table with same-schema children needs a resolve pass right
  after its upsert** — never trust `upsertMany`'s return value for the
  generated `id` (see "Every table's real PK is a serial int" above);
  re-query by business key instead, build a `{business_key → id}` map,
  then use it to write the next layer. E.g. `task` and `tag` each get
  upserted and re-queried by `uuid` before `task_tag` can be written;
  `assignee` before `task.assignee_id`; `collaborator` before
  `task_collaborator`/`comment.author_id`/`task_activity_log.actor_id`.
- **Run order matters, even though Postgres won't enforce it**, because
  every cross-module reference is a plain uuid/varchar column, not a real
  FK (see "five schemas" above): `team-management` (workspace_members,
  personal-board-adjacent bits) → `board-management` (members, personal
  boards, sections, teams, boards, board_team/board_member) →
  `task-collaboration` (collaborators) → `task-management` (assignees,
  which need §2's personal boards to already exist; then tasks pass 1;
  then task_collaborator/attachments/comments/activity, which need
  collaborators to already exist) → `task-management` pass 2
  (`parent_task_id`, see below).
- **`parent_task_id` needs its own level-by-level pass, not a single
  patch.** It's the one genuinely recursive same-schema FK (a task's
  parent is itself a task, arbitrarily deep), so a flat "upsert everyone,
  then resolve" won't work in one step — the resolve step needs the
  *parent's* freshly-generated `id`, which doesn't exist until the parent
  row is written. Insert breadth-first instead:
  1. `upsertMany` every task with no Asana parent (root tasks) — the
     already-planned pass 1, `parent_task_id` left `null`.
  2. Re-query that batch by `uuid` to build `{task_uuid → id}`.
  3. `upsertMany` every task whose Asana parent is now in that map, this
     time setting `parent_task_id` directly from it (no null-then-patch
     needed for this batch).
  4. Re-query *that* batch too, merge into the same map, and repeat from
     step 3 for the next level down, until a pass finds no task left to
     insert.
  5. Anything still unplaced after that has no resolvable parent in the
     export (a genuine data problem, not an ordering one) — log it and
     handle separately; the two-pass build in §3 already guarantees every
     `parent_task_gid` points at a real task in the export, so this should
     never actually trigger.
- Copy the finished `build/` folder into the `padmasana-service` repo (or
  point the seeders at it via an env var — either works, the seeder code
  is the same), then run `npm run seed:run -- --context <module>` once per
  module, in the order above.

## Still to confirm before writing code

1. **Getting the padmasana identity export** — the file feeding step 0's
   `email → user_reference_code` lookup (see "One person, four local
   identity rows"). A small coordination task with whoever has padmasana
   DB access, not a design decision.
2. **Team ownership / board `created_by` rule.** No Asana data names a
   team's owner or a project's creator (`team.json`/`project.json` only
   have `gid`/`name`/`description`); this doc's default for both is
   "lowest-gid member." Worth a sanity check with whoever knows these
   teams/projects before committing to it as the default.
