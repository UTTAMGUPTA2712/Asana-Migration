"""Client + build-time validation for padmasana's own identity source.

`padmasana-service` resolves every `email` this package's build/ files
reference into a `workspace_member.user_reference_code` live, at seed time
(DESIGN.md §10) - this package itself never touches that table. But
`user_reference_code` comes from the same place padmasana-service's own
`import-users` CLI command gets it from
(`modules/shared/infrastructure/cli-commands/commands/import-users.
command.ts` -> `ImportUsersOrchestrator` ->
`FirebaseAuthorizationServiceClient.importWorkspaceUsers`): a plain,
unauthenticated `POST {FIREBASE_AUTH_API_URL}/workspace-users/import`
against the Firebase Authorization service, with `{workspace,
organization_unit}` as the body - a trusted service-to-service call, no
token/header involved on either side.

This module makes that exact same call so this package can save the
result to `build/padmasana_users.json` and cross-check every email
build/'s files reference against it - surfacing offline, before a real
seed run, what DESIGN.md §10 says padmasana would otherwise hard-error on
("a hard error, halting the run, if it's missing").
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from asana_migration.storage import read_json, write_json

log = logging.getLogger("padmasana_migration.import_users")

_SKIP_FILES = {"padmasana_users.json", "gid_to_uuid.json"}


class ImportUsersError(Exception):
    pass


def fetch_workspace_users(
    base_url: str, workspace: str, organization_unit: str, timeout: float = 60.0,
) -> list[dict]:
    """`POST {base_url}/workspace-users/import`, no auth (see module
    docstring - the real client sends none either). Returns the same
    normalized shape `ImportUsersOrchestrator` builds from the raw
    response: `{user_reference_code, email, name, last_name, profile_url,
    workspace}`. The raw response is flattened one level first, matching
    the orchestrator's own `firebaseUsers.flat()` (harmless if it was
    already flat)."""
    url = f"{base_url.rstrip('/')}/workspace-users/import"
    resp = requests.post(
        url, json={"workspace": workspace, "organization_unit": organization_unit}, timeout=timeout,
    )
    if resp.status_code not in (200, 201):
        raise ImportUsersError(f"POST {url} -> {resp.status_code}: {resp.text[:500]}")
    raw = resp.json()
    flat: list[dict] = []
    for item in raw:
        flat.extend(item) if isinstance(item, list) else flat.append(item)
    return [
        {
            "user_reference_code": u.get("code_reference"),
            "email": u.get("email"),
            "name": u.get("name"),
            "last_name": u.get("last_name"),
            "profile_url": u.get("profile_photo"),
            "workspace": u.get("workspace"),
        }
        for u in flat
    ]


def write_users(build_dir: Path, users: list[dict], merge: bool = True) -> Path:
    path = build_dir / "padmasana_users.json"
    if merge and path.exists():
        existing = read_json(path, default=[]) or []
        by_ref = {u.get("user_reference_code"): u for u in existing if u.get("user_reference_code")}
        by_email = {u.get("email").strip().lower(): u for u in existing if u.get("email")}
        for u in users:
            ref = u.get("user_reference_code")
            em = (u.get("email") or "").strip().lower()
            if ref and ref in by_ref:
                by_ref[ref].update(u)
            elif em and em in by_email:
                by_email[em].update(u)
            else:
                existing.append(u)
                if ref:
                    by_ref[ref] = u
                if em:
                    by_email[em] = u
        final_users = existing
    else:
        final_users = users

    write_json(path, final_users)
    return path


def report_missing_users(build_dir: Path, users: list[dict] | None = None) -> list[dict]:
    """Cross-checks all Asana users in user_to_asana_gid.json against padmasana_users.json
    and returns the list of Asana users that still lack Padmasana data, logging clear warnings."""
    if users is None:
        users_path = build_dir / "padmasana_users.json"
        if not users_path.exists():
            log.warning(
                "=== %s does not exist - `padmasana-import-users` has never been run ===", users_path,
            )
            log.warning(
                "Every mention/assignee/collaborator below will fall back to raw Asana data (no real "
                "user_reference_code) until you run `padmasana-import-users --firebase-auth-api-url <url> "
                "--workspace <ws> --organization-unit <ou>` first."
            )
        users = read_json(users_path, default=[]) or []
    known_emails = {u["email"].strip().lower() for u in users if u.get("email")}
    known_refs = {u["user_reference_code"] for u in users if u.get("user_reference_code")}

    user_map = read_json(build_dir / "user_to_asana_gid.json", default={}) or {}
    missing = []
    for gid, u in user_map.items():
        ref = u.get("user_reference_code")
        em = (u.get("email") or "").strip().lower()
        if (ref and ref in known_refs) or (em and em in known_emails):
            continue
        missing.append(u)

    if missing:
        log.warning("=== %d Asana user(s) still have no data in padmasana_users.json ===", len(missing))
        for m in missing[:20]:
            log.warning("  - %s (email: '%s', asana_gid: %s)", m.get("name") or "Unknown", m.get("email") or "", m.get("asana_gid"))
        if len(missing) > 20:
            log.warning("  ... and %d more (see %s/user_to_asana_gid.json)", len(missing) - 20, build_dir)
        log.warning("Run `padmasana-import-users --workspace <workspace> --organization-unit <ou>` to import them.")
    else:
        log.info("All %d Asana user(s) have matching Padmasana user data.", len(user_map))

    return missing


def _collect_referenced_emails(build_dir: Path) -> dict[str, list[str]]:
    """Every value under an `email`/`*_email` key in every flat
    `build/*.json` file (the per-task `build/tasks/*/` tree is already
    folded into these by `compile_build` - no need to walk it separately),
    mapped back to which file(s) it came from, so a missing-email warning
    says where to go look. Skips this module's own output and
    `gid_to_uuid.json` - neither carries emails worth checking."""
    emails: dict[str, list[str]] = {}
    for path in sorted(build_dir.glob("*.json")):
        if path.name in _SKIP_FILES:
            continue
        data = read_json(path, default=None)
        if data is None:
            continue
        rows = data if isinstance(data, list) else [data]
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key, value in row.items():
                if not value or not isinstance(value, str):
                    continue
                if key == "email" or key.endswith("_email"):
                    emails.setdefault(value, [])
                    if path.name not in emails[value]:
                        emails[value].append(path.name)
    return emails


def validate_emails(build_dir: Path, users: list[dict]) -> list[str]:
    """Cross-checks every email build/'s flat files reference against the
    fetched padmasana user list. Returns the sorted list of emails with no
    matching padmasana user."""
    known = {u["email"].strip().lower() for u in users if u.get("email")}
    referenced = _collect_referenced_emails(build_dir)
    missing = sorted(email for email in referenced if email.strip().lower() not in known)
    for email in missing:
        log.warning(
            "email %s (referenced in %s) has no matching padmasana user",
            email, ", ".join(referenced[email]),
        )
    return missing

