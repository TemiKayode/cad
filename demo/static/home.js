// Phase 17: the workspace home page. Lists every room (both kinds) via
// GET /api/workspace/rooms, with per-room rename/history/restore backed by
// the REST endpoints in crdt_cad.server.app. Uses the same `withToken`/
// `roomTokenFor` helpers from common.js the 2D/3D demos already use for
// their own room-scoped REST calls -- when auth is enabled, a thumbnail/
// rename/history request for a room this browser hasn't joined (no stored
// token) simply 401s and falls back to a placeholder, rather than the page
// erroring outright; see the README for this documented, accepted limit.

initThemeToggle();

function relativeTime(unixSeconds) {
  const deltaSec = Date.now() / 1000 - unixSeconds;
  if (deltaSec < 60) return "just now";
  const mins = Math.floor(deltaSec / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(unixSeconds * 1000).toLocaleDateString();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function roomHomeUrl(kind, roomId) {
  const path = kind === "mesh" ? "/3d" : "/2d";
  return `${path}?room=${encodeURIComponent(roomId)}`;
}

async function fetchRooms() {
  const resp = await fetch("/api/workspace/rooms");
  if (!resp.ok) throw new Error(`failed to list rooms (${resp.status})`);
  return resp.json();
}

function renderRoomCard(room) {
  const {
    kind, room_id: roomId, display_name: displayName, updated_at: updatedAt,
    visibility, owner_display_name: ownerDisplayName, your_role: yourRole, org_name: orgName,
  } = room;
  const kindLabel = kind === "mesh" ? "3D" : "2D";
  const name = displayName || roomId;

  const card = document.createElement("div");
  card.className = "room-card";

  const thumb = document.createElement("div");
  thumb.className = "room-thumb";
  if (kind === "drawing") {
    const img = document.createElement("img");
    img.src = withToken(`/api/rooms/${encodeURIComponent(roomId)}/thumbnail.svg`, "drawing", roomId);
    img.onerror = () => {
      thumb.innerHTML = iconHtml("file", "placeholder-icon");
    };
    thumb.appendChild(img);
  } else {
    thumb.innerHTML = iconHtml("box", "placeholder-icon");
  }
  card.appendChild(thumb);

  const body = document.createElement("div");
  body.className = "room-card-body";
  // Part 6 P2: visibility is only ever set on a room a signed-in user
  // has claimed -- every pre-existing/anonymous room keeps exactly
  // today's badge-free card.
  const visBadge = visibility
    ? `<span class="room-visibility-badge ${visibility}">${visibility}</span>` : "";
  // Part 6 P3: an org-owned room shows its org, not the original
  // creator -- access here comes from team membership, not a personal
  // grant, and that's the more useful thing to tell every other member.
  let contextMeta = "";
  if (orgName) {
    contextMeta = `<span class="room-card-meta">org: ${escapeHtml(orgName)} -- your role: ${escapeHtml(yourRole)}</span>`;
  } else if (yourRole && yourRole !== "owner" && ownerDisplayName) {
    contextMeta = `<span class="room-card-meta">shared by ${escapeHtml(ownerDisplayName)} -- your role: ${yourRole}</span>`;
  }
  body.innerHTML = `
    <span class="room-kind-badge ${kind}">${kindLabel}</span>
    ${visBadge}
    <span class="room-card-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
    <span class="room-card-meta">room: ${escapeHtml(roomId)}</span>
    <span class="room-card-meta">updated ${relativeTime(updatedAt)}</span>
    ${contextMeta}
  `;
  card.appendChild(body);

  const actions = document.createElement("div");
  actions.className = "room-card-actions";

  const openBtn = document.createElement("button");
  openBtn.textContent = "Open";
  openBtn.onclick = () => {
    location.href = roomHomeUrl(kind, roomId);
  };
  actions.appendChild(openBtn);

  const renameBtn = document.createElement("button");
  renameBtn.textContent = "Rename";
  renameBtn.onclick = () => openRenameModal(kind, roomId, name);
  actions.appendChild(renameBtn);

  const historyBtn = document.createElement("button");
  historyBtn.textContent = "History";
  historyBtn.onclick = () => openHistoryModal(kind, roomId, name);
  actions.appendChild(historyBtn);

  if (yourRole === "owner") {
    const shareBtn = document.createElement("button");
    shareBtn.textContent = "Share";
    shareBtn.onclick = () => openShareModal(kind, roomId, name);
    actions.appendChild(shareBtn);
  }

  card.appendChild(actions);
  return card;
}

async function loadRooms() {
  const grid = document.getElementById("roomGrid");
  const emptyState = document.getElementById("emptyState");
  let rooms;
  try {
    rooms = await fetchRooms();
  } catch (err) {
    grid.innerHTML = "";
    emptyState.style.display = "block";
    emptyState.textContent = `Could not load rooms: ${err.message}`;
    return;
  }
  grid.innerHTML = "";
  if (!rooms.length) {
    emptyState.style.display = "block";
    emptyState.textContent = 'No rooms yet -- click "New 2D drawing" or "New 3D mesh" above to create one.';
    return;
  }
  emptyState.style.display = "none";
  for (const room of rooms) grid.appendChild(renderRoomCard(room));
}

// -- rename modal -------------------------------------------------------------
//
// Phase D4 keyboard reachability audit: these two modals predate this
// phase (Phase 17) as plain `display:none` <div>s with no focus
// management at all -- trapFocusIn (common.js, shared with the command
// palette/shortcut overlay/Time-Travel Merge preview) keeps Tab inside
// whichever is open and hands focus back to the "Rename"/"History"
// button that opened it, and both now close on Esc or a backdrop click
// like every other overlay in the app.

let renameTarget = null; // { kind, roomId }
let _releaseModalFocus = null;

function openRenameModal(kind, roomId, currentName) {
  renameTarget = { kind, roomId };
  document.getElementById("renameInput").value = currentName;
  document.getElementById("renameModal").style.display = "flex";
  _releaseModalFocus = trapFocusIn(document.querySelector("#renameModal .modal"));
  document.getElementById("renameInput").focus(); // preferred over trapFocusIn's default "first focusable" (the close button)
}

function closeRenameModal() {
  document.getElementById("renameModal").style.display = "none";
  renameTarget = null;
  if (_releaseModalFocus) { _releaseModalFocus(); _releaseModalFocus = null; }
}

document.getElementById("renameModal").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) closeRenameModal();
});

