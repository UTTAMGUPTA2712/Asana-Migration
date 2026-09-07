"use strict";

const $ = (id) => document.getElementById(id);
const views = {
  token: $("view-token"), teams: $("view-teams"), team: $("view-team"), project: $("view-project"),
};
const state = { team: null, project: null, pollTimer: null };

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function showView(name) {
  Object.entries(views).forEach(([k, el]) => el.classList.toggle("hidden", k !== name));
}

function showError(msg) {
  const el = $("errorBanner");
  el.textContent = msg;
  el.classList.remove("hidden");
}
function clearError() { $("errorBanner").classList.add("hidden"); }

async function api(path, opts) {
  const resp = await fetch(path, opts && {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await resp.json(); } catch (_) { /* no body */ }
  if (!resp.ok) throw new Error((data && data.error) || `Request failed (${resp.status})`);
  return data;
}

function setCrumbs() {
  const parts = [`<a data-nav="teams">Teams</a>`];
  if (state.team) parts.push(`<a data-nav="team">${escapeHtml(state.team.name)}</a>`);
  if (state.project) parts.push(`<span>${escapeHtml(state.project.name)}</span>`);
  $("crumbs").innerHTML = "› " + parts.join(" › ");
}
document.addEventListener("click", (e) => {
  const nav = e.target.getAttribute && e.target.getAttribute("data-nav");
  if (nav === "teams") goTeams();
  else if (nav === "team" && state.team) openTeam(state.team.gid, state.team.name);
});

// -- boot ---------------------------------------------------------------

async function boot() {
  stopPolling();
  try {
    const s = await api("/api/state");
    $("rpmInput").value = s.rate_limit_per_minute;
    $("depthInput").value = s.max_subtask_depth;
    updateQueueBadge(s.queue);
    if (!s.has_token) { showView("token"); return; }
    await goTeams();
  } catch (err) {
    showError(err.message);
  }
}

setInterval(async () => {
  try { updateQueueBadge(await api("/api/jobs")); } catch (_) { /* ignore transient errors */ }
}, 3000);

function updateQueueBadge(q) {
  const active = q.queued + q.running;
  const badge = $("queueBadge");
  badge.textContent = active
    ? `⏳ ${active} pending (${q.running} running)` + (q.error ? `, ${q.error} failed` : "")
    : (q.error ? `${q.error} failed jobs` : "idle");
  badge.classList.toggle("active", active > 0);
}

// -- settings popover -----------------------------------------------------

$("settingsBtn").addEventListener("click", () => $("settingsPop").classList.toggle("hidden"));
$("saveSettingsBtn").addEventListener("click", async () => {
  try {
    await api("/api/settings", { method: "POST", body: { rate_limit_per_minute: Number($("rpmInput").value) } });
    $("settingsPop").classList.add("hidden");
    clearError();
  } catch (err) { showError(err.message); }
});
$("changeTokenBtn").addEventListener("click", () => { $("settingsPop").classList.add("hidden"); showView("token"); });

// -- token screen -----------------------------------------------------

$("connectBtn").addEventListener("click", async () => {
  const token = $("tokenInput").value.trim();
  if (!token) return;
  $("connectBtn").disabled = true;
  try {
    await api("/api/token", { method: "POST", body: { token } });
    $("tokenInput").value = "";
    clearError();
    await boot();
  } catch (err) {
    showError(err.message);
  } finally {
    $("connectBtn").disabled = false;
  }
});

// -- teams screen -----------------------------------------------------

async function goTeams() {
  stopPolling();
  state.team = null; state.project = null;
  showView("teams"); setCrumbs();
  await loadTeams();
}

async function loadTeams() {
  const data = await api("/api/teams");
  $("teamsEmpty").classList.toggle("hidden", data.imported);
  const grid = $("teamsGrid");
  grid.innerHTML = "";
  for (const ws of data.workspaces) {
    for (const team of ws.teams) {
      const tile = document.createElement("div");
      tile.className = "tile";
      tile.innerHTML = `<h3>${escapeHtml(team.name)}</h3>
        <div class="sub">${escapeHtml(ws.workspace.name)}</div>`;
      tile.addEventListener("click", () => openTeam(team.gid, team.name));
      grid.appendChild(tile);
    }
  }
}

$("importTeamsBtn").addEventListener("click", async () => {
  $("importTeamsBtn").disabled = true;
  $("importTeamsBtn").textContent = "Importing…";
  try {
    await api("/api/import-teams", { method: "POST" });
    clearError();
    await loadTeams();
  } catch (err) {
    showError(err.message);
  } finally {
    $("importTeamsBtn").disabled = false;
    $("importTeamsBtn").textContent = "Import teams";
  }
});

// -- team screen (projects) -----------------------------------------------------

async function openTeam(gid, name) {
  stopPolling();
  state.team = { gid, name };
  state.project = null;
  showView("team"); setCrumbs();
  $("teamTitle").textContent = name;
  await loadProjects();
  state.pollTimer = setInterval(loadProjects, 3000);
}

function statusBadge(view) {
  const status = view.error_jobs > 0 ? "error" : view.status;
  const label = { not_imported: "not imported", importing: "importing…", complete: "imported", error: "error" }[status] || status;
  return `<span class="badge ${status}">${label}</span>`;
}

async function loadProjects() {
  let data;
  try {
    data = await api(`/api/teams/${state.team.gid}/projects`);
    clearError();
  } catch (err) { showError(err.message); return; }

  const grid = $("projectsGrid");
  grid.innerHTML = "";
  let anyActive = false;
  for (const p of data.projects) {
    if (p.status === "importing") anyActive = true;
    const meta = p.meta || {};
    const done = meta.tasks_imported || 0, total = meta.tasks_discovered || 0;
    const pct = total ? Math.round((done / total) * 100) : (p.status === "complete" ? 100 : 0);
    const tile = document.createElement("div");
    tile.className = "tile";
    tile.innerHTML = `
      <div class="row"><h3>${escapeHtml(p.name)}</h3>${statusBadge(p)}</div>
      <div class="sub">${meta.sections_total ?? "?"} sections · ${done}/${total || "?"} tasks · ${meta.comments_imported || 0} tasks' comments fetched</div>
      <div class="progress ${p.status === "not_imported" ? "hidden" : ""}"><div style="width:${pct}%"></div></div>
      <div class="row" style="margin-top:10px;">
        <button class="small importBtn" ${p.status === "not_imported" ? "" : "disabled"}>
          ${p.status === "not_imported" ? "Import" : (p.status === "complete" ? "Imported ✓" : "Importing…")}
        </button>
      </div>`;
    tile.querySelector(".importBtn").addEventListener("click", async (e) => {
      e.stopPropagation();
      await api(`/api/teams/${state.team.gid}/projects/${p.gid}/import`, { method: "POST" });
      loadProjects();
    });
    tile.addEventListener("click", () => openProject(p.gid, p.name));
    grid.appendChild(tile);
  }
  if (!anyActive) stopPolling(loadProjects);
}

$("importAllBtn").addEventListener("click", async () => {
  try {
    const r = await api(`/api/teams/${state.team.gid}/import-all`, { method: "POST" });
    clearError();
    if (!state.pollTimer) state.pollTimer = setInterval(loadProjects, 3000);
    await loadProjects();
    showError(`Queued ${r.queued} of ${r.total} project(s) for import. This will run slowly in the background — you can navigate around while it works.`);
  } catch (err) { showError(err.message); }
});
$("refreshProjectsBtn").addEventListener("click", async () => {
  try { await api(`/api/teams/${state.team.gid}/refresh-projects`, { method: "POST" }); await loadProjects(); }
  catch (err) { showError(err.message); }
});

function stopPolling() {
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
}

// -- project screen -----------------------------------------------------

async function openProject(gid, name) {
  stopPolling();
  state.project = { gid, name };
  showView("project"); setCrumbs();
  $("projectTitle").textContent = name;
  await loadProjectDetail();
  state.pollTimer = setInterval(loadProjectDetail, 3000);
}

async function loadProjectDetail() {
  let data;
  try {
    data = await api(`/api/teams/${state.team.gid}/projects/${state.project.gid}`);
    clearError();
  } catch (err) { showError(err.message); return; }

  const status = data.error_jobs > 0 ? "error" : data.status;
  const label = { not_imported: "not imported", importing: "importing…", complete: "imported", error: "error" }[status] || status;
  const badgeEl = $("projectBadge");
  badgeEl.className = `badge ${status}`;
  badgeEl.textContent = label;

  const meta = data.meta || {};
  const done = meta.tasks_imported || 0, total = meta.tasks_discovered || 0;
  $("projectMeta").textContent =
    `${meta.sections_total ?? "?"} sections · ${done}/${total || "?"} tasks imported · ${meta.comments_imported || 0} tasks' comments fetched` +
    (meta.started_at ? ` · started ${meta.started_at}` : "") + (meta.completed_at ? ` · completed ${meta.completed_at}` : "");

  const wrap = $("projectProgressWrap");
  if (data.status === "not_imported") { wrap.classList.add("hidden"); }
  else {
    wrap.classList.remove("hidden");
    const pct = total ? Math.round((done / total) * 100) : (data.status === "complete" ? 100 : 5);
    $("projectProgressBar").style.width = pct + "%";
  }

  $("importProjectBtn").classList.toggle("hidden", data.status !== "not_imported");

  $("membersList").innerHTML = (data.members || [])
    .map((m) => `<span class="chip">${escapeHtml(m.name || m.email || m.gid)}</span>`)
    .join("") || `<span class="muted">No members data yet.</span>`;

  $("taskTree").innerHTML = renderTree(data.tree || []);
  if (!(data.tree || []).length && data.status === "not_imported") {
    $("taskTree").innerHTML = `<p class="muted">Not imported yet — click "Import this project" above.</p>`;
  }
  document.querySelectorAll(".task-row").forEach((row) => {
    row.addEventListener("click", () => openTaskModal(row.dataset.gid, row.dataset.name));
  });

  if (data.status !== "importing") stopPolling(loadProjectDetail);
}

function renderTree(tree) {
  return tree.map((block) => `
    <div style="margin-top:14px;">
      <strong>${escapeHtml(block.section ? block.section.name : "(no section)")}</strong>
      ${renderTasks(block.tasks)}
    </div>`).join("");
}

function renderTasks(tasks) {
  if (!tasks || !tasks.length) return `<p class="muted" style="margin:6px 0 0;">No tasks imported yet.</p>`;
  return `<ul class="task-list">` + tasks.map((t) => `
    <li>
      <div class="task-row ${t.completed ? "done" : ""}" data-gid="${t.gid}" data-name="${escapeHtml(t.name)}">
        <span class="name">${escapeHtml(t.name)}</span>
        ${t.assignee ? `<span class="tag">${escapeHtml(t.assignee)}</span>` : ""}
        ${t.comments_count ? `<span class="tag">💬 ${t.comments_count}</span>` : ""}
        ${t.num_subtasks ? `<span class="tag">${t.subtasks.length}/${t.num_subtasks} subtasks</span>` : ""}
      </div>
      ${t.subtasks && t.subtasks.length ? renderTasks(t.subtasks) : ""}
    </li>`).join("") + `</ul>`;
}

$("importProjectBtn").addEventListener("click", async () => {
  try {
    await api(`/api/teams/${state.team.gid}/projects/${state.project.gid}/import`, { method: "POST" });
    if (!state.pollTimer) state.pollTimer = setInterval(loadProjectDetail, 3000);
    await loadProjectDetail();
  } catch (err) { showError(err.message); }
});

// -- task modal -----------------------------------------------------

async function openTaskModal(taskGid, name) {
  $("taskModalBackdrop").classList.remove("hidden");
  $("taskModalBody").innerHTML = `<h3>${escapeHtml(name)}</h3><p class="muted">Loading…</p>`;
  try {
    const data = await api(`/api/teams/${state.team.gid}/projects/${state.project.gid}/tasks/${taskGid}`);
    const t = data.task || {};
    const collaborators = (data.collaborators || [])
      .map((c) => `<span class="chip">${escapeHtml(c.name || c.email)}</span>`).join("") || `<span class="muted">None</span>`;
    const comments = (data.comments || []).map((c) => `
      <div class="comment">
        <div class="meta">${escapeHtml((c.created_by || {}).name || "Unknown")} · ${escapeHtml(c.created_at || "")}</div>
        <div>${escapeHtml(c.text || "")}</div>
      </div>`).join("") || `<p class="muted">No comments.</p>`;
    $("taskModalBody").innerHTML = `
      <h3>${escapeHtml(t.name)}</h3>
      <p class="muted">${t.completed ? "✅ Completed" : "Open"} ${t.due_on ? "· due " + escapeHtml(t.due_on) : ""}
        ${t.assignee ? "· assignee " + escapeHtml(t.assignee.name) : ""}</p>
      <p>${escapeHtml(t.notes || "").replace(/\n/g, "<br>") || '<span class="muted">No description.</span>'}</p>
      <div class="section-block"><h4>Collaborators</h4><div class="chip-list">${collaborators}</div></div>
      <div class="section-block"><h4>Comments</h4>${comments}</div>
    `;
  } catch (err) {
    $("taskModalBody").innerHTML = `<p class="error-banner">${escapeHtml(err.message)}</p>`;
  }
}
$("closeModalBtn").addEventListener("click", () => $("taskModalBackdrop").classList.add("hidden"));
$("taskModalBackdrop").addEventListener("click", (e) => { if (e.target.id === "taskModalBackdrop") e.currentTarget.classList.add("hidden"); });

boot();
