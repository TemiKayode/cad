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
  const { kind, room_id: roomId, display_name: displayName, updated_at: updatedAt } = room;
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
  body.innerHTML = `
    <span class="room-kind-badge ${kind}">${kindLabel}</span>
    <span class="room-card-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
    <span class="room-card-meta">room: ${escapeHtml(roomId)}</span>
    <span class="room-card-meta">updated ${relativeTime(updatedAt)}</span>
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

let renameTarget = null; // { kind, roomId }

function openRenameModal(kind, roomId, currentName) {
  renameTarget = { kind, roomId };
  document.getElementById("renameInput").value = currentName;
  document.getElementById("renameModal").style.display = "flex";
  document.getElementById("renameInput").focus();
}

function closeRenameModal() {
  document.getElementById("renameModal").style.display = "none";
  renameTarget = null;
}

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

async function openHistoryModal(kind, roomId, name) {
  document.getElementById("historyModalTitle").textContent = `Version history -- ${name}`;
  const body = document.getElementById("historyModalBody");
  body.innerHTML = '<div class="empty-hint">Loading...</div>';
  document.getElementById("historyModal").style.display = "flex";

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
    document.getElementById("historyModal").style.display = "none";
    location.href = roomHomeUrl(kind, newRoomId);
  } catch (err) {
    window.alert(`Restore failed: ${err.message}`);
  }
}

document.getElementById("historyModalClose").onclick = () => {
  document.getElementById("historyModal").style.display = "none";
};

// -- new room ------------------------------------------------------------------

function createRoom(kind) {
  const name = window.prompt(`Name for the new ${kind === "mesh" ? "3D mesh" : "2D drawing"} room:`, "");
  if (name === null) return;
  const roomId = name.trim() || "room-" + Math.random().toString(36).slice(2, 8);
  location.href = roomHomeUrl(kind, roomId);
}

document.getElementById("newDrawingBtn").onclick = () => createRoom("drawing");
document.getElementById("newMeshBtn").onclick = () => createRoom("mesh");

loadRooms();
