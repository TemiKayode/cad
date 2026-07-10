// Operator admin panel (Part 6 P4). Every action here is re-checked
// server-side by require_platform_admin() -- this page's own gating
// (hide everything unless /api/auth/me says is_platform_admin) is purely
// so a non-admin signed-in user doesn't see a confusing empty shell,
// never the actual access boundary.

initThemeToggle();

let usersById = {};
let orgsById = {};

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

async function loadUsers() {
  const resp = await fetch("/api/admin/users");
  if (!resp.ok) return [];
  const users = await resp.json();
  usersById = Object.fromEntries(users.map((u) => [u.user_id, u]));
  return users;
}

async function loadOrgs() {
  const resp = await fetch("/api/admin/orgs");
  if (!resp.ok) return [];
  const orgs = await resp.json();
  orgsById = Object.fromEntries(orgs.map((o) => [o.org_id, o]));
  return orgs;
}

async function loadRooms() {
  const resp = await fetch("/api/admin/rooms");
  return resp.ok ? await resp.json() : [];
}

async function loadReports() {
  const resp = await fetch("/api/admin/reports?status=open");
  return resp.ok ? await resp.json() : [];
}

async function resolveReport(reportId, status) {
  const resp = await fetch(`/api/admin/reports/${encodeURIComponent(reportId)}/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  if (!resp.ok) {
    const detail = (await resp.json().catch(() => ({}))).detail || resp.statusText;
    showToast(`Could not update report: ${detail}`, "error");
    return;
  }
  showToast(status === "resolved" ? "Report resolved" : "Report dismissed", "success");
  await refreshReports();
}

async function setUserDisabled(userId, disabled) {
  const resp = await fetch(`/api/admin/users/${encodeURIComponent(userId)}/disabled`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ disabled }),
  });
  if (!resp.ok) {
    const detail = (await resp.json().catch(() => ({}))).detail || resp.statusText;
    showToast(`Could not update user: ${detail}`, "error");
    return;
  }
  showToast(disabled ? "User disabled" : "User re-enabled", "success");
  await refreshUsers();
}

async function deleteRoom(kind, roomId) {
  if (!window.confirm(`Permanently delete this ${kind} room (${roomId})? This cannot be undone.`)) return;
  const resp = await fetch(`/api/admin/rooms/${kind}/${encodeURIComponent(roomId)}`, { method: "DELETE" });
  if (!resp.ok) {
    const detail = (await resp.json().catch(() => ({}))).detail || resp.statusText;
    showToast(`Could not delete room: ${detail}`, "error");
    return;
  }
  showToast("Room deleted", "success");
  await refreshRooms();
}

function renderUsers(users) {
  const body = document.getElementById("usersTableBody");
  body.innerHTML = "";
  for (const u of users) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(u.email)}</td>
      <td>${escapeHtml(u.display_name || "")}</td>
      <td>${u.disabled ? '<span class="status-pill offline"><span class="status-dot"></span>disabled</span>' : '<span class="status-pill online"><span class="status-dot"></span>active</span>'}</td>
      <td></td>
    `;
    const actionCell = tr.lastElementChild;
    const btn = document.createElement("button");
    btn.className = u.disabled ? "primary-btn" : "danger-btn";
    btn.textContent = u.disabled ? "Enable" : "Disable";
    btn.onclick = () => setUserDisabled(u.user_id, !u.disabled);
    actionCell.appendChild(btn);
    body.appendChild(tr);
  }
}

function renderOrgs(orgs) {
  const body = document.getElementById("orgsTableBody");
  body.innerHTML = "";
  for (const o of orgs) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(o.name)}</td>
      <td style="font-family:var(--font-mono);color:var(--text-secondary)">${escapeHtml(o.org_id)}</td>
      <td>${escapeHtml(o.default_visibility)}</td>
      <td>${escapeHtml(o.billing_plan || "free")}${o.billing_status ? " (" + escapeHtml(o.billing_status) + ")" : ""}</td>
    `;
    body.appendChild(tr);
  }
}

function renderRooms(rooms) {
  const body = document.getElementById("roomsTableBody");
  body.innerHTML = "";
  for (const r of rooms) {
    const owner = r.owner_org_id
      ? `org: ${escapeHtml((orgsById[r.owner_org_id] || {}).name || r.owner_org_id)}`
      : escapeHtml((usersById[r.owner_user_id] || {}).email || r.owner_user_id || "");
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(r.kind)}</td>
      <td style="font-family:var(--font-mono);color:var(--text-secondary)">${escapeHtml(r.room_id)}</td>
      <td>${owner}</td>
      <td>${escapeHtml(r.visibility)}</td>
      <td></td>
    `;
    const actionCell = tr.lastElementChild;
    const btn = document.createElement("button");
    btn.className = "danger-btn";
    btn.textContent = "Delete";
    btn.onclick = () => deleteRoom(r.kind, r.room_id);
    actionCell.appendChild(btn);
    body.appendChild(tr);
  }
}

function renderReports(reports) {
  const body = document.getElementById("reportsTableBody");
  body.innerHTML = "";
  if (reports.length === 0) {
    body.innerHTML = '<tr><td colspan="5" class="empty-hint">No open reports.</td></tr>';
    return;
  }
  for (const r of reports) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(r.room_kind)}/${escapeHtml(r.room_id)}</td>
      <td>${escapeHtml(r.reason)}</td>
      <td>${escapeHtml(r.details || "")}</td>
      <td>${escapeHtml(r.status)}</td>
      <td></td>
    `;
    const actionCell = tr.lastElementChild;
    const resolveBtn = document.createElement("button");
    resolveBtn.className = "primary-btn";
    resolveBtn.textContent = "Resolve";
    resolveBtn.onclick = () => resolveReport(r.report_id, "resolved");
    const dismissBtn = document.createElement("button");
    dismissBtn.className = "ghost-btn";
    dismissBtn.textContent = "Dismiss";
    dismissBtn.onclick = () => resolveReport(r.report_id, "dismissed");
    actionCell.appendChild(resolveBtn);
    actionCell.appendChild(dismissBtn);
    body.appendChild(tr);
  }
}

async function refreshUsers() { renderUsers(await loadUsers()); }
async function refreshOrgs() { renderOrgs(await loadOrgs()); }
async function refreshRooms() { renderRooms(await loadRooms()); }
async function refreshReports() { renderReports(await loadReports()); }

async function init() {
  const acct = await fetchAccount();
  if (!acct.signed_in || !acct.is_platform_admin) {
    document.getElementById("adminGate").style.display = "";
    return;
  }
  document.getElementById("adminMain").style.display = "";
  await refreshUsers();
  await refreshOrgs();
  await refreshRooms();
  await refreshReports();
}

init();
