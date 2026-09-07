"""Local web UI: token entry -> teams -> team's projects -> project tree.

Every GET that lists/shows data reads only from the local JSON tree (so the
browser is always usable offline / instantly on reload) except the two
explicitly "go fetch a little from Asana" actions: importing the team list,
and refreshing one team's project list -- both single, cheap, rate-limited
calls. Anything heavier (a project's sections/tasks/subtasks/comments) is
only ever done by enqueueing jobs for the background worker.
"""

from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from . import config as config_mod
from .client import AsanaApiError, AsanaAuthError, AsanaClient
from .importer import (
    ImporterContext,
    build_task_tree,
    ensure_team_projects_index,
    import_workspaces_and_teams,
    project_view,
)
from .jobs import JobQueue
from .rate_limiter import RateLimiter
from .storage import Paths, TaskIndex, read_json
from .worker import Worker

log = logging.getLogger("asana_migration.webapp")
STATIC_DIR = Path(__file__).parent / "static"


class AppState:
    """Everything the routes need, rebuilt whenever the token/settings change."""

    def __init__(self):
        self.cfg = config_mod.load_config()
        self.paths = Paths()
        self.queue = JobQueue()
        self.rate_limiter = RateLimiter(self.cfg.rate_limit_per_minute)
        self.client: AsanaClient | None = None
        self.ctx: ImporterContext | None = None
        self.worker: Worker | None = None
        if self.cfg.has_token:
            self._build_client()

    def _build_client(self) -> None:
        self.client = AsanaClient(self.cfg.token, self.rate_limiter)
        self.ctx = ImporterContext(
            client=self.client,
            paths=self.paths,
            queue=self.queue,
            max_subtask_depth=self.cfg.max_subtask_depth,
        )
        self.worker = Worker(self.ctx)
        self.worker.start()

    def set_token(self, token: str) -> None:
        self.cfg = config_mod.set_token(token)
        self._build_client()

    def update_rate_limit(self, rpm: int) -> None:
        self.cfg.rate_limit_per_minute = rpm
        config_mod.save_config(self.cfg)
        self.rate_limiter.set_rate(rpm)

    def require_client(self):
        if not self.client:
            raise ClientNotConfigured()


