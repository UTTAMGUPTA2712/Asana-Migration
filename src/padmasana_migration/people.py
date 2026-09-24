"""Everything about resolving *who* an Asana person is, from the export.

No script in this package writes or resolves padmasana identity rows - that
happens live, at seed time, against padmasana's own database (DESIGN.md
§5.3, §10). Every reference this package writes is just the person's Asana
`email`, carried straight through.

Two things still need doing entirely offline, from the export alone:

- **Picking a team/board's owner/creator** (DESIGN.md §2, §5.4) - Asana
  names none, so the rule is the lowest-gid member, overridable per team.
- **Resolving the *target* person of a `assigned`/`unassigned` story**
  (DESIGN.md §5.8) - Asana's Stories API gives no structured field for this,
  only an inline profile link buried in `html_text`. Recovering an email
  from that link's gid means already knowing every person's gid -> email in
  this workspace, which is what `build_person_registry` below collects, once,
  by scanning every place the export already carries a full `{gid, name,
  email}` person record.
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from pathlib import Path

from asana_migration.storage import Paths, read_json, write_json

log = logging.getLogger("padmasana_migration.people")

PROFILE_GID_RE = re.compile(r'/profile/(\d+)"')
USER_MENTION_RE = re.compile(r'<a\b[^>]*\bdata-asana-type=["\']user["\'][^>]*\bdata-asana-gid=["\'](\d+)["\'][^>]*>(.*?)</a>')
USER_MENTION_ALT_RE = re.compile(r'<a\b[^>]*\bdata-asana-gid=["\'](\d+)["\'][^>]*\bdata-asana-type=["\']user["\'][^>]*>(.*?)</a>')
ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S)
HREF_PROFILE_RE = re.compile(r'\bhref=["\'][^"\']*/profile/(\d+)')
DATA_GID_RE = re.compile(r'\bdata-asana-gid=["\'](\d+)["\']')
DATA_TYPE_USER_RE = re.compile(r'\bdata-asana-type=["\']user["\']')
TAG_RE = re.compile(r"<[^>]+>")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Asana's placeholder for people it no longer lets us see - never a real
# identity, so never used to match two records to each other by name.
PLACEHOLDER_NAMES = {"private user"}


class PersonRegistry(dict):
    """gid -> {"email", "name", "source"}, plus `by_profile`: Asana profile
    id (the number in `app.asana.com/0/profile/<id>` links) -> user gid.
    The two are different ids for the same person - a rich-text mention
    carries both (`href` + `data-asana-gid`), but assigned/unassigned
    stories only carry the profile link, so this index is what turns one
    back into the other."""

    def __init__(self) -> None:
        super().__init__()
        self.by_profile: dict[str, str] = {}


def _anchor_text(raw: str) -> str:
    return html_lib.unescape(TAG_RE.sub("", raw)).lstrip("@").strip()


def build_person_registry(data_paths: Paths) -> PersonRegistry:
    """gid -> {"email", "name", "source"} for every person this export ever
    names: first everyone with a full record (team/project members and
    followers, task assignees/followers/collaborators, every comment's/
    story's `created_by`), then everyone who only shows up as a user link
    inside rich text (mentions in notes/comments, the target of an
    assigned/unassigned story). Read-only, built once up front and never
    mutated again - safe to share across every worker thread without
    locking."""
    registry = PersonRegistry()
    # profile id -> every anchor text seen for it, for links that carry no
    # user gid (resolved after the scan, once every full record is known)
    profile_texts: dict[str, set[str]] = {}

    def add(person: dict | None, source: str = "record") -> None:
        if not person or not person.get("gid"):
            return
        gid = person["gid"]
        existing = registry.get(gid)
        if existing is None:
            registry[gid] = {"email": person.get("email"), "name": person.get("name"), "source": source}
        else:
            if source == "record":
                existing["source"] = "record"
            if not existing.get("email") and person.get("email"):
                existing["email"] = person["email"]
            if not existing.get("name") and person.get("name"):
                existing["name"] = person["name"]

    def scan_html(html: str | None) -> None:
        if not html or "<a" not in html:
            return
        for attrs, inner in ANCHOR_RE.findall(html):
            profile = HREF_PROFILE_RE.search(attrs)
            gid = DATA_GID_RE.search(attrs) if DATA_TYPE_USER_RE.search(attrs) else None
            text = _anchor_text(inner)
            if gid:
                add({"gid": gid.group(1), "name": text or None}, source="mention")
                if profile:
                    registry.by_profile[profile.group(1)] = gid.group(1)
            elif profile:
                profile_texts.setdefault(profile.group(1), set()).add(text)

    if not data_paths.root.exists():
        return registry

    for workspace_dir in data_paths.root.iterdir():
        if not workspace_dir.is_dir():
            continue

        teams_dir = workspace_dir / "teams"
        if teams_dir.exists():
            for team_dir in teams_dir.iterdir():
                for member in read_json(team_dir / "members.json", default=[]) or []:
                    add(member)

        projects_dir = workspace_dir / "projects"
        if projects_dir.exists():
            for project_dir in projects_dir.iterdir():
                project = read_json(project_dir / "project.json", default={}) or {}
                add(project.get("owner"))
                for member in project.get("members", []) or []:
                    add(member)
                for follower in project.get("followers", []) or []:
                    add(follower)
                scan_html(project.get("html_notes"))
                for member in read_json(project_dir / "members.json", default=[]) or []:
                    add(member)

        tasks_dir = workspace_dir / "tasks"
        if tasks_dir.exists():
            for task_dir in tasks_dir.iterdir():
                task = read_json(task_dir / "task.json", default={}) or {}
                add(task.get("assignee"))
                for follower in task.get("followers", []) or []:
                    add(follower)
                scan_html(task.get("html_notes"))
                for collaborator in read_json(task_dir / "collaborators.json", default=[]) or []:
                    add(collaborator)
                for comment in read_json(task_dir / "comments.json", default=[]) or []:
                    add(comment.get("created_by"))
                    scan_html(comment.get("html_text"))
                for story in read_json(task_dir / "stories.json", default=[]) or []:
                    add(story.get("created_by"))
                    scan_html(story.get("html_text"))

    _resolve_bare_profile_links(registry, profile_texts)
    return registry


def _resolve_bare_profile_links(registry: PersonRegistry, profile_texts: dict[str, set[str]]) -> None:
    """A profile link with no `data-asana-gid` next to it (assigned/
    unassigned stories) names its person only by display text. Tie it to an
    already-known person when that text is an email or a name belonging to
    exactly one of them (several: left unresolved); matching nobody, it's
    someone this export has no other record of, so they get their own entry
    keyed by the profile id."""
    by_email: dict[str, set[str]] = {}
    by_name: dict[str, set[str]] = {}
    for gid, person in registry.items():
        if person.get("email"):
            by_email.setdefault(person["email"].strip().lower(), set()).add(gid)
        name = (person.get("name") or "").strip().lower()
        if name and name not in PLACEHOLDER_NAMES:
            by_name.setdefault(name, set()).add(gid)

    for profile, texts in sorted(profile_texts.items()):
        if profile in registry.by_profile:
            continue
        texts = {t for t in texts if t and "/profile/" not in t}
        emails = {t.lower() for t in texts if EMAIL_RE.match(t)}
        names = {t.lower() for t in texts - emails if t.lower() not in PLACEHOLDER_NAMES}

        candidates: set[str] = set()
        for e in emails:
            candidates |= by_email.get(e, set())
        if not candidates:
            for n in names:
                candidates |= by_name.get(n, set())
        if len(candidates) == 1:
            registry.by_profile[profile] = next(iter(candidates))
            continue
        if candidates:
            # Same name on several known people - can't tell which, and it's
            # almost certainly one of them, not someone new.
            continue

        name = next(iter(sorted(t for t in texts if not EMAIL_RE.match(t))), None) or next(iter(sorted(texts)), None)
        registry[profile] = {
            "email": next(iter(sorted(emails)), None),
            "name": name,
            "source": "profile_link",
        }
        registry.by_profile[profile] = profile


def email_of(person: dict | None) -> str | None:
    return (person or {}).get("email") or None


def lowest_gid_person(people: list[dict]) -> dict | None:
    """The stable, deterministic default for "no owner named" (DESIGN.md
    §2): the lowest-gid person among a team/project's own members. Asana
    gids are numeric strings, so this compares numerically, not
    lexicographically."""
    with_gid = [p for p in people if p.get("gid")]
    if not with_gid:
        return None
    return min(with_gid, key=lambda p: int(p["gid"]))


def resolve_owner_email(
    members: list[dict],
    registry: dict[str, dict],
    override_user_gid: str | None = None,
) -> tuple[str | None, str | None]:
    """Returns (email, source) where `source` is "override", "lowest_gid" or
    None (nobody resolvable at all - the caller has to decide how to handle
    that, e.g. skip the team/board and log a hard warning, since
    `owner_id`/`created_by` is NOT NULL on the padmasana side)."""
    if override_user_gid:
        person = registry.get(override_user_gid)
        if person and person.get("email"):
            return person["email"], "override"
    lowest = lowest_gid_person(members)
    if lowest and lowest.get("email"):
        return lowest["email"], "lowest_gid"
    if lowest and lowest.get("gid"):
        # Member record on hand has no email (e.g. only gid/name was ever
        # captured) - last resort, check the global registry for the same gid.
        person = registry.get(lowest["gid"])
        if person and person.get("email"):
            return person["email"], "lowest_gid"
    return None, None


def resolve_profile_link_email(html_text: str | None, registry: dict[str, dict]) -> tuple[str | None, str | None]:
    """For an `assigned`/`unassigned` story: the *target* person (the one
    being assigned/unassigned) is the last inline profile link in
    `html_text` (the first is always the actor, same person as the story's
    own `created_by`). Returns (email, gid) - email is None if that gid
    never showed up anywhere else in the export with a real email attached,
    in which case the caller falls back to the plain name in the story's
    `text` (DESIGN.md §5.8 - best-effort, same spirit as the renamed/
    description-updated caveat)."""
    gids = PROFILE_GID_RE.findall(html_text or "")
    if not gids:
        return None, None
    target_gid = getattr(registry, "by_profile", {}).get(gids[-1], gids[-1])
    person = registry.get(target_gid)
    return (person.get("email") if person else None), target_gid


def load_user_map(build_dir: Path) -> dict[str, dict]:
    """Loads build/user_to_asana_gid.json if present, returning a dict of asana_gid -> user dict."""
    path = build_dir / "user_to_asana_gid.json"
    return read_json(path, default={}) or {}


def build_user_map(
    data_paths: Paths,
    build_dir: Path,
    out_path: Path | None = None,
) -> dict[str, dict]:
    """Scans all Asana users across export and comments/mentions, cross-references
    with padmasana_users.json, preserves any existing manual overrides, and saves
    to build/user_to_asana_gid.json."""
    if out_path is None:
        out_path = build_dir / "user_to_asana_gid.json"

    registry = build_person_registry(data_paths)

    # Also scan comments for mentioned user gids
    comments_file = build_dir / "comments.json"
    if comments_file.exists():
        for c in read_json(comments_file, default=[]) or []:
            html = c.get("html_text") or ""
            for m in USER_MENTION_RE.finditer(html):
                gid, raw_name = m.group(1), m.group(2).lstrip("@").strip()
                if gid not in registry:
                    registry[gid] = {"email": None, "name": raw_name}
            for m in USER_MENTION_ALT_RE.finditer(html):
                gid, raw_name = m.group(1), m.group(2).lstrip("@").strip()
                if gid not in registry:
                    registry[gid] = {"email": None, "name": raw_name}

    padmasana_users = read_json(build_dir / "padmasana_users.json", default=[]) or []
    users_by_email: dict[str, dict] = {}
    users_by_name: dict[str, dict] = {}
    for pu in padmasana_users:
        if pu.get("email"):
            users_by_email[pu["email"].strip().lower()] = pu
        full_name = f"{pu.get('name') or ''} {pu.get('last_name') or ''}".strip().lower()
        if full_name:
            users_by_name[full_name] = pu

    existing_map = read_json(out_path, default={}) or {}

    result_map: dict[str, dict] = {}
    for gid, person in sorted(registry.items(), key=lambda item: int(item[0]) if item[0].isdigit() else item[0]):
        email = (person.get("email") or "").strip()
        name = (person.get("name") or "").strip()
        existing = existing_map.get(gid) or {}

        matched_pu = None
        if email and email.lower() in users_by_email:
            matched_pu = users_by_email[email.lower()]
        elif name and name.lower() in users_by_name:
            matched_pu = users_by_name[name.lower()]

        user_ref = existing.get("user_reference_code") or (matched_pu.get("user_reference_code") if matched_pu else None)
        profile_url = existing.get("profile_url") or (matched_pu.get("profile_url") if matched_pu else None)
        pad_name = existing.get("padmasana_name") or (
            f"{matched_pu.get('name') or ''} {matched_pu.get('last_name') or ''}".strip() if matched_pu else None
        )
        workspace = existing.get("workspace") or (matched_pu.get("workspace") if matched_pu else None)

        status = "mapped" if user_ref else "missing_padmasana_user"

        result_map[gid] = {
            "asana_gid": gid,
            "name": name,
            "email": email or (matched_pu.get("email") if matched_pu else "") or existing.get("email") or "",
            "user_reference_code": user_ref,
            "padmasana_name": pad_name or name,
            "profile_url": profile_url,
            "workspace": workspace,
            "source": person.get("source") or "record",
            "status": status,
        }

    write_json(out_path, result_map)
    mapped_count = sum(1 for u in result_map.values() if u["status"] == "mapped")
    missing_count = len(result_map) - mapped_count
    log.info(
        "User map written to %s (%d total Asana users, %d mapped, %d missing in padmasana_users.json)",
        out_path, len(result_map), mapped_count, missing_count,
    )
    return result_map
