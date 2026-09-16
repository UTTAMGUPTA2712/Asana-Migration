"""Script 2 - `build_teams_and_boards.py` (DESIGN.md §7).

Everything here is small enough to build in memory in one pass and write out
at the end - no job queue, single-threaded (DESIGN.md §4). Never touches
identity (DESIGN.md §5.3) - every reference to a person below is just their
Asana `email`, carried straight through; §10 resolves it live, later.

Reads only from `data/` (via `asana_migration.storage.Paths`/`read_json`,
the exact layout `asana_migration` already wrote - see its `storage.py`
module docstring) and writes only to `build/` - `data/` is never modified.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from asana_migration.storage import Paths, read_json, write_json

from .people import build_person_registry, resolve_owner_email

log = logging.getLogger("padmasana_migration.build_teams_and_boards")

# `asana_migration.importer.other_projects_team_gid` mints synthetic team
# dirs with this prefix for projects visible to you but not on any of your
# real Asana teams (see that module's docstring). They have no real members
# to pick an owner from, so they're never migrated as a padmasana `team` -
# their projects still become boards, just with no `board_team` row (a board
# with no real team behind it is a legitimate, if team-less, board).
OTHER_PROJECTS_TEAM_GID_PREFIX = "other-projects-"


def _existing_uuids(build_dir: Path | None, filename: str) -> dict[str, str]:
    """asana_gid -> uuid already written by a previous run of this script,
    if any - reused so re-running it (e.g. to pick up newly-imported teams/
    projects) doesn't reissue identities for things already seeded into a
    real padmasana instance (same reasoning as `build_tasks._stable_uuid`)."""
    if build_dir is None:
        return {}
    return {row["asana_gid"]: row["uuid"] for row in read_json(build_dir / filename, default=[]) or [] if row.get("asana_gid")}


def build(data_paths: Paths, team_owner_overrides: dict[str, str] | None = None, build_dir: Path | None = None) -> dict:
    """Returns the in-memory result (also what the tests/CLI report from) -
    callers that want it on disk call `write_output` with this. `build_dir`,
    if given, is only ever read from here (to recover previously-minted
    uuids, see `_existing_uuids`) - never written to until `write_output`."""
    team_owner_overrides = team_owner_overrides or {}
    registry = build_person_registry(data_paths)

    existing_team_uuids = _existing_uuids(build_dir, "teams.json")
    existing_board_uuids = _existing_uuids(build_dir, "boards.json")
    existing_section_uuids = _existing_uuids(build_dir, "sections.json")

    tags = _build_tags(data_paths, existing_tag_uuids=_existing_uuids(build_dir, "tags.json"))
    teams: list[dict] = []
    boards: dict[str, dict] = {}  # project_gid -> board (deduped - a project can be on >1 team)
    sections: list[dict] = []
    board_teams: list[dict] = []
    board_members: list[dict] = []
    team_members: list[dict] = []

    board_uuid_by_project_gid: dict[str, str] = {}
    seen_board_team_pairs: set[tuple[str, str]] = set()

    if not data_paths.root.exists():
        log.warning("No data directory at %s - nothing to build.", data_paths.root)
        return _result(tags, teams, boards, sections, board_teams, board_members, team_members)

    for workspace_dir in sorted(p for p in data_paths.root.iterdir() if p.is_dir()):
        teams_dir = workspace_dir / "teams"
        if not teams_dir.exists():
            continue

        for team_dir in sorted(teams_dir.iterdir()):
            team_json = read_json(team_dir / "team.json", default={}) or {}
            team_gid = team_json.get("gid")
            if not team_gid:
                continue
            is_virtual = team_json.get("is_virtual") or team_gid.startswith(OTHER_PROJECTS_TEAM_GID_PREFIX)

            projects_index = read_json(team_dir / "projects_index.json", default=[]) or []
            team_uuid = None
            if not is_virtual:
                members = read_json(team_dir / "members.json", default=[]) or []
                owner_email, source = resolve_owner_email(
                    members, registry, team_owner_overrides.get(team_gid),
                )
                if not owner_email:
                    log.warning(
                        "Team '%s' (%s): no resolvable owner (no members with a known email) - "
                        "skipping this team (its projects still become boards).",
                        team_json.get("name"), team_gid,
                    )
                else:
                    team_uuid = existing_team_uuids.get(team_gid) or str(uuid.uuid4())
                    teams.append({
                        "asana_gid": team_gid,
                        "uuid": team_uuid,
                        "name": team_json.get("name"),
                        "description": team_json.get("description") or "",
                        "privacy": "PRIVATE",
                        "owner_email": owner_email,
                    })
                    log.info("Team '%s' (%s): owner=%s (%s)", team_json.get("name"), team_gid, owner_email, source)
                    for member in members:
                        email = member.get("email")
                        if email:
                            team_members.append({"team_uuid": team_uuid, "workspace_member_email": email})

            for stub in projects_index:
                project_gid = stub.get("gid")
                if not project_gid:
                    continue
                if project_gid not in boards:
                    board = _build_board(
                        data_paths, workspace_dir, project_gid, registry, sections,
                        existing_board_uuids, existing_section_uuids,
                    )
                    if board is None:
                        continue
                    boards[project_gid] = board
                    board_uuid_by_project_gid[project_gid] = board["uuid"]
                    for member in board.pop("_members", []):
                        email = member.get("email")
                        if email:
                            board_members.append({"board_uuid": board["uuid"], "member_email": email})

                if team_uuid is not None:
                    board_uuid = board_uuid_by_project_gid.get(project_gid)
                    pair = (board_uuid, team_uuid)
                    if board_uuid and pair not in seen_board_team_pairs:
                        seen_board_team_pairs.add(pair)
                        board_teams.append({"board_uuid": board_uuid, "team_uuid": team_uuid})

    return _result(tags, teams, boards, sections, board_teams, board_members, team_members)


def _build_tags(data_paths: Paths, existing_tag_uuids: dict[str, str]) -> list[dict]:
    """`tag.color` is genuinely free text, `tag.name` is a per-locale `jsonb`
    map (DESIGN.md §5.5) - workspace-wide, small, built in memory here the
    same way teams/boards are. A `uuid` is minted per tag so
    `build_tasks.py`'s `task_tag` pivot has a stable business key to
    reference (DESIGN.md §10: "task and tag are each upserted and
    re-queried by uuid before task_tag can be written")."""
    tags: dict[str, dict] = {}
    if not data_paths.root.exists():
        return []
    for workspace_dir in sorted(p for p in data_paths.root.iterdir() if p.is_dir()):
        for tag in read_json(workspace_dir / "tags.json", default=[]) or []:
            gid = tag.get("gid")
            if not gid or gid in tags:
                continue
            tags[gid] = {
                "asana_gid": gid,
                "uuid": existing_tag_uuids.get(gid) or str(uuid.uuid4()),
                "name": {"en": tag.get("name") or ""},
                "color": tag.get("color") or "",
            }
    return list(tags.values())


def _build_board(
    data_paths: Paths, workspace_dir: Path, project_gid: str, registry: dict, sections_out: list[dict],
    existing_board_uuids: dict[str, str], existing_section_uuids: dict[str, str],
) -> dict | None:
    project_dir = data_paths.find_project_dir_anywhere(project_gid) or data_paths.project_dir(workspace_dir, project_gid)
    project_json = read_json(project_dir / "project.json", default=None)
    if project_json is None:
        # Listed in a team's projects_index.json but never actually fetched
        # (import still in progress, or the project's own crawl errored out)
        # - nothing to build yet; a re-run after `import-all` finishes picks
        # it up.
        log.warning("Project %s: listed on a team but not yet imported under data/ - skipping for now.", project_gid)
        return None

    members = read_json(project_dir / "members.json", default=[]) or project_json.get("members", []) or []
    created_by_email, source = resolve_owner_email(members, registry)
    if not created_by_email:
        log.warning(
            "Project '%s' (%s): no resolvable creator (no members with a known email) - skipping this board.",
            project_json.get("name"), project_gid,
        )
        return None

    board_uuid = existing_board_uuids.get(project_gid) or str(uuid.uuid4())
    log.info("Board '%s' (%s): created_by=%s (%s)", project_json.get("name"), project_gid, created_by_email, source)

    section_list = read_json(project_dir / "sections.json", default=[]) or []
    for order, sec in enumerate(section_list):
        sections_out.append({
            "asana_gid": sec.get("gid"),
            "uuid": existing_section_uuids.get(sec.get("gid")) or str(uuid.uuid4()),
            "board_uuid": board_uuid,
            "board_asana_gid": project_gid,
            "name": sec.get("name"),
            "order": order,
        })

    return {
        "asana_gid": project_gid,
        "uuid": board_uuid,
        "name": project_json.get("name"),
        "description": project_json.get("notes") or "",
        "privacy": "PRIVATE",
        "created_by_email": created_by_email,
        "_members": members,  # popped by the caller before this dict is written out
    }


def _result(tags, teams, boards, sections, board_teams, board_members, team_members) -> dict:
    return {
        "tags": tags,
        "teams": teams,
        "boards": list(boards.values()),
        "sections": sections,
        "board_teams": board_teams,
        "board_members": board_members,
        "team_members": team_members,
    }


def write_output(build_dir: Path, result: dict) -> None:
    write_json(build_dir / "tags.json", result["tags"])
    write_json(build_dir / "teams.json", result["teams"])
    write_json(build_dir / "boards.json", result["boards"])
    write_json(build_dir / "sections.json", result["sections"])
    write_json(build_dir / "board_teams.json", result["board_teams"])
    write_json(build_dir / "board_members.json", result["board_members"])
    write_json(build_dir / "team_members.json", result["team_members"])