class ClientNotConfigured(Exception):
    pass


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    state = AppState()

    def error_response(exc: Exception, code: int = 400):
        return jsonify({"error": str(exc)}), code

    @app.errorhandler(ClientNotConfigured)
    def _no_token(exc):
        return error_response(Exception("No Asana token configured yet."), 401)

    @app.errorhandler(AsanaAuthError)
    def _auth_err(exc):
        return error_response(exc, 401)

    @app.errorhandler(AsanaApiError)
    def _api_err(exc):
        return error_response(exc, 502)

    # -- static frontend -------------------------------------------------

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:filename>")
    def static_files(filename):
        return send_from_directory(STATIC_DIR, filename)

    # -- app / token state -------------------------------------------------

    @app.get("/api/state")
    def api_state():
        return jsonify({
            "has_token": state.cfg.has_token,
            "rate_limit_per_minute": state.cfg.rate_limit_per_minute,
            "max_subtask_depth": state.cfg.max_subtask_depth,
            "queue": state.queue.stats(),
        })

    @app.post("/api/token")
    def api_set_token():
        token = (request.get_json(force=True) or {}).get("token", "").strip()
        if not token:
            return error_response(Exception("token is required"))
        # Validate against Asana before saving so a typo doesn't get persisted.
        probe = AsanaClient(token, state.rate_limiter)
        me = probe.get_me()
        state.set_token(token)
        return jsonify({"ok": True, "me": me})

    @app.post("/api/settings")
    def api_settings():
        body = request.get_json(force=True) or {}
        rpm = body.get("rate_limit_per_minute")
        if rpm:
            state.update_rate_limit(int(rpm))
        return jsonify({"ok": True, "rate_limit_per_minute": state.cfg.rate_limit_per_minute})

    @app.get("/api/jobs")
    def api_jobs():
        return jsonify(state.queue.stats())

    # -- teams (workspace-level) -------------------------------------------

    @app.post("/api/import-teams")
    def api_import_teams():
        state.require_client()
        data = import_workspaces_and_teams(state.ctx)
        return jsonify({"workspaces": data})

    @app.get("/api/teams")
    def api_teams():
        """Local-only: whatever has already been imported to disk."""
        out = []
        root = state.paths.root
        if root.exists():
            for ws_dir in sorted(root.iterdir()):
                ws = read_json(ws_dir / "workspace.json")
                if not ws:
                    continue
                teams = []
                teams_dir = ws_dir / "teams"
                if teams_dir.exists():
                    for team_dir in sorted(teams_dir.iterdir()):
                        team = read_json(team_dir / "team.json")
                        if team:
                            teams.append(team)
                out.append({"workspace": ws, "teams": teams})
        return jsonify({"workspaces": out, "imported": bool(out)})

    # -- one team's projects -------------------------------------------------

    def _team_dir_or_404(team_gid: str) -> Path:
        team_dir = state.paths.find_team_dir_anywhere(team_gid)
        if not team_dir:
            raise LookupError(f"Team {team_gid} not imported locally yet. Import teams first.")
        return team_dir

    @app.get("/api/teams/<team_gid>/projects")
    def api_team_projects(team_gid):
        try:
            team_dir = _team_dir_or_404(team_gid)
        except LookupError as exc:
            return error_response(exc, 404)

        index_path = team_dir / "projects_index.json"
        cached = read_json(index_path)
        if cached is None:
            state.require_client()
            cached = ensure_team_projects_index(state.ctx, team_dir, team_gid)

        projects = []
        for p in cached:
            project_dir = state.paths.project_dir(team_dir, p["gid"], p.get("name"))
            view = project_view(state.queue, project_dir, p["gid"])
            projects.append({**p, **view})
        return jsonify({
            "team": read_json(team_dir / "team.json"),
            "projects": projects,
        })

    @app.post("/api/teams/<team_gid>/refresh-projects")
    def api_refresh_team_projects(team_gid):
        try:
            team_dir = _team_dir_or_404(team_gid)
        except LookupError as exc:
            return error_response(exc, 404)
        state.require_client()
        projects = ensure_team_projects_index(state.ctx, team_dir, team_gid, force=True)
        return jsonify({"projects": projects})

    @app.post("/api/teams/<team_gid>/import-all")
    def api_import_all_projects(team_gid):
        try:
            team_dir = _team_dir_or_404(team_gid)
        except LookupError as exc:
            return error_response(exc, 404)
        state.require_client()
        cached = read_json(team_dir / "projects_index.json") or ensure_team_projects_index(
            state.ctx, team_dir, team_gid
        )
        queued = 0
        for p in cached:
            job = state.queue.push(
                "import_project",
                {"team_dir": str(team_dir), "project_gid": p["gid"]},
                dedupe_key=f"project:{p['gid']}",
            )
            if job:
                queued += 1
        return jsonify({"queued": queued, "total": len(cached)})

    @app.post("/api/teams/<team_gid>/projects/<project_gid>/import")
    def api_import_project(team_gid, project_gid):
        try:
            team_dir = _team_dir_or_404(team_gid)
        except LookupError as exc:
            return error_response(exc, 404)
        state.require_client()
        job = state.queue.push(
            "import_project",
            {"team_dir": str(team_dir), "project_gid": project_gid},
            dedupe_key=f"project:{project_gid}",
        )
        return jsonify({"queued": job is not None})

    # -- one project's detail (sections/tasks/subtasks tree) -----------------

    @app.get("/api/teams/<team_gid>/projects/<project_gid>")
    def api_project_detail(team_gid, project_gid):
        try:
            team_dir = _team_dir_or_404(team_gid)
        except LookupError as exc:
            return error_response(exc, 404)
        project_dir = state.paths.project_dir(team_dir, project_gid)
        view = project_view(state.queue, project_dir, project_gid)
        members = read_json(project_dir / "members.json", default=[]) or []
        sections = read_json(project_dir / "sections.json", default=[]) or []
        tree = build_task_tree(project_dir)
        return jsonify({**view, "members": members, "sections": sections, "tree": tree})

    @app.get("/api/teams/<team_gid>/projects/<project_gid>/tasks/<task_gid>")
    def api_task_detail(team_gid, project_gid, task_gid):
        try:
            team_dir = _team_dir_or_404(team_gid)
        except LookupError as exc:
            return error_response(exc, 404)
        project_dir = state.paths.project_dir(team_dir, project_gid)
        entry = TaskIndex(project_dir).read().get(task_gid)
        if not entry:
            return error_response(Exception("Task not imported locally yet."), 404)
        task_dir = project_dir / entry["path"]
        return jsonify({
            "task": read_json(task_dir / "task.json", default={}),
            "comments": read_json(task_dir / "comments.json", default=[]),
            "collaborators": read_json(task_dir / "collaborators.json", default=[]),
            "index_entry": entry,
        })

    return app
