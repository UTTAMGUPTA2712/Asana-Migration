"""Transforms Asana HTML in task descriptions and comments to Padmasana Tiptap HTML.

Converts:
1. Mentions:
   <a data-asana-type="user" data-asana-gid="<gid>">@Name</a> ->
   <span class="mention" data-type="mention" data-id="<user_ref_code>" data-label="<Name>"
         data-profile-url="<url>" data-email="<email>">...</span>
2. Attachments & inline images:
   <img data-asana-type="attachment" data-asana-gid="<gid>"> ->
   <img src="<file_service_url>/files/<file_uuid>/download" alt="<name>" ...>
   Asset URLs (app.asana.com/app/asana/-/get_asset?asset_id=<gid>) -> download URLs
3. Links to Asana boards and tasks:
   app.asana.com/0/<board_gid>/board -> <app_url>/my-boards/<board_uuid>
   app.asana.com/0/<board_gid>/<task_gid> -> <app_url>/my-boards/<board_uuid>?taskId=<task_uuid>
4. Strips outer <body>...</body> tags and formats linebreaks for Tiptap editor.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from asana_migration.storage import Paths, read_json, write_json

from . import config
from .people import build_user_map, load_user_map

log = logging.getLogger("padmasana_migration.format_docs")

# Regex patterns for matching Asana HTML elements
USER_MENTION_RE = re.compile(
    r'<a\b(?=[^>]*\bdata-asana-type=["\']user["\'])(?=[^>]*\bdata-asana-gid=["\'](\d+)["\'])[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
USER_MENTION_PROFILE_RE = re.compile(
    r'<a\b(?=[^>]*\bdata-asana-type=["\']user["\'])(?=[^>]*\bhref=["\'][^"\']*/profile/(\d+)["\'])[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
# A handful of older mentions in this export carry no `data-asana-*`
# attributes at all - just a bare `<a href=".../profile/<gid>">Name</a>`.
# That href's gid is not trustworthy (it doesn't match the person's real
# Asana gid elsewhere in the export - unlike `data-asana-gid` above), so
# this is resolved by exact name match against the user map instead
# (`FormatContext.user_by_name`), not by gid.
BARE_PROFILE_MENTION_RE = re.compile(
    r'<a\b(?![^>]*\bdata-asana-type=)(?=[^>]*\bhref=["\'][^"\']*/profile/\d+["\'])[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
IMG_ATTACHMENT_RE = re.compile(
    r'<img\b(?=[^>]*\bdata-asana-type=["\']attachment["\'])(?=[^>]*\bdata-asana-gid=["\'](\d+)["\'])[^>]*>',
    re.IGNORECASE,
)
# Asana's own inline `<img>` tags carry the real pixel size as
# `data-src-width`/`data-src-height`. padmasana-app's own image upload flow
# (editor.tsx's `buildImagePlaceholderStyle`) sizes a freshly-inserted image
# the same way - `width`/`height` attrs plus a matching
# `aspect-ratio: W / H; max-width: 100%; width: Wpx; height: auto;` style -
# so migrated images are reproduced with that same markup rather than a
# generic fixed style, whenever the source dimensions are available.
IMG_WIDTH_RE = re.compile(r'data-src-width=["\'](\d+)["\']', re.IGNORECASE)
IMG_HEIGHT_RE = re.compile(r'data-src-height=["\'](\d+)["\']', re.IGNORECASE)
OBJECT_ATTACHMENT_RE = re.compile(
    r'<object\b(?=[^>]*\bdata-asana-type=["\']attachment["\'])(?=[^>]*\bdata-asana-gid=["\'](\d+)["\'])[^>]*>(.*?)</object>',
    re.IGNORECASE | re.DOTALL,
)
ASSET_URL_RE = re.compile(
    r'https?://app\.asana\.com/app/asana/-/get_asset\?asset_id=(\d+)',
    re.IGNORECASE,
)
ASANA_TASK_LINK_RE = re.compile(
    r'https?://app\.asana\.com/0/(\d+)/(\d+)(?:/[a-z0-9_-]+)?',
    re.IGNORECASE,
)
# Asana's other task-permalink shape (`task.json`'s own `permalink_url`, and
# the one users actually get from "Copy link"): /1/<workspace_gid>/
# [project/<project_gid>/]task/<task_gid>[?focus=true]. Same target as
# ASANA_TASK_LINK_RE above, just a different URL shape - no project/board
# gid worth capturing here, `format_task_link` already falls back to
# `task_board_by_task_uuid` when none is given.
ASANA_TASK_PERMALINK_RE = re.compile(
    r'https?://app\.asana\.com/1/\d+/(?:project/\d+/)?task/(\d+)(?:\?[^\s"\'<>]*)?',
    re.IGNORECASE,
)
ASANA_BOARD_LINK_RE = re.compile(
    r'https?://app\.asana\.com/0/(\d+)/(?:board|list)',
    re.IGNORECASE,
)
ASANA_GENERIC_LINK_RE = re.compile(
    r'https?://app\.asana\.com/0/(\d+)/?',
    re.IGNORECASE,
)


@dataclass
class FormatContext:
    app_url: str
    file_service_url: str
    user_map: dict[str, dict]  # asana_gid -> user dict
    user_by_name: dict[str, dict]  # lowercased asana name -> user dict
    board_by_asana_gid: dict[str, dict]  # asana_gid -> board dict
    task_uuid_by_asana_gid: dict[str, str]  # asana_gid -> task_uuid
    task_board_by_task_uuid: dict[str, str]  # task_uuid -> board_uuid
    attachment_by_asana_gid: dict[str, dict]  # asana_gid -> {name, uuid, download_url}


def load_format_context(
    build_dir: Path,
    app_url: str,
    file_service_url: str,
    data_paths: Paths | None = None,
) -> FormatContext:
    app_url = app_url.rstrip("/")
    file_service_url = file_service_url.rstrip("/")

    # 1. User map
    user_map = load_user_map(build_dir)
    if not user_map:
        if data_paths is None:
            data_paths = Paths(root=config.DATA_DIR)
        user_map = build_user_map(data_paths, build_dir)

    user_by_name: dict[str, dict] = {}
    for user in user_map.values():
        name = (user.get("name") or "").strip().lower()
        if name and name not in user_by_name:
            user_by_name[name] = user

    # 2. Boards
    boards_raw = read_json(build_dir / "boards.json", default=[]) or []
    board_by_asana_gid = {b["asana_gid"]: b for b in boards_raw if b.get("asana_gid")}

    # 3. Tasks gid -> uuid
    gid_to_uuid = read_json(build_dir / "gid_to_uuid.json", default={}) or {}

    # 4. Task board mappings from board_sections / section_tasks
    task_board_by_task_uuid: dict[str, str] = {}
    sections_raw = read_json(build_dir / "sections.json", default=[]) or []
    board_by_section_uuid = {s["uuid"]: s["board_uuid"] for s in sections_raw if s.get("uuid") and s.get("board_uuid")}

    section_tasks = read_json(build_dir / "section_tasks.json", default=[]) or []
    for st in section_tasks:
        tu = st.get("task_uuid")
        su = st.get("board_section_uuid")
        if tu and su and su in board_by_section_uuid and tu not in task_board_by_task_uuid:
            task_board_by_task_uuid[tu] = board_by_section_uuid[su]

    # 5. Attachments
    attachment_by_asana_gid: dict[str, dict] = {}
    for filename in ("attachments.json", "comment_attachments.json"):
        for att in read_json(build_dir / filename, default=[]) or []:
            gid = att.get("asana_gid")
            if not gid:
                continue
            meta = att.get("metadata") or {}
            file_uuid = meta.get("uuid")
            name = att.get("name") or meta.get("name") or "attachment"
            if file_uuid:
                download_url = f"{file_service_url}/files/{file_uuid}/download"
            else:
                # Link-only (gdrive/external) attachments have no file-service
                # uuid - their url lives under metadata (upload_attachments.py's
                # `_link_only_record`), not on the attachment entry itself.
                download_url = meta.get("url") or ""
            attachment_by_asana_gid[gid] = {
                "name": name,
                "uuid": file_uuid,
                "download_url": download_url,
            }

    return FormatContext(
        app_url=app_url,
        file_service_url=file_service_url,
        user_map=user_map,
        user_by_name=user_by_name,
        board_by_asana_gid=board_by_asana_gid,
        task_uuid_by_asana_gid=gid_to_uuid,
        task_board_by_task_uuid=task_board_by_task_uuid,
        attachment_by_asana_gid=attachment_by_asana_gid,
    )


def format_mention(user_gid: str | None, raw_label: str, ctx: FormatContext, user: dict | None = None) -> str:
    if user is None:
        user = ctx.user_map.get(user_gid) or {}
    clean_label = raw_label.lstrip("@").strip()

    user_id = user.get("user_reference_code") or user.get("asana_gid") or user_gid or ""
    label = user.get("padmasana_name") or user.get("name") or clean_label
    profile_url = user.get("profile_url") or ""
    email = user.get("email") or ""

    if profile_url:
        avatar_html = f'<img src="{profile_url}" class="mention-avatar-img" />'
    else:
        first_letter = label[0].upper() if label else "U"
        avatar_html = f'<span class="mention-avatar-text">{first_letter}</span>'

    return (
        f'<span class="mention" data-type="mention" data-id="{user_id}" '
        f'data-label="{label}" data-profile-url="{profile_url}" data-email="{email}">'
        f'<span class="mention-avatar">{avatar_html}</span>'
        f'<span style="display: none">&nbsp;</span>'
        f'<span class="mention-label">{label}</span>'
        f'</span>'
    )


def format_task_link(board_gid: str | None, task_gid: str, ctx: FormatContext) -> str:
    task_uuid = ctx.task_uuid_by_asana_gid.get(task_gid)
    if not task_uuid:
        # Not in this export (e.g. a "[Private Link]" to a task outside the
        # migrated workspace) - nothing to rewrite to, so fall back to a
        # valid Asana link rather than a malformed one when there's no board.
        if board_gid:
            return f"https://app.asana.com/0/{board_gid}/{task_gid}"
        return f"https://app.asana.com/0/{task_gid}"

    board_uuid = None
    if board_gid and board_gid in ctx.board_by_asana_gid:
        board_uuid = ctx.board_by_asana_gid[board_gid]["uuid"]
    elif task_uuid in ctx.task_board_by_task_uuid:
        board_uuid = ctx.task_board_by_task_uuid[task_uuid]

    if board_uuid:
        return f"{ctx.app_url}/my-boards/{board_uuid}?taskId={task_uuid}"
    return f"{ctx.app_url}/my-tasks?taskId={task_uuid}"


def format_board_link(board_gid: str, ctx: FormatContext) -> str:
    board = ctx.board_by_asana_gid.get(board_gid)
    if board and board.get("uuid"):
        return f"{ctx.app_url}/my-boards/{board['uuid']}"
    return f"https://app.asana.com/0/{board_gid}/board"


def format_html_content(raw_html: str, ctx: FormatContext) -> str:
    """Formats raw Asana HTML into Padmasana Tiptap HTML."""
    if not raw_html or not raw_html.strip():
        return ""

    content = raw_html

    # Strip outer <body> tags
    content = re.sub(r"^\s*<body>", "", content, flags=re.IGNORECASE)
    content = re.sub(r"</body>\s*$", "", content, flags=re.IGNORECASE)

    # 1. User Mentions: by data-asana-gid
    def _sub_mention(match: re.Match) -> str:
        user_gid = match.group(1)
        raw_label = match.group(2)
        return format_mention(user_gid, raw_label, ctx)

    content = USER_MENTION_RE.sub(_sub_mention, content)

    # 1b. User Mentions: by /profile/<gid> in href if not yet matched
    def _sub_mention_profile(match: re.Match) -> str:
        user_gid = match.group(1)
        raw_label = match.group(2)
        return format_mention(user_gid, raw_label, ctx)

    content = USER_MENTION_PROFILE_RE.sub(_sub_mention_profile, content)

    # 1c. Bare `<a href=".../profile/<gid>">Name</a>` mentions with no
    # `data-asana-*` attributes at all (older export rows) - resolved by
    # exact name match, since that href's gid is not trustworthy (see
    # BARE_PROFILE_MENTION_RE's docstring above).
    def _sub_bare_profile_mention(match: re.Match) -> str:
        raw_label = match.group(1)
        name_key = re.sub(r"<[^>]+>", "", raw_label).strip().lower()
        user = ctx.user_by_name.get(name_key)
        if not user:
            return match.group(0)
        return format_mention(None, raw_label, ctx, user=user)

    content = BARE_PROFILE_MENTION_RE.sub(_sub_bare_profile_mention, content)

    # 2. Inline images
    def _sub_img_att(match: re.Match) -> str:
        att_gid = match.group(1)
        att = ctx.attachment_by_asana_gid.get(att_gid)
        if not att or not att.get("download_url"):
            return match.group(0)
        name = att.get("name") or "image"
        tag = match.group(0)
        w_match = IMG_WIDTH_RE.search(tag)
        h_match = IMG_HEIGHT_RE.search(tag)
        if w_match and h_match:
            w, h = w_match.group(1), h_match.group(1)
            style = f"aspect-ratio: {w} / {h}; max-width: 100%; width: {w}px; height: auto;"
            return f'<img src="{att["download_url"]}" alt="{name}" width="{w}" height="{h}" style="{style}" />'
        return f'<img src="{att["download_url"]}" alt="{name}" style="display:block;max-width:100%;margin-left:auto;margin-right:auto" />'

    content = IMG_ATTACHMENT_RE.sub(_sub_img_att, content)

    # 2b. External media object
    def _sub_object_att(match: re.Match) -> str:
        att_gid = match.group(1)
        inner = match.group(2)
        att = ctx.attachment_by_asana_gid.get(att_gid)
        if att and att.get("download_url"):
            name = att.get("name") or "attachment"
            return f'<a href="{att["download_url"]}" target="_blank" rel="noopener noreferrer">{name}</a>'
        return inner

    content = OBJECT_ATTACHMENT_RE.sub(_sub_object_att, content)

    # 2c. Asset URLs (get_asset?asset_id=...)
    def _sub_asset_url(match: re.Match) -> str:
        att_gid = match.group(1)
        att = ctx.attachment_by_asana_gid.get(att_gid)
        if att and att.get("download_url"):
            return att["download_url"]
        return match.group(0)

    content = ASSET_URL_RE.sub(_sub_asset_url, content)

    # 3. Asana Task Links (app.asana.com/0/<board_gid>/<task_gid>)
    def _sub_task_link(match: re.Match) -> str:
        board_gid = match.group(1)
        task_gid = match.group(2)
        return format_task_link(board_gid, task_gid, ctx)

    content = ASANA_TASK_LINK_RE.sub(_sub_task_link, content)

    # 3a2. Asana Task Permalinks (app.asana.com/1/<ws>/[project/<p>/]task/<gid>)
    def _sub_task_permalink(match: re.Match) -> str:
        task_gid = match.group(1)
        return format_task_link(None, task_gid, ctx)

    content = ASANA_TASK_PERMALINK_RE.sub(_sub_task_permalink, content)

    # 3b. Asana Board Links (app.asana.com/0/<board_gid>/board or /list)
    def _sub_board_link(match: re.Match) -> str:
        board_gid = match.group(1)
        return format_board_link(board_gid, ctx)

    content = ASANA_BOARD_LINK_RE.sub(_sub_board_link, content)

    # 3c. Generic Asana links: app.asana.com/0/<gid>
    def _sub_generic_link(match: re.Match) -> str:
        gid = match.group(1)
        if gid in ctx.task_uuid_by_asana_gid:
            return format_task_link(None, gid, ctx)
        if gid in ctx.board_by_asana_gid:
            return format_board_link(gid, ctx)
        return match.group(0)

    content = ASANA_GENERIC_LINK_RE.sub(_sub_generic_link, content)

    # 4. Clean up any remaining raw data-asana attributes on standard tags
    content = re.sub(r'\s+data-asana-[a-z0-9_-]+="[^"]*"', '', content)

    # 5. Format newlines into <br /> if content is not wrapped in block elements
    lines = content.splitlines()
    if lines and not any(tag in content.lower() for tag in ("<p>", "<div>", "<ul>", "<ol>", "<h1>", "<h2>", "<h3>", "<blockquote>")):
        content = "<p>" + "<br />".join(line for line in lines if line.strip()) + "</p>"

    return content.strip()


def format_all_tasks(
    build_dir: Path,
    ctx: FormatContext,
    task_gid_filter: str | None = None,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Formats HTML in task.json and comments.json for every task in build/tasks/*/
    and updates flat build files."""
    tasks_dir = build_dir / "tasks"
    if not tasks_dir.exists():
        log.warning("No tasks directory found at %s", tasks_dir)
        return 0, 0

    total_tasks = 0
    total_comments = 0

    task_dirs = [tasks_dir / task_gid_filter] if task_gid_filter else sorted(tasks_dir.iterdir())

    all_tasks_updated = []
    all_comments_updated = []

    for task_dir in task_dirs:
        if not task_dir.is_dir():
            continue
        task_json_path = task_dir / "task.json"
        task = read_json(task_json_path, default=None)
        if not task:
            continue

        total_tasks += 1
        raw_desc = task.get("description_html") or task.get("description") or ""
        formatted_desc = format_html_content(raw_desc, ctx)

        if not dry_run:
            task["description"] = formatted_desc
            task["description_html"] = formatted_desc
            write_json(task_json_path, task)
        all_tasks_updated.append(task)

        # Comments
        comments_json_path = task_dir / "comments.json"
        comments = read_json(comments_json_path, default=[]) or []
        for c in comments:
            total_comments += 1
            raw_c_html = c.get("html_text") or c.get("text") or ""
            formatted_c = format_html_content(raw_c_html, ctx)
            c["html_text"] = formatted_c
            c["body"] = formatted_c

        if not dry_run:
            write_json(comments_json_path, comments)
        all_comments_updated.extend(comments)

    if not dry_run and not task_gid_filter:
        # Update flat files
        flat_tasks_path = build_dir / "tasks.json"
        if flat_tasks_path.exists():
            write_json(flat_tasks_path, all_tasks_updated)
        flat_comments_path = build_dir / "comments.json"
        if flat_comments_path.exists():
            write_json(flat_comments_path, all_comments_updated)

    return total_tasks, total_comments
