"""Synthetic padmasana users for Asana users the Firebase Authorization
service doesn't know about.

`import-users` only returns people that exist in Firebase. Asana users with
no match there - "Private User"s (Asana hides their identity), "Deleted
User"s, and real people whose account isn't in the imported workspaces -
would otherwise fall back to raw Asana data with no `user_reference_code`.

This module fabricates a full padmasana user record (same shape
`fetch_workspace_users` returns, plus `workspace` as the dict the Firebase
service itself returns: `{domain, organization_unit, lang}`) for each of
them so they can be created in padmasana and referenced like anyone else:

* "Private User"  (no email)  -> `Private User 1`, `Private User 2`, ...
* "Deleted User"              -> `Deleted User 1`, `Deleted User 2`, ...
* anyone with an email        -> first matched to an existing real user with
                                 the same email local part (other domain alias);
                                 otherwise name derived from it (`jordy.villao@x` ->
                                 `Jordy Villao`), real email kept
* anyone with only a name     -> that name, synthetic email

`user_reference_code` is a deterministic 21-digit number (same length as
the real Google IDs) derived from the Asana gid and prefixed `999` so it's
recognizable and can't collide with a real one in practice. Numbering and
codes are persisted in `build/placeholder_users.json` (keyed by asana_gid),
so re-running never renumbers or re-issues anything.

Results are merged into `build/padmasana_users.json` and written onto the
matching entries of `build/user_to_asana_gid.json`.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from pathlib import Path

from asana_migration.storage import read_json, write_json

from .import_users import write_users

log = logging.getLogger("padmasana_migration.placeholder_users")

PLACEHOLDER_FILE = "placeholder_users.json"

# Static on purpose - not read from config. Verified against the real Firebase
# data: `funiber.org` + `/ctdesarrollo.org` is an existing combination (80
# real users, all `@funiber.org`), with `es` as the preferred language of
# ~92% of all users.
WORKSPACE = {
    "domain": "funiber.org",
    "organization_unit": "/ctdesarrollo.org",
    "lang": [{"languageCode": "es", "preference": "preferred"}],
}
EMAIL_DOMAIN = "funiber.org"
CODE_PREFIX = "999"


def _code_for(gid: str) -> str:
    digits = str(int(hashlib.sha256(f"asana:{gid}".encode()).hexdigest(), 16))
    return (CODE_PREFIX + digits)[:21]


def _slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", ".", text.lower()).strip(".")


def _name_from_email(email: str) -> tuple[str, str]:
    local = email.split("@", 1)[0]
    parts = [p for p in re.split(r"[._\-+\d]+", local) if p]
    parts = [p.capitalize() for p in parts] or [local]
    return parts[0], " ".join(parts[1:])


def _split_name(full: str) -> tuple[str, str]:
    bits = full.split()
    return (bits[0], " ".join(bits[1:])) if bits else ("", "")


def _kind(u: dict) -> str:
    name = (u.get("name") or "").strip().lower()
    email = (u.get("email") or "").strip().lower()
    if name == "deleted user" or email.startswith("deleteduser@"):
        return "deleted"
    if name == "private user" or (not email and not name):
        return "private"
    return "email" if email else "named"


def _real_user_index(users: list[dict]) -> dict[str, list[dict]]:
    """Real (non-placeholder) padmasana users by email local part, so an Asana
    `jordy.villao@ctdesarrollo.org` finds the existing `jordy.villao@funiber.org`
    (same person, other domain alias) instead of getting a duplicate fabricated."""
    idx: dict[str, list[dict]] = {}
    for p in users:
        if p.get("placeholder") or not p.get("email"):
            continue
        idx.setdefault(p["email"].split("@", 1)[0].lower(), []).append(p)
    return idx


def _pick_real(cands: list[dict]) -> dict:
    def rank(p: dict) -> int:
        dom = p["email"].split("@", 1)[1].lower()
        ou = (p.get("workspace") or {}).get("organization_unit")
        return (dom != EMAIL_DOMAIN) * 2 + (ou != WORKSPACE["organization_unit"])
    return min(cands, key=rank)


def create_placeholder_users(build_dir: Path) -> list[dict]:
    map_path = build_dir / "user_to_asana_gid.json"
    user_map = read_json(map_path, default={}) or {}
    if not user_map:
        raise SystemExit(f"{map_path} is empty - run `padmasana-map-users` first.")

    domain = EMAIL_DOMAIN
    ws = WORKSPACE
    store_path = build_dir / PLACEHOLDER_FILE
    store: dict[str, dict] = read_json(store_path, default={}) or {}

    counters = {"private": 0, "deleted": 0}
    for rec in store.values():
        k = rec.get("placeholder_kind")
        if k in counters:
            counters[k] = max(counters[k], rec.get("placeholder_index", 0))

    padmasana_users = read_json(build_dir / "padmasana_users.json", default=[]) or []
    known_emails = {(p.get("email") or "").strip().lower() for p in padmasana_users if p.get("email")}

    real_idx = _real_user_index(padmasana_users)
    matched: list[tuple[str, dict]] = []

    created: list[dict] = []
    for gid, u in user_map.items():
        if u.get("user_reference_code") and gid not in store:
            continue  # already matched to a real padmasana user
        if gid in store:
            created.append(store[gid])
            continue
        kind = _kind(u)
        email = (u.get("email") or "").strip()
        if kind == "email":
            cands = real_idx.get(email.split("@", 1)[0].lower())
            if cands:
                matched.append((gid, _pick_real(cands)))
                continue
        index = 0
        if kind in counters:
            counters[kind] += 1
            index = counters[kind]
            label = "Private User" if kind == "private" else "Deleted User"
            name, last_name = label, str(index)
            email = f"{_slug(label)}.{index}@{domain}"
        elif kind == "email":
            name, last_name = _name_from_email(email)
        else:
            name, last_name = _split_name(u["name"].strip())
            email = f"{_slug(u['name'])}.{gid[-6:]}@{domain}"
        if email.lower() in known_emails:
            log.warning("placeholder email %s already exists in padmasana_users.json - skipping gid %s", email, gid)
            continue
        store[gid] = {
            "user_reference_code": _code_for(gid),
            "email": email,
            "name": name,
            "last_name": last_name,
            "profile_url": None,
            "workspace": ws,
            "asana_gid": gid,
            "placeholder": True,
            "placeholder_kind": kind,
            "placeholder_index": index,
        }
        created.append(store[gid])

    # Fallback actor for rows Asana attributes to nobody (rules, integrations,
    # API imports) where padmasana's column is NOT NULL - task.created_by,
    # comment.author_id, attachment.uploaded_by, task_activity_log.actor_id.
    # Not an Asana user, so it's never in user_to_asana_gid.json.
    from .people import SYSTEM_USER_KEY
    system = store.setdefault(SYSTEM_USER_KEY, {
        "user_reference_code": _code_for("system"),
        "email": f"asana.migration@{domain}",
        "name": "Asana",
        "last_name": "Migration",
        "profile_url": None,
        "workspace": ws,
        "asana_gid": None,
        "placeholder": True,
        "placeholder_kind": "system",
        "placeholder_index": 0,
    })
    if system["email"].lower() in known_emails and not any(
        p.get("user_reference_code") == system["user_reference_code"] for p in padmasana_users
    ):
        raise SystemExit(f"system user email {system['email']} already belongs to a real padmasana user")
    created.append(system)

    write_json(store_path, store)

    # padmasana_users.json entries: same keys the Firebase client returns.
    rows = [{k: r[k] for k in ("user_reference_code", "email", "name", "last_name", "profile_url", "workspace")}
            | {"placeholder": True} for r in created]
    write_users(build_dir, rows, merge=True)

    for r in created:
        entry = user_map.get(r["asana_gid"])
        if not entry:
            continue
        entry["user_reference_code"] = r["user_reference_code"]
        entry["email"] = r["email"]
        entry["padmasana_name"] = f"{r['name']} {r['last_name']}".strip()
        entry["profile_url"] = r["profile_url"]
        entry["workspace"] = r["workspace"]
        entry["status"] = "mapped"
        entry["placeholder"] = True
    for gid, real in matched:
        entry = user_map[gid]
        entry["user_reference_code"] = real["user_reference_code"]
        entry["padmasana_name"] = f"{real.get('name') or ''} {real.get('last_name') or ''}".strip()
        entry["profile_url"] = real.get("profile_url")
        entry["workspace"] = real.get("workspace")
        entry["status"] = "mapped"
        entry["matched_real_email"] = real["email"]
    log.info("%d Asana user(s) matched to an existing user by email local part", len(matched))
    write_json(map_path, user_map)
    return created
