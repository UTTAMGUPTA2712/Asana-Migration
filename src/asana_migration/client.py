"""Thin, rate-limited, paginating wrapper around the Asana REST API.

Every method here issues at most one HTTP request per call (pagination is a
generator so callers can fetch one page at a time and yield control back to
the job queue between pages) and every request passes through the shared
``RateLimiter`` plus retry/backoff handling for 429 and transient 5xx errors.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Generator, Iterable

import requests

from .rate_limiter import RateLimiter

BASE_URL = "https://app.asana.com/api/1.0"
log = logging.getLogger("asana_migration.client")


class AsanaAuthError(Exception):
    pass


class AsanaApiError(Exception):
    pass


class AsanaClient:
    def __init__(self, token: str, rate_limiter: RateLimiter, timeout: float = 30.0):
        self.token = token
        self.rate_limiter = rate_limiter
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def _request(self, method: str, path: str, params: dict | None = None, max_retries: int = 6) -> dict:
        url = path if path.startswith("http") else f"{BASE_URL}{path}"
        attempt = 0
        while True:
            self.rate_limiter.acquire()
            log.debug("%s %s params=%s", method, path, params)
            try:
                resp = self.session.request(method, url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                attempt += 1
                if attempt > max_retries:
                    raise AsanaApiError(f"network error calling {url}: {exc}") from exc
                log.warning("network error calling %s (attempt %d/%d): %s", path, attempt, max_retries, exc)
                time.sleep(min(2**attempt, 30))
                continue

            if resp.status_code == 401:
                raise AsanaAuthError("Asana rejected the personal access token (401).")
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 15))
                log.info("Asana rate limit hit (429) on %s - pausing all requests for %.0fs", path, retry_after)
                self.rate_limiter.pause_for(retry_after)
                attempt += 1
                if attempt > max_retries:
                    raise AsanaApiError(f"rate limited too many times calling {url}")
                continue
            if resp.status_code >= 500:
                attempt += 1
                if attempt > max_retries:
                    raise AsanaApiError(f"{url} returned {resp.status_code} repeatedly")
                log.warning("%s returned %d (attempt %d/%d), retrying", path, resp.status_code, attempt, max_retries)
                time.sleep(min(2**attempt, 30))
                continue
            if resp.status_code >= 400:
                raise AsanaApiError(f"{method} {url} -> {resp.status_code}: {resp.text[:500]}")

            return resp.json()

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def paginate(self, path: str, params: dict | None = None, page_size: int = 100) -> Generator[dict, None, None]:
        """Yield individual items across all pages of a collection endpoint.

        Asana caps every list response at ``page_size`` (max 100) records and
        hands back a ``next_page.offset`` cursor when there's more; this loops
        on that cursor - one rate-limited request per page - until it's gone,
        so callers never see a silently-truncated collection.
        """
        params = dict(params or {})
        params["limit"] = page_size
        offset = None
        page_num = 1
        while True:
            if offset:
                params["offset"] = offset
            payload = self.get(path, params=params)
            page_items = payload.get("data", [])
            log.debug("%s: page %d returned %d item(s)", path, page_num, len(page_items))
            for item in page_items:
                yield item
            next_page = payload.get("next_page")
            if not next_page or not next_page.get("offset"):
                return
            offset = next_page["offset"]
            page_num += 1

    def get_me(self) -> dict:
        return self.get("/users/me", {"opt_fields": "gid,name,email"})["data"]

    def get_workspaces(self) -> Iterable[dict]:
        yield from self.paginate("/workspaces", {"opt_fields": "gid,name,is_organization"})

    def get_teams_for_workspace(self, user_gid: str, workspace_gid: str) -> Iterable[dict]:
        yield from self.paginate(
            f"/users/{user_gid}/teams",
            {"organization": workspace_gid, "opt_fields": "gid,name,description"},
        )

    def get_team(self, team_gid: str) -> dict:
        return self.get(f"/teams/{team_gid}", {"opt_fields": "gid,name,description,organization.name"})["data"]

    def get_projects_for_team(self, team_gid: str) -> Iterable[dict]:
        # No `archived` filter: this is a full export, so archived projects
        # are included too (each result carries its own `archived` field so
        # callers/UI can still tell them apart).
        yield from self.paginate(
            f"/teams/{team_gid}/projects",
            {"opt_fields": "gid,name,archived,created_at,modified_at"},
        )

    def get_projects_for_workspace(self, workspace_gid: str) -> Iterable[dict]:
        """Every project you can see in the workspace, independent of team
        membership - catches projects that are visible to you (org-public,
        or you were added individually) whose home team you don't belong to,
        which /teams/{gid}/projects would never surface."""
        yield from self.paginate(
            f"/workspaces/{workspace_gid}/projects",
            {"opt_fields": "gid,name,archived,created_at,modified_at"},
        )

    def get_project(self, project_gid: str) -> dict:
        fields = (
            "gid,name,notes,color,archived,created_at,modified_at,due_on,start_on,"
            "public,default_view,owner.gid,owner.name,owner.email,"
            "team.gid,team.name,workspace.gid,workspace.name,"
            "members.gid,members.name,members.email,"
            "followers.gid,followers.name,followers.email,"
            "custom_field_settings.custom_field.name,custom_field_settings.custom_field.gid"
        )
        return self.get(f"/projects/{project_gid}", {"opt_fields": fields})["data"]

    def get_sections_for_project(self, project_gid: str) -> Iterable[dict]:
        yield from self.paginate(f"/projects/{project_gid}/sections", {"opt_fields": "gid,name,created_at"})

    def get_tasks_for_project(self, project_gid: str) -> Iterable[dict]:
        """Used for projects with no sections (e.g. plain list/board projects)."""
        yield from self.paginate(
            f"/projects/{project_gid}/tasks",
            {"opt_fields": "gid,name,resource_type", "completed_since": "1970-01-01"},
        )

    def get_tasks_for_section(self, section_gid: str) -> Iterable[dict]:
        yield from self.paginate(f"/sections/{section_gid}/tasks", {"opt_fields": "gid,name,resource_type"})

    def get_task(self, task_gid: str) -> dict:
        fields = (
            "gid,name,notes,html_notes,completed,completed_at,created_at,modified_at,"
            "due_on,due_at,start_on,start_at,permalink_url,num_subtasks,"
            "assignee.gid,assignee.name,assignee.email,"
            "assignee_status,parent.gid,parent.name,"
            "followers.gid,followers.name,followers.email,"
            "tags.gid,tags.name,"
            "memberships.section.gid,memberships.section.name,"
            "custom_fields.name,custom_fields.display_value,"
            "projects.gid,projects.name"
        )
        return self.get(f"/tasks/{task_gid}", {"opt_fields": fields})["data"]

    def get_subtasks_for_task(self, task_gid: str) -> Iterable[dict]:
        yield from self.paginate(f"/tasks/{task_gid}/subtasks", {"opt_fields": "gid,name,resource_type"})

    def get_stories_for_task(self, task_gid: str) -> Iterable[dict]:
        fields = "gid,type,resource_subtype,text,html_text,created_at,created_by.gid,created_by.name,created_by.email"
        yield from self.paginate(f"/tasks/{task_gid}/stories", {"opt_fields": fields})

    def get_attachments_for_task(self, task_gid: str) -> Iterable[dict]:
        fields = "gid,name,host,download_url,view_url,permanent_url,size,resource_subtype,created_at"
        yield from self.paginate(f"/tasks/{task_gid}/attachments", {"opt_fields": fields})

    def download_file(self, url: str) -> bytes:
        """Fetch raw bytes from an attachment's (short-lived, pre-signed)
        download_url. This isn't an api.asana.com call - it doesn't carry our
        bearer token and doesn't count against Asana's API rate limit - but
        it's still paced through the same limiter so a task with many
        attachments doesn't burst a pile of downloads at once."""
        self.rate_limiter.acquire()
        resp = requests.get(url, timeout=self.timeout)
        resp.raise_for_status()
        return resp.content