document.getElementById("renameModalClose").onclick = closeRenameModal;
document.getElementById("renameCancelBtn").onclick = closeRenameModal;
document.getElementById("renameSaveBtn").onclick = async () => {
  if (!renameTarget) return;
  const { kind, roomId } = renameTarget;
  const name = document.getElementById("renameInput").value.trim();
  const path = kind === "mesh" ? `/api/mesh/${encodeURIComponent(roomId)}/rename` : `/api/rooms/${encodeURIComponent(roomId)}/rename`;
  try {
    const resp = await fetch(withToken(path, kind, roomId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ display_name: name }),
    });
    if (!resp.ok) {
      const detail = (await resp.json().catch(() => ({}))).detail || resp.statusText;
      throw new Error(detail);
    }
    closeRenameModal();
    await loadRooms();
  } catch (err) {
    window.alert(`Rename failed: ${err.message}`);
  }
};

// -- history / restore modal ---------------------------------------------------

function closeHistoryModal() {
  document.getElementById("historyModal").style.display = "none";
  if (_releaseModalFocus) { _releaseModalFocus(); _releaseModalFocus = null; }
}

async function openHistoryModal(kind, roomId, name) {
  document.getElementById("historyModalTitle").textContent = `Version history -- ${name}`;
  const body = document.getElementById("historyModalBody");
  body.innerHTML = '<div class="empty-hint">Loading...</div>';
  document.getElementById("historyModal").style.display = "flex";
  _releaseModalFocus = trapFocusIn(document.querySelector("#historyModal .modal"));

  const listPath = kind === "mesh" ? `/api/mesh/${encodeURIComponent(roomId)}/versions` : `/api/rooms/${encodeURIComponent(roomId)}/versions`;
  let versions;
  try {
    const resp = await fetch(withToken(listPath, kind, roomId));
    if (!resp.ok) throw new Error(resp.statusText);
    versions = await resp.json();
  } catch (err) {
    body.innerHTML = `<div class="empty-hint">Could not load history: ${escapeHtml(err.message)}</div>`;
    return;
  }

  if (!versions.length) {
    body.innerHTML = '<div class="empty-hint">No checkpoints yet -- version history is written periodically as the room is edited, and immediately whenever someone hits Save.</div>';
    return;
  }

  body.innerHTML = "";
  for (const v of versions) {
    const row = document.createElement("div");
    row.className = "version-row";
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = `${new Date(v.created_at * 1000).toLocaleString()} (${relativeTime(v.created_at)})`;
    row.appendChild(meta);

    const restoreBtn = document.createElement("button");
    restoreBtn.textContent = "Restore";
    restoreBtn.title = "Forks this version into a brand-new room -- never rewrites this room's own history";
    restoreBtn.onclick = () => restoreVersion(kind, roomId, v.version_id);
    row.appendChild(restoreBtn);

    body.appendChild(row);
  }
}

