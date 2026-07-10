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
    visibility, owner_display_name: ownerDisplayName, your_role: yourRole,
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
  const sharedByMeta = yourRole && yourRole !== "owner" && ownerDisplayName
    ? `<span class="room-card-meta">shared by ${escapeHtml(ownerDisplayName)} -- your role: ${yourRole}</span>` : "";
  body.innerHTML = `
    <span class="room-kind-badge ${kind}">${kindLabel}</span>
    ${visBadge}
    <span class="room-card-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
    <span class="room-card-meta">room: ${escapeHtml(roomId)}</span>
    <span class="room-card-meta">updated ${relativeTime(updatedAt)}</span>
    ${sharedByMeta}
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
}

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
  area.innerHTML = "";
  if (acct.mode !== "accounts") return;
  if (!acct.signed_in) {
    const btn = document.createElement("button");
    btn.id = "signInBtn";
    btn.textContent = "Sign in";
    btn.onclick = () => openSignInModal(acct.oauth_providers);
    area.appendChild(btn);
    return;
  }
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

loadRooms();