async function restoreVersion(kind, roomId, versionId) {
  const restorePath = kind === "mesh"
    ? `/api/mesh/${encodeURIComponent(roomId)}/versions/${versionId}/restore`
    : `/api/rooms/${encodeURIComponent(roomId)}/versions/${versionId}/restore`;
  try {
    const resp = await fetch(withToken(restorePath, kind, roomId), { method: "POST" });
    if (!resp.ok) {
      const detail = (await resp.json().catch(() => ({}))).detail || resp.statusText;
      throw new Error(detail);
    }
    const { new_room_id: newRoomId } = await resp.json();
    // A restored version may need auth in its own right (it inherited the
    // token-gated content of an existing room's snapshot) -- carry this
    // browser's token for the *original* room forward to the new one, the
    // same way a Share invite link does, so opening it doesn't immediately
    // prompt for the secret again.
    const token = roomTokenFor(kind, roomId);
    if (token) localStorage.setItem(`crdt_cad_token:${kind}:${newRoomId}`, token);
    closeHistoryModal();
    location.href = roomHomeUrl(kind, newRoomId);
  } catch (err) {
    window.alert(`Restore failed: ${err.message}`);
  }
}

document.getElementById("historyModalClose").onclick = closeHistoryModal;

document.getElementById("historyModal").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) closeHistoryModal();
});

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (document.getElementById("historyModal").style.display === "flex") closeHistoryModal();
  else if (document.getElementById("renameModal").style.display === "flex") closeRenameModal();
  else if (document.getElementById("shareModal").style.display === "flex") closeShareModal();
  else if (document.getElementById("orgsModal").style.display === "flex") closeOrgsModal();
});

// -- sharing (Part 6 P2) -----------------------------------------------------
// Only ever reachable via a room card's "Share" button, itself only
// rendered when the room's list entry says `your_role === "owner"` --
// the REST endpoints underneath enforce the same thing server-side
// (require_owner_access), so this is a convenience gate, not the real
// boundary. Plain fetch(), no `withToken()`: these endpoints authorize
// off the signed-in session cookie, not a room token.

let shareTarget = null; // { kind, roomId }

function sharingBasePath(kind, roomId) {
  return kind === "mesh" ? `/api/mesh/${encodeURIComponent(roomId)}` : `/api/rooms/${encodeURIComponent(roomId)}`;
}

async function openShareModal(kind, roomId, name) {
  shareTarget = { kind, roomId };
  document.getElementById("shareModalTitle").textContent = `Share -- ${name}`;
  document.getElementById("shareModal").style.display = "flex";
  _releaseModalFocus = trapFocusIn(document.querySelector("#shareModal .modal"));
  await loadSharing();
  await loadShareOrgTransferOptions();
}

// Part 6 P3: lets the room's current manager (owner, or an org admin --
// require_owner_access allows both) hand it off to any organization
// they themselves admin. Hidden entirely if they don't admin any org --
// nothing to transfer *to*.
async function loadShareOrgTransferOptions() {
  const section = document.getElementById("shareOrgTransferSection");
  const select = document.getElementById("shareOrgSelect");
  section.style.display = "none";
  select.innerHTML = "";
  const resp = await fetch("/api/orgs");
  if (!resp.ok) return;
  const orgs = (await resp.json()).filter((o) => o.role === "admin");
  if (!orgs.length) return;
  for (const org of orgs) {
    const opt = document.createElement("option");
    opt.value = org.org_id;
    opt.textContent = org.name;
    select.appendChild(opt);
  }
  section.style.display = "";
}

document.getElementById("shareOrgTransferBtn").onclick = async () => {
  if (!shareTarget) return;
  const orgId = document.getElementById("shareOrgSelect").value;
  if (!orgId) return;
  const { kind, roomId } = shareTarget;
  const resp = await fetch(`${sharingBasePath(kind, roomId)}/transfer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ org_id: orgId }),
  });
  if (!resp.ok) {
    const detail = (await resp.json().catch(() => ({}))).detail || resp.statusText;
    showToast(`Could not transfer: ${detail}`, "error");
    return;
  }
  showToast("Transferred to organization", "success");
  await loadSharing();
  await loadRooms();
};

function closeShareModal() {
  document.getElementById("shareModal").style.display = "none";
  shareTarget = null;
  if (_releaseModalFocus) { _releaseModalFocus(); _releaseModalFocus = null; }
}

async function loadSharing() {
  if (!shareTarget) return;
  const { kind, roomId } = shareTarget;
  const resp = await fetch(`${sharingBasePath(kind, roomId)}/sharing`);
  if (!resp.ok) {
    document.getElementById("shareVisibilityRow").textContent = "Could not load sharing settings.";
    return;
  }
  const data = await resp.json();
  renderShareVisibility(data.visibility);
  renderShareGrants(data.grants);
}

function renderShareVisibility(current) {
  const row = document.getElementById("shareVisibilityRow");
  row.innerHTML = "";
  for (const v of ["private", "link", "public"]) {
    const btn = document.createElement("button");
    btn.textContent = v[0].toUpperCase() + v.slice(1);
    btn.className = v === current ? "primary-btn" : "";
    btn.onclick = () => setVisibility(v);
    row.appendChild(btn);
  }
}

async function setVisibility(visibility) {
  if (!shareTarget) return;
  const { kind, roomId } = shareTarget;
  const resp = await fetch(`${sharingBasePath(kind, roomId)}/visibility`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ visibility }),
  });
  if (!resp.ok) {
    const detail = (await resp.json().catch(() => ({}))).detail || resp.statusText;
    showToast(`Could not change visibility: ${detail}`, "error");
    return;
  }
  await loadSharing();
  await loadRooms();
}

function renderShareGrants(grants) {
  const list = document.getElementById("shareGrantsList");
  list.innerHTML = "";
  if (!grants.length) {
    list.innerHTML = '<div class="empty-hint">Nobody else has access yet.</div>';
    return;
  }
  for (const g of grants) {
    const row = document.createElement("div");
    row.className = "share-grant-row";
    row.innerHTML = `
      <span class="grant-email" title="${escapeHtml(g.email)}">${escapeHtml(g.display_name || g.email)}</span>
      <span class="room-card-meta">${escapeHtml(g.role)}</span>
    `;
    const removeBtn = document.createElement("button");
    removeBtn.className = "ghost-btn";
    removeBtn.title = "Remove access";
    removeBtn.setAttribute("aria-label", "Remove access");
    removeBtn.innerHTML = iconHtml("x");
    removeBtn.onclick = () => revokeGrant(g.user_id);
    row.appendChild(removeBtn);
    list.appendChild(row);
  }
}

async function revokeGrant(userId) {
  if (!shareTarget) return;
  const { kind, roomId } = shareTarget;
  await fetch(`${sharingBasePath(kind, roomId)}/grant/${encodeURIComponent(userId)}`, { method: "DELETE" });
  await loadSharing();
}

document.getElementById("shareGrantBtn").onclick = async () => {
  if (!shareTarget) return;
  const email = document.getElementById("shareGrantEmail").value.trim();
  if (!email) return;
  const role = document.getElementById("shareGrantRole").value;
  const { kind, roomId } = shareTarget;
  const resp = await fetch(`${sharingBasePath(kind, roomId)}/grant`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, role }),
  });
  if (!resp.ok) {
    const detail = (await resp.json().catch(() => ({}))).detail || resp.statusText;
    showToast(`Could not grant access: ${detail}`, "error");
    return;
  }
  document.getElementById("shareGrantEmail").value = "";
  await loadSharing();
};

document.getElementById("shareModalClose").onclick = closeShareModal;
document.getElementById("shareModal").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) closeShareModal();
});

// -- organizations & teams (Part 6 P3) ---------------------------------------
// A team, not just a shared document: an org's members see every
// private, org-owned document automatically (per _account_role_for_room
// server-side) -- no per-document, per-person grant needed once someone
// is on the team. This modal has two views sharing one dialog: a list
// of the signed-in user's orgs, and (clicking one) that org's own
// members/defaults/invite panel.

let currentOrgId = null;

function openOrgsModal() {
  document.getElementById("orgsModal").style.display = "flex";
  _releaseModalFocus = trapFocusIn(document.querySelector("#orgsModal .modal"));
  showOrgsListView();
}

function closeOrgsModal() {
  document.getElementById("orgsModal").style.display = "none";
  currentOrgId = null;
  if (_releaseModalFocus) { _releaseModalFocus(); _releaseModalFocus = null; }
}

function showOrgsListView() {
  currentOrgId = null;
  document.getElementById("orgsListView").style.display = "";
  document.getElementById("orgDetailView").style.display = "none";
  document.getElementById("orgsModalTitle").textContent = "Organizations";
  loadOrgsList();
}

async function loadOrgsList() {
  const list = document.getElementById("orgsList");
  const resp = await fetch("/api/orgs");
  const orgs = resp.ok ? await resp.json() : [];
  list.innerHTML = "";
  if (!orgs.length) {
    list.innerHTML = '<div class="empty-hint">Not a member of any organization yet.</div>';
    return;
  }
  for (const org of orgs) {
    const row = document.createElement("div");
    row.className = "share-grant-row";
    row.style.cursor = "pointer";
    row.innerHTML = `
      <span class="grant-email">${escapeHtml(org.name)}</span>
      <span class="room-card-meta">${escapeHtml(org.role)}</span>
    `;
    row.onclick = () => showOrgDetailView(org.org_id);
    list.appendChild(row);
  }
}

document.getElementById("newOrgBtn").onclick = async () => {
  const name = document.getElementById("newOrgName").value.trim();
  if (!name) return;
  const resp = await fetch("/api/orgs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!resp.ok) {
    const detail = (await resp.json().catch(() => ({}))).detail || resp.statusText;
    showToast(`Could not create organization: ${detail}`, "error");
    return;
  }
  document.getElementById("newOrgName").value = "";
  await loadOrgsList();
};

async function showOrgDetailView(orgId) {
  currentOrgId = orgId;
  document.getElementById("orgsListView").style.display = "none";
  document.getElementById("orgDetailView").style.display = "";
  await loadOrgDetail();
}

document.getElementById("orgDetailBackBtn").onclick = showOrgsListView;

async function loadOrgDetail() {
  if (!currentOrgId) return;
  const resp = await fetch(`/api/orgs/${encodeURIComponent(currentOrgId)}`);
  if (!resp.ok) {
    showToast("Could not load organization", "error");
    showOrgsListView();
    return;
  }
  const org = await resp.json();
  document.getElementById("orgsModalTitle").textContent = org.name;

  const me = await fetchAccount();
  const myMembership = org.members.find((m) => m.user_id === me.user?.user_id);
  const isAdmin = myMembership?.role === "admin";

  document.getElementById("orgDetailAdminControls").style.display = isAdmin ? "" : "none";
  document.getElementById("orgInviteRow").style.display = isAdmin ? "" : "none";
  if (isAdmin) {
    document.getElementById("orgDefaultVisibility").value = org.default_visibility;
    document.getElementById("orgAllowViewerLinks").checked = org.allowed_share_link_roles.includes("viewer");
    document.getElementById("orgAllowEditorLinks").checked = org.allowed_share_link_roles.includes("editor");
    await loadOrgSSO();
  }
  renderOrgMembers(org.members, isAdmin);
}

async function loadOrgSSO() {
  const resp = await fetch(`/api/orgs/${encodeURIComponent(currentOrgId)}/sso`);
  if (!resp.ok) return;
  const sso = await resp.json();
  document.getElementById("orgSSOStatus").textContent = sso.configured ? `(configured -- ${sso.domain})` : "(not configured)";
  document.getElementById("orgSSOIssuer").value = sso.issuer || "";
  document.getElementById("orgSSODomain").value = sso.domain || "";
  document.getElementById("orgSSOClientId").value = "";
  document.getElementById("orgSSOClientSecret").value = "";
}

document.getElementById("orgSSOSaveBtn").onclick = async () => {
  if (!currentOrgId) return;
  const resp = await fetch(`/api/orgs/${encodeURIComponent(currentOrgId)}/sso`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      issuer: document.getElementById("orgSSOIssuer").value.trim(),
      client_id: document.getElementById("orgSSOClientId").value.trim(),
      client_secret: document.getElementById("orgSSOClientSecret").value.trim(),
      domain: document.getElementById("orgSSODomain").value.trim(),
    }),
  });
  if (!resp.ok) {
    const detail = (await resp.json().catch(() => ({}))).detail || resp.statusText;
    showToast(`Could not save SSO configuration: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`, "error");
    return;
  }
  showToast("SSO configuration saved", "success");
  await loadOrgSSO();
};

document.getElementById("orgSSOClearBtn").onclick = async () => {
  if (!currentOrgId) return;
  if (!window.confirm("Clear this organization's SSO configuration? Sign-in from its captured domain will fall back to magic links / OAuth.")) return;
  await fetch(`/api/orgs/${encodeURIComponent(currentOrgId)}/sso`, { method: "DELETE" });
  await loadOrgSSO();
};

function renderOrgMembers(members, isAdmin) {
  const list = document.getElementById("orgMembersList");
  list.innerHTML = "";
  for (const m of members) {
    const row = document.createElement("div");
    row.className = "share-grant-row";
    const statusSuffix = m.status === "pending" ? " (invited, not yet joined)" : "";
    row.innerHTML = `
      <span class="grant-email" title="${escapeHtml(m.email)}">${escapeHtml(m.display_name || m.email)}${statusSuffix}</span>
    `;
    if (isAdmin) {
      const roleSelect = document.createElement("select");
      roleSelect.innerHTML = '<option value="member">Member</option><option value="admin">Admin</option>';
      roleSelect.value = m.role;
      roleSelect.onchange = () => setOrgMemberRole(m.user_id, roleSelect.value);
      row.appendChild(roleSelect);
      const removeBtn = document.createElement("button");
      removeBtn.className = "ghost-btn";
      removeBtn.title = "Remove from organization";
      removeBtn.setAttribute("aria-label", "Remove from organization");
      removeBtn.innerHTML = iconHtml("x");
      removeBtn.onclick = () => removeOrgMember(m.user_id);
      row.appendChild(removeBtn);
    } else {
      const roleLabel = document.createElement("span");
      roleLabel.className = "room-card-meta";
      roleLabel.textContent = m.role;
      row.appendChild(roleLabel);
    }
    list.appendChild(row);
  }
}

async function setOrgMemberRole(userId, role) {
  const resp = await fetch(`/api/orgs/${encodeURIComponent(currentOrgId)}/members/${encodeURIComponent(userId)}/role`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ role }),
  });
  if (!resp.ok) {
    const detail = (await resp.json().catch(() => ({}))).detail || resp.statusText;
    showToast(`Could not change role: ${detail}`, "error");
  }
  await loadOrgDetail();
}

async function removeOrgMember(userId) {
  const resp = await fetch(`/api/orgs/${encodeURIComponent(currentOrgId)}/members/${encodeURIComponent(userId)}`, {
    method: "DELETE",
  });
  if (!resp.ok) {
    const detail = (await resp.json().catch(() => ({}))).detail || resp.statusText;
    showToast(`Could not remove member: ${detail}`, "error");
  }
  await loadOrgDetail();
}

document.getElementById("orgInviteBtn").onclick = async () => {
  const email = document.getElementById("orgInviteEmail").value.trim();
  if (!email) return;
  const role = document.getElementById("orgInviteRole").value;
  const resp = await fetch(`/api/orgs/${encodeURIComponent(currentOrgId)}/invite`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, role }),
  });
  if (!resp.ok) {
    const detail = (await resp.json().catch(() => ({}))).detail || resp.statusText;
    showToast(`Could not invite: ${detail}`, "error");
    return;
  }
  document.getElementById("orgInviteEmail").value = "";
  await loadOrgDetail();
};

async function saveOrgDefaults() {
  if (!currentOrgId) return;
  const roles = [];
  if (document.getElementById("orgAllowViewerLinks").checked) roles.push("viewer");
  if (document.getElementById("orgAllowEditorLinks").checked) roles.push("editor");
  await fetch(`/api/orgs/${encodeURIComponent(currentOrgId)}/defaults`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      default_visibility: document.getElementById("orgDefaultVisibility").value,
      allowed_share_link_roles: roles,
    }),
  });
}
document.getElementById("orgDefaultVisibility").addEventListener("change", saveOrgDefaults);
document.getElementById("orgAllowViewerLinks").addEventListener("change", saveOrgDefaults);
document.getElementById("orgAllowEditorLinks").addEventListener("change", saveOrgDefaults);

document.getElementById("orgLeaveBtn").onclick = async () => {
  if (!currentOrgId) return;
  if (!window.confirm("Leave this organization?")) return;
  const resp = await fetch(`/api/orgs/${encodeURIComponent(currentOrgId)}/leave`, { method: "POST" });
  if (!resp.ok) {
    const detail = (await resp.json().catch(() => ({}))).detail || resp.statusText;
    showToast(`Could not leave: ${detail}`, "error");
    return;
  }
  showOrgsListView();
  await loadRooms();
};

document.getElementById("orgsBtn").onclick = openOrgsModal;
document.getElementById("orgsModalClose").onclick = closeOrgsModal;
document.getElementById("orgsModal").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) closeOrgsModal();
});

// -- new room ------------------------------------------------------------------

function createRoom(kind) {
  const name = window.prompt(`Name for the new ${kind === "mesh" ? "3D mesh" : "2D drawing"} room:`, "");
  if (name === null) return;
  const roomId = name.trim() || "room-" + Math.random().toString(36).slice(2, 8);
  location.href = roomHomeUrl(kind, roomId);
}

document.getElementById("newDrawingBtn").onclick = () => createRoom("drawing");
document.getElementById("newMeshBtn").onclick = () => createRoom("mesh");

// -- accounts (Part 6 P1) --------------------------------------------------------
// Everything below renders nothing at all on a tokens-mode deployment --
// the home page is byte-for-byte the pre-accounts experience there.

const signInModal = document.getElementById("signInModal");

function openSignInModal(oauthProviders) {
  document.getElementById("signInForm").style.display = "";
  document.getElementById("signInSent").style.display = "none";
  const oauthRow = document.getElementById("oauthButtons");
  oauthRow.innerHTML = "";
  if (oauthProviders && oauthProviders.length) {
    for (const provider of oauthProviders) {
      const btn = document.createElement("button");
      btn.textContent = `Continue with ${provider[0].toUpperCase()}${provider.slice(1)}`;
      btn.style.flex = "1";
      btn.onclick = () => { location.href = `/api/auth/oauth/${provider}/start`; };
      oauthRow.appendChild(btn);
    }
    oauthRow.style.display = "flex";
  } else {
    oauthRow.style.display = "none";
  }
  signInModal.style.display = "flex";
  document.getElementById("signInEmail").focus();
}

function closeSignInModal() { signInModal.style.display = "none"; }
document.getElementById("signInModalClose").onclick = closeSignInModal;
signInModal.addEventListener("click", (e) => { if (e.target === e.currentTarget) closeSignInModal(); });

async function requestSignInLink() {
  const email = document.getElementById("signInEmail").value.trim();
  if (!email) { document.getElementById("signInEmail").focus(); return; }
  const resp = await fetch("/api/auth/request-link", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  });
  if (!resp.ok) {
    const detail = (await resp.json().catch(() => ({}))).detail || resp.statusText;
    showToast(`Could not send link: ${detail}`, "error");
    return;
  }
  const result = await resp.json();
  document.getElementById("signInForm").style.display = "none";
  document.getElementById("signInSent").style.display = "";
  document.getElementById("signInSentTo").textContent = email;
  const devWrap = document.getElementById("signInDevLinkWrap");
  if (result.dev_link) {
    devWrap.style.display = "";
    document.getElementById("signInDevLink").href = result.dev_link;
  } else {
    devWrap.style.display = "none";
  }
}
document.getElementById("signInLinkBtn").onclick = requestSignInLink;
document.getElementById("signInEmail").addEventListener("keydown", (e) => {
  if (e.key === "Enter") requestSignInLink();
});

async function signOut(everywhere) {
  await fetch(everywhere ? "/api/auth/logout-everywhere" : "/api/auth/logout", { method: "POST" });
  if (everywhere) await fetch("/api/auth/logout", { method: "POST" }); // drop this session's cookie too
  location.reload();
}

function renderAccountArea(acct) {
  const area = document.getElementById("accountArea");
  const orgsBtn = document.getElementById("orgsBtn");
  const adminLink = document.getElementById("adminLink");
  const notificationsBtn = document.getElementById("notificationsBtn");
  area.innerHTML = "";
  if (acct.mode !== "accounts") {
    orgsBtn.style.display = "none";
    adminLink.style.display = "none";
    notificationsBtn.style.display = "none";
    return;
  }
  adminLink.style.display = acct.signed_in && acct.is_platform_admin ? "" : "none";
  if (!acct.signed_in) {
    orgsBtn.style.display = "none";
    notificationsBtn.style.display = "none";
    const btn = document.createElement("button");
    btn.id = "signInBtn";
    btn.textContent = "Sign in";
    btn.onclick = () => openSignInModal(acct.oauth_providers);
    area.appendChild(btn);
    return;
  }
  orgsBtn.style.display = ""; // Part 6 P3: organizations need a real account, same gate as everything else here
  notificationsBtn.style.display = ""; // Part 6 P5: @mentions land here, real account only
  refreshNotificationsBadge();
  const chip = document.createElement("button");
  chip.id = "accountChip";
  chip.className = "status-pill";
  chip.title = `Signed in as ${acct.user.email} -- click to sign out`;
  chip.textContent = acct.user.display_name || acct.user.email;
  chip.onclick = () => {
    if (window.confirm(`Sign out of ${acct.user.email}?`)) signOut(false);
  };
  area.appendChild(chip);
}

fetchAccount().then((acct) => {
  renderAccountArea(acct);
  syncActorNameFromAccount();
});

// -- notifications (Part 6 P5) ---------------------------------------------------
// A workspace-wide bell, not room-scoped like comments/activity -- an
// @mention can happen in any room this account can reach, so this polls
// independently of whichever room (if any) is currently open elsewhere.

async function refreshNotificationsBadge() {
  const resp = await fetch("/api/notifications?unread_only=true");
  if (!resp.ok) return;
  const body = await resp.json();
  const btn = document.getElementById("notificationsBtn");
  btn.textContent = body.unread_count > 0 ? `Notifications (${body.unread_count})` : "Notifications";
}
setInterval(refreshNotificationsBadge, 30000);

function renderNotificationRow(n) {
  const row = document.createElement("div");
  row.className = "comment-row";
  if (!n.read) row.style.background = "var(--accent-muted)";
  const roomId = n.payload.room_id;
  const roomKind = n.payload.room_kind;
  const label =
    n.kind === "mention"
      ? `<b>${escapeHtml(n.payload.from_display_name || "Someone")}</b> mentioned you: "${escapeHtml(n.payload.text || "")}"`
      : escapeHtml(n.kind);
  row.innerHTML = `<div style="flex:1;cursor:pointer">${label}<div class="room-card-meta">${relativeTime(n.created_at)}</div></div>`;
  row.querySelector("div").onclick = async () => {
    if (!n.read) await fetch(`/api/notifications/${n.notification_id}/read`, { method: "POST" });
    if (roomId && roomKind) location.href = roomHomeUrl(roomKind, roomId);
  };
  return row;
}

async function openNotificationsModal() {
  const modal = document.getElementById("notificationsModal");
  const body = document.getElementById("notificationsListBody");
  modal.style.display = "flex";
  const resp = await fetch("/api/notifications");
  if (!resp.ok) return;
  const data = await resp.json();
  body.innerHTML = "";
  if (data.notifications.length === 0) {
    body.innerHTML = '<div class="empty-hint">No notifications yet.</div>';
    return;
  }
  for (const n of data.notifications) body.appendChild(renderNotificationRow(n));
}

function closeNotificationsModal() {
  document.getElementById("notificationsModal").style.display = "none";
  refreshNotificationsBadge();
}

document.getElementById("notificationsBtn").onclick = openNotificationsModal;
document.getElementById("notificationsModalClose").onclick = closeNotificationsModal;
document.getElementById("notificationsModal").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) closeNotificationsModal();
});
document.getElementById("notificationsMarkAllBtn").onclick = async () => {
  await fetch("/api/notifications/read-all", { method: "POST" });
  await openNotificationsModal();
  await refreshNotificationsBadge();
};

loadRooms();
