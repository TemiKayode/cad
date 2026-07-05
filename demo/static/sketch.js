// 2D collaborative sketch demo built directly on the crdt_cad.crdt.document
// wire protocol. See common.js for the WebSocket relay client and the
// design note on why merge logic stays server-side.

initThemeToggle();
initTooltips();
initPanelCollapse();

const actorId = getOrCreateActorId();
let actorName = getOrCreateActorName();
const actorColor = colorForActor(actorId);
const room = new URLSearchParams(location.search).get("room") || "demo";
document.getElementById("roomInput").value = room;
document.getElementById("actorLabel").textContent = `${actorName} (${actorId})`;

// Phase 17 read-only share links: true once the server's own snapshot/delta
// reply says this connection is a "viewer" (see RelayConnection's onRole) --
// gates the canvas pointerdown handler further down so a viewer can pan/zoom
// but never start an edit gesture.
let viewerMode = false;

const clock = new LocalClock(actorId);
const rid = () => Math.random().toString(36).slice(2, 10);

const state = {
  layers: new Map(),      // id -> {name, ...}
  layerOrder: [],
  pathIndex: new Set(),
  pathProps: new Map(),   // id -> {layer_id, color, stroke_width, [shape props]}
  pathNodes: new Map(),   // id -> [{id,o,v,db}]  (already in document order)
  comments: new Map(),
  presence: new Map(),
  settings: new Map(),    // "units" | "grid_spacing" | "snap_step" -> value (Phase 11)
  dimensions: new Map(),  // dim_id -> {a_path, a_node, b_path, b_node, offset} (Phase 13)
  constraints: new Map(), // constraint_id -> {kind, anchors, param} (Phase 14)
  groups: new Set(),      // group_id -> just existence, like layers (Phase 15)
};

// -- document units (Phase 11) ------------------------------------------------
// Stored/CRDT geometry is always raw px-equivalent world units, regardless
// of this setting -- "units" is a *display*-layer conversion (cursor
// readout, shape numeric input, SVG/DXF export scale via the server's own
// identical table in crdt_cad.crdt.document), never a migration of
// existing coordinates. Mirrors UNITS_PX_PER_UNIT in document.py exactly.
const UNITS_PX_PER_UNIT = { px: 1.0, mm: 96.0 / 25.4, in: 96.0 };
function currentUnits() {
  return state.settings.get("units") || "px";
}
function pxPerUnit() {
  return UNITS_PX_PER_UNIT[currentUnits()] || 1.0;
}
function toDisplayUnits(px) {
  return px / pxPerUnit();
}
function fromDisplayUnits(value) {
  return value * pxPerUnit();
}
/** Area scales as length squared, so its unit conversion factor is the
 * square of the linear one -- used by the Measure tool's Area/Perimeter
 * mode (Phase 13). */
function toDisplayUnitsArea(px2) {
  return px2 / (pxPerUnit() * pxPerUnit());
}
function unitSuffix() {
  return currentUnits() === "px" ? "" : currentUnits();
}

const ui = {
  tool: "pen",
  activeLayer: null,
  hiddenLayers: new Set(),
  selectedPaths: new Set(), // Phase 12: multi-selection (marquee + shift-click)
  opsCount: 0,
};

// -- selection helpers (Phase 12) ----------------------------------------------

function selectOnly(pathId) {
  ui.selectedPaths = pathId ? new Set([pathId]) : new Set();
}
function toggleSelection(pathId) {
  if (ui.selectedPaths.has(pathId)) ui.selectedPaths.delete(pathId);
  else ui.selectedPaths.add(pathId);
}
/** The single selected path when there's exactly one -- used by panels
 * (color/width, comments) that only make sense for one path at a time;
 * a multi-selection shows a different, bulk-action panel instead (see
 * renderSelectionPanel). */
function primarySelectedPath() {
  return ui.selectedPaths.size === 1 ? [...ui.selectedPaths][0] : null;
}
const undoStack = [];
const redoStack = [];
let pendingPolygon = [];
// -- interactive constraint solver UI (Phase 9) -----------------------------
// Up to 2 {pathId, nodeId, pos} entries -- see the "constrain" tool's click
// handler and applyConstraint() below. The already-tested `/api/solve`
// endpoint (crdt_cad.geometry.constraints) does the actual solving; this
// is purely a client-side workflow on top of it, same as every other
// edit here -- points move via the exact same delete+insert path_geom
// ops any other point move would use (see movePathPoint), broadcast the
// normal way so everyone in the room sees it.
let constraintSelection = [];
// Phase 14: dragging a point while the Constrain tool is active
// re-solves any persisted constraints touching its path on release --
// null, or {pathId, nodeId, startWorld, livePos, moved}. See the
// pointerdown/move/up wiring and commitConstrainedPointDrag below.
let constrainDrag = null;
let justDraggedConstraintPoint = false;
let lastMousePt = null; // world coordinates -- see the view transform section below

// -- measure tool (Phase 13, read-only, client-local) -----------------------
// Distance/Angle pick up to 2 {pathId, nodeId, pos} points, same shape as
// constraintSelection above (and reuses findAdjacentPoint for Angle, the
// same neighbor-inference Constrain's parallel/perpendicular already use).
// Area/Perimeter instead picks one whole path/shape directly -- no CRDT
// ops are ever sent for any of this, it's purely a local readout.
let measureMode = "distance"; // "distance" | "angle" | "area"
let measureSelection = [];
let measureResult = null; // {text} or null

// -- dimension annotations (Phase 13, persistent, shared) --------------------
// Up to 2 {pathId, nodeId, pos} entries, same picking UX as Constrain --
// committing a dimension (see commitDimension) sends a real "dimension"
// op so every collaborator sees it, unlike the ephemeral Measure tool
// above.
let dimensionSelection = [];

// Phase 11 shape primitives: {kind, anchor:[wx,wy], current:[wx,wy]} while
// a shape tool's click-drag gesture is in progress, else null -- see the
// "shape primitives" section further down for the full design.
let shapeDraft = null;

// Phase 12 selection editing: null, or one of
//   {mode:"move", startWorld, selection:Set<pathId>, primaryId,
//    primaryPivotWorld0:[wx,wy], liveDelta:[dx,dy], moved:bool}
//   {mode:"marquee", startScreen:[sx,sy], currentScreen:[sx,sy], additive:bool}
// -- see the "selection dragging" section further down.
let selectDrag = null;
// {pt:[wx,wy], kind:"endpoint"|"midpoint"|"center"} while the cursor is
// snapped to nearby geometry (object snapping, Phase 12), else null.
let activeSnapGlyph = null;
let shortcutOverlayEl = null;

// -- view transform (Phase 10) ------------------------------------------------
// Pan (panX/panY, screen pixels the world origin is offset by) + zoom are
// purely client-local UI state -- never synced, never touching the CRDT --
// per the brief: "the view transform is client-local state -- it is not
// CRDT data and must not sync." All stored/sent geometry (path points,
// presence cursors) is in *world* coordinates; only rendering and input
// mapping go through this transform. A fresh view (panX=0, panY=0, zoom=1)
// maps world 1:1 to screen pixels, so every pre-existing room's pixel-space
// data renders exactly as it always did -- world space is a strict superset.
const view = { panX: 0, panY: 0, zoom: 1 };
let snapToGridEnabled = false;

function screenToWorld(sx, sy) {
  return [(sx - view.panX) / view.zoom, (sy - view.panY) / view.zoom];
}
function worldToScreen(wx, wy) {
  return [wx * view.zoom + view.panX, wy * view.zoom + view.panY];
}

/** Picks a "nice" world-space grid step (1/2/5 x10^n *in the current
 * document unit*, then converted back to world px) so its on-screen
 * spacing stays in a comfortable, zoom-independent pixel range -- used
 * for both grid rendering and snap-to-grid, so snapping always matches
 * whatever grid is currently visible. Unit-aware (Phase 11): with
 * units="mm", the grid lands on nice round millimeters, not nice round
 * pixels that happen to look reasonable on screen. */
function pickGridStep(zoom) {
  const ppu = pxPerUnit();
  const targetScreenPx = 60;
  const rawStepInUnits = targetScreenPx / zoom / ppu;
  const magnitude = Math.pow(10, Math.floor(Math.log10(rawStepInUnits)));
  const residual = rawStepInUnits / magnitude;
  let nice;
  if (residual < 1.5) nice = 1;
  else if (residual < 3.5) nice = 2;
  else if (residual < 7.5) nice = 5;
  else nice = 10;
  return nice * magnitude * ppu;
}

function snapWorldPoint([wx, wy]) {
  if (!snapToGridEnabled) return [wx, wy];
  const step = pickGridStep(view.zoom);
  return [Math.round(wx / step) * step, Math.round(wy / step) * step];
}

// -- wire <-> local state -----------------------------------------------------

/** Bumps `clock` past every OpId found in a freshly-loaded snapshot -- see
 * the identical helper (and the LWW tie-break bug it fixes) in mesh3d.js /
 * LocalClock.observe() in common.js for the full rationale. Without this, a
 * fresh client's first edit to something another actor already touched at a
 * higher counter (e.g. a bulk SVG/DXF import) would silently lose. */
function observeSnapshotCounters(doc) {
  let maxCounter = 0;
  const bump = (idPair) => { if (idPair && idPair[0] > maxCounter) maxCounter = idPair[0]; };
  const scanEntries = (lww) => { if (lww) for (const e of lww.entries) bump(e.id); };
  scanEntries(doc.layers);
  for (const m of Object.values(doc.layer_props || {})) scanEntries(m);
  scanEntries(doc.path_index);
  for (const m of Object.values(doc.path_props || {})) scanEntries(m);
  for (const rga of Object.values(doc.paths || {})) {
    for (const n of rga.nodes) { bump(n.id); bump(n.db); }
  }
  scanEntries(doc.comments);
  scanEntries(doc.presence);
  scanEntries(doc.settings);
  scanEntries(doc.dimensions);
  scanEntries(doc.constraints);
  scanEntries(doc.groups);
  clock.observe(maxCounter);
}

function loadSnapshot(doc) {
  observeSnapshotCounters(doc);
  state.layers.clear();
  state.layerOrder = [];
  for (const e of doc.layers.entries) {
    if (!e.d) {
      if (!state.layers.has(e.k)) state.layerOrder.push(e.k);
      state.layers.set(e.k, {});
    }
  }
  for (const [lid, m] of Object.entries(doc.layer_props)) {
    if (!state.layers.has(lid)) continue;
    const props = {};
    for (const e of m.entries) if (!e.d) props[e.k] = e.v;
    state.layers.set(lid, props);
  }
  state.pathIndex = new Set(doc.path_index.entries.filter((e) => !e.d).map((e) => e.k));
  state.pathProps.clear();
  for (const [pid, m] of Object.entries(doc.path_props)) {
    const props = {};
    for (const e of m.entries) if (!e.d) props[e.k] = e.v;
    state.pathProps.set(pid, props);
  }
  state.pathNodes.clear();
  for (const [pid, rga] of Object.entries(doc.paths)) {
    state.pathNodes.set(pid, rga.nodes.slice());
  }
  state.comments.clear();
  for (const e of doc.comments.entries) if (!e.d) state.comments.set(e.k, e.v);
  state.presence.clear();
  for (const e of doc.presence.entries) if (!e.d) state.presence.set(e.k, e.v);
  state.settings.clear();
  for (const e of doc.settings.entries) if (!e.d) state.settings.set(e.k, e.v);
  state.dimensions.clear();
  for (const e of doc.dimensions.entries) if (!e.d) state.dimensions.set(e.k, e.v);
  state.constraints.clear();
  for (const e of doc.constraints.entries) if (!e.d) state.constraints.set(e.k, e.v);
  state.groups = new Set(doc.groups.entries.filter((e) => !e.d).map((e) => e.k));

  if (!ui.activeLayer || !state.layers.has(ui.activeLayer)) {
    ui.activeLayer = state.layerOrder[0] || null;
  }
  syncUnitsSelect();
  renderAll();
}

function applyOp(op) {
  const p = op.payload;
  if (p && p.id) clock.observe(p.id[0]);
  if (op.target === "layer") {
    if (!p.d) {
      if (!state.layers.has(p.k)) {
        state.layerOrder.push(p.k);
        state.layers.set(p.k, {});
        if (!ui.activeLayer) ui.activeLayer = p.k;
      }
    } else {
      state.layers.delete(p.k);
      state.layerOrder = state.layerOrder.filter((id) => id !== p.k);
    }
  } else if (op.target === "layer_prop") {
    const props = state.layers.get(op.scope) || {};
    if (!p.d) props[p.k] = p.v; else delete props[p.k];
    state.layers.set(op.scope, props);
  } else if (op.target === "path_index") {
    if (!p.d) state.pathIndex.add(p.k); else state.pathIndex.delete(p.k);
  } else if (op.target === "path_prop") {
    const props = state.pathProps.get(op.scope) || {};
    if (!p.d) props[p.k] = p.v; else delete props[p.k];
    state.pathProps.set(op.scope, props);
  } else if (op.target === "path_geom") {
    applyGeomOp(op.scope, p);
  } else if (op.target === "comment") {
    if (!p.d) state.comments.set(p.k, p.v); else state.comments.delete(p.k);
  } else if (op.target === "presence") {
    if (!p.d) {
      state.presence.set(p.k, p.v);
      p2p.maybeConnectTo(p.k);
    } else {
      state.presence.delete(p.k);
    }
  } else if (op.target === "setting") {
    if (!p.d) state.settings.set(p.k, p.v); else state.settings.delete(p.k);
    syncUnitsSelect();
  } else if (op.target === "dimension") {
    if (!p.d) state.dimensions.set(p.k, p.v); else state.dimensions.delete(p.k);
  } else if (op.target === "constraint") {
    if (!p.d) state.constraints.set(p.k, p.v); else state.constraints.delete(p.k);
  } else if (op.target === "group") {
    if (!p.d) state.groups.add(p.k); else state.groups.delete(p.k);
  }
}

function applyGeomOp(pathId, payload) {
  let nodes = state.pathNodes.get(pathId);
  if (!nodes) { nodes = []; state.pathNodes.set(pathId, nodes); }
  applyRgaOp(nodes, payload);
}

function pathPoints(pathId) {
  return liveValues(state.pathNodes.get(pathId));
}

/** Mirrors crdt_cad.crdt.document.curve_prop_key -- the path_prop key a
 * curve segment (Phase 8) arriving at this anchor point is stored under,
 * server-side and here alike. See render()'s path-drawing loop. */
function curvePropKey(nodeId) {
  return `curve:${opIdKey(nodeId)}`;
}

/** Undoes a locally-optimistic apply for an op the server refused (the
 * geometry validity gate rejected it). Only path_geom inserts can be
 * rejected today -- see _validate_op server-side. */
function revertRejectedOp(op) {
  if (op.target !== "path_geom" || op.payload.t !== "ins") return;
  const nodes = state.pathNodes.get(op.scope);
  if (!nodes) return;
  const idx = nodes.findIndex((n) => idEq(n.id, op.payload.id));
  if (idx !== -1) nodes.splice(idx, 1);
}

function applyIncomingOps(ops) {
  for (const op of ops) applyOp(op);
  ui.opsCount += ops.length;
  renderAll();
}

// -- relay connection -----------------------------------------------------------

// `conn`/`p2p` are assigned inside the async bootstrap below (ensureRoomAccess
// awaits an /api/auth/required check, and possibly a passphrase prompt,
// before a token -- or null, on the zero-config default -- is available).
// Every reference elsewhere in this file is inside a function or event
// handler, so it only runs well after this has resolved.
let conn, p2p;

(async () => {
  const token = await ensureRoomAccess("drawing", room);
  const persistedOutbox = await loadPersistedOutbox("drawing", room, actorId);
  if (persistedOutbox.length) {
    showToast(`Recovered ${persistedOutbox.length} offline edit(s) from before this page loaded`, "info");
  }
  conn = new RelayConnection(`/ws/${encodeURIComponent(room)}`, actorId, {
    onSnapshot: (doc) => {
      loadSnapshot(doc);
      // Replay recovered-but-unsent edits on top of the fresh snapshot --
      // the server never echoes ops back to the actor that sent them, so
      // without this, edits recovered from before a hard refresh would be
      // silently missing from view even though they get flushed and
      // correctly persisted server-side.
      for (const op of persistedOutbox) applyOp(op);
      if (persistedOutbox.length) renderAll();
    },
    onDelta: (ops) => applyIncomingOps(ops),
    onOps: (ops) => applyIncomingOps(ops),
    onStatus: (status) => setStatus(status),
    onRejected: (reason, op) => {
      revertRejectedOp(op);
      renderAll();
      showToast(`Rejected: ${reason}`, "error");
    },
    onSaved: () => showToast("Saved", "success"),
    onMergePreview: (mine, theirs, proceed) => showMergePreviewModal(mine, theirs, describeDocOps, proceed),
    onRole: (role) => {
      viewerMode = applyViewerModeUI(role);
    },
    token,
    kind: "drawing",
    room,
    initialOutbox: persistedOutbox,
  });

  p2p = new P2PManager(conn, actorId, {
    onPeerData: (_peerActorId, ops) => applyIncomingOps(ops),
    onPeerStatus: () => updateP2pPill(),
  });
})();

function updateP2pPill() {
  const pill = document.getElementById("p2pPill");
  const count = p2p.connectedCount;
  if (count > 0) {
    pill.style.display = "";
    pill.className = "status-pill online";
    document.getElementById("p2pText").textContent = `P2P ×${count}`;
  } else {
    pill.style.display = "none";
  }
}

function sendOps(ops) {
  if (viewerMode) {
    // Robust chokepoint: every mutating code path in this file (tool
    // gestures, keyboard shortcuts, bulk-action buttons, import, AI
    // generate) funnels through this one function before anything is
    // transmitted -- gating here (rather than only at each call site)
    // is what guarantees a viewer's optimistic local edit never leaks
    // out over *either* channel, including the direct WebRTC P2P path
    // conn.send()'s own viewer guard can't see at all.
    console.warn("sendOps() called while connected as a read-only viewer -- dropped, not sent");
    return;
  }
  ui.opsCount += ops.length;
  conn.send(ops);
  if (!conn.userWantsOffline) p2p.broadcastOps(ops);
}

function setStatus(status) {
  const pill = document.getElementById("statusPill");
  pill.className = `status-pill ${status}`;
  document.getElementById("statusText").textContent = status;
  document.getElementById("offlineToggle").textContent = status === "offline" ? "Reconnect" : "Go offline";
  if (status === "unauthorized") {
    // The token we had (or lack thereof) was rejected -- clear it and
    // re-prompt rather than let RelayConnection keep retrying with the
    // same bad token forever (see the WS_CLOSE_UNAUTHORIZED handling in
    // common.js). Also strip any ?token= from the URL first: otherwise
    // ensureRoomAccess() would just re-adopt the same just-proven-bad
    // token from the URL again instead of ever reaching the re-prompt.
    clearRoomToken("drawing", room);
    const url = new URL(location.href);
    url.searchParams.delete("token");
    history.replaceState({}, "", url);
    showToast("Incorrect or expired room secret -- please try again", "error");
    ensureRoomAccess("drawing", room).then((token) => {
      conn.token = token;
      conn._connect();
    });
  }
}

document.getElementById("offlineToggle").onclick = () => {
  if (conn.userWantsOffline) {
    conn.goOnline();
  } else {
    conn.goOffline();
    p2p.disconnectAll(); // otherwise an already-open P2P data channel would keep syncing behind the WS's back
    updateP2pPill();
  }
};

document.getElementById("roomInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    const v = e.target.value.trim() || "demo";
    location.search = `?room=${encodeURIComponent(v)}`;
  }
});

// -- save / download / import / share --------------------------------------------

document.getElementById("saveBtn").onclick = () => conn.save();

function triggerDownload(url) {
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}
document.getElementById("downloadJsonBtn").onclick = () =>
  triggerDownload(withToken(`/api/rooms/${encodeURIComponent(room)}/export/json`, "drawing", room));
document.getElementById("downloadSvgBtn").onclick = () =>
  triggerDownload(withToken(`/api/rooms/${encodeURIComponent(room)}/export/svg`, "drawing", room));
document.getElementById("downloadDxfBtn").onclick = () =>
  triggerDownload(withToken(`/api/rooms/${encodeURIComponent(room)}/export/dxf`, "drawing", room));

// -- PNG export (Phase 15) -------------------------------------------------------
// Purely client-side (canvas.toBlob()) -- no server work needed, per the
// brief. Unlike JSON/SVG/DXF's server-served attachments (which carry
// their own filename via Content-Disposition, so triggerDownload's
// download="" defers to it), a blob: URL has no filename of its own, so
// this sets one explicitly instead of reusing triggerDownload.
function triggerBlobDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

function downloadCanvasAsPng(filename) {
  canvas.toBlob((blob) => {
    if (!blob) { showToast("PNG export failed", "error"); return; }
    triggerBlobDownload(blob, filename);
  }, "image/png");
}

document.getElementById("downloadPngBtn").onclick = () => downloadCanvasAsPng(`${room}.png`);
document.getElementById("downloadPngFitBtn").onclick = () => {
  // Fit-to-content variant: temporarily re-frame the view, capture, then
  // restore the user's actual pan/zoom -- canvas.toBlob() is async, so
  // the restore must happen *inside* its callback, after the capture has
  // actually read the canvas, not right after the (synchronous)
  // fitToContent() call returns.
  const savedView = { ...view };
  fitToContent();
  canvas.toBlob((blob) => {
    if (blob) triggerBlobDownload(blob, `${room}-fit.png`);
    else showToast("PNG export failed", "error");
    view.panX = savedView.panX;
    view.panY = savedView.panY;
    view.zoom = savedView.zoom;
    render();
    updateZoomIndicator();
  }, "image/png");
};

document.getElementById("importBtn").onclick = () => document.getElementById("importFileInput").click();
document.getElementById("importFileInput").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const ext = file.name.split(".").pop().toLowerCase();
  if (ext !== "svg" && ext !== "dxf") {
    showToast("Only .svg or .dxf files are supported", "error");
    e.target.value = "";
    return;
  }
  const body = ext === "svg" ? await file.text() : await file.arrayBuffer();
  try {
    const resp = await fetch(withToken(`/api/rooms/${encodeURIComponent(room)}/import/${ext}`, "drawing", room), { method: "POST", body });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    const result = await resp.json();
    showToast(`Imported ${result.path_count} path(s)`, "success");
  } catch (err) {
    showToast(`Import failed: ${err.message}`, "error");
  }
  e.target.value = "";
});

document.getElementById("shareBtn").onclick = async () => {
  let url = `${location.origin}/2d?room=${encodeURIComponent(room)}`;
  const token = roomTokenFor("drawing", room);
  if (token) url += `&token=${encodeURIComponent(token)}`;
  try {
    await navigator.clipboard.writeText(url);
    showToast("Invite link copied to clipboard", "success");
  } catch (err) {
    showToast(url, "info");
  }
};

document.getElementById("shareViewOnlyBtn").onclick = async () => {
  // Phase 17: mints a fresh viewer-role token via the dedicated share-link
  // endpoint (needs *editor* access to this room, so a viewer can't mint
  // themselves -- or anyone else -- an escalated link) and copies an
  // invite URL carrying it, instead of the current session's own token
  // (which is whatever role this browser already holds).
  try {
    const resp = await fetch(withToken(`/api/rooms/${encodeURIComponent(room)}/share-link`, "drawing", room), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role: "viewer" }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    const { token } = await resp.json();
    const url = `${location.origin}/2d?room=${encodeURIComponent(room)}&token=${encodeURIComponent(token)}`;
    try {
      await navigator.clipboard.writeText(url);
      showToast("Read-only invite link copied to clipboard", "success");
    } catch {
      showToast(url, "info");
    }
  } catch (err) {
    showToast(`Could not create a view-only link: ${err.message}`, "error");
  }
};

document.getElementById("renameActorBtn").onclick = () => {
  const next = window.prompt("Your display name (shown to collaborators):", actorName);
  const updated = setActorName(next);
  if (!updated) return;
  actorName = updated;
  document.getElementById("actorLabel").textContent = `${actorName} (${actorId})`;
};

// -- document name (Phase D2 top bar; reuses Phase 17's rename endpoint) -------

const docNameBtn = document.getElementById("docNameBtn");

async function refreshDocName() {
  try {
    const resp = await fetch("/api/workspace/rooms");
    const rows = await resp.json();
    const row = rows.find((r) => r.kind === "drawing" && r.room_id === room);
    docNameBtn.textContent = (row && row.display_name) || room;
  } catch {
    docNameBtn.textContent = room;
  }
}
refreshDocName();

docNameBtn.onclick = async () => {
  const current = docNameBtn.textContent;
  const next = window.prompt("Rename this room:", current);
  if (next === null || !next.trim() || next.trim() === current) return;
  try {
    await renameRoom("drawing", room, next.trim(), conn);
    docNameBtn.textContent = next.trim();
    showToast("Renamed", "success");
  } catch (err) {
    showToast(`Rename failed: ${err.message}`, "error");
  }
};

// -- local mutation helpers (mirrors crdt_cad.crdt.document.DrawingDocument) ---

function addLayer(name) {
  const id = "layer_" + rid();
  const ops = [];
  let op = { target: "layer", payload: lwwOp(clock.tick(), id, true, false) };
  applyOp(op); ops.push(op);
  op = { target: "layer_prop", scope: id, payload: lwwOp(clock.tick(), "name", name, false) };
  applyOp(op); ops.push(op);
  sendOps(ops);
  return id;
}

function addPath(layerId, points, color, width, extraProps = {}) {
  const id = "path_" + rid();
  const ops = [];
  let op = { target: "path_index", payload: lwwOp(clock.tick(), id, true, false) };
  applyOp(op); ops.push(op);
  const propEntries = [["layer_id", layerId], ["color", color], ["stroke_width", width], ...Object.entries(extraProps)];
  for (const [k, v] of propEntries) {
    op = { target: "path_prop", scope: id, payload: lwwOp(clock.tick(), k, v, false) };
    applyOp(op); ops.push(op);
  }
  let prevId = null;
  const pointIds = [];
  for (const pt of points) {
    const insId = clock.tick();
    op = { target: "path_geom", scope: id, payload: rgaInsertOp(insId, prevId, pt) };
    applyOp(op); ops.push(op);
    prevId = insId;
    pointIds.push(insId);
  }
  sendOps(ops);
  undoStack.push({ kind: "path_add", pathId: id });
  redoStack.length = 0;
  return { id, lastPointId: prevId, pointIds };
}

function appendPoint(pathId, lastPointId, pt) {
  const insId = clock.tick();
  const op = { target: "path_geom", scope: pathId, payload: rgaInsertOp(insId, lastPointId, pt) };
  applyOp(op);
  sendOps([op]);
  return insId;
}

function removePath(pathId) {
  const op = { target: "path_index", payload: lwwOp(clock.tick(), pathId, null, true) };
  applyOp(op);
  sendOps([op]);
  undoStack.push({ kind: "path_remove", pathId });
  redoStack.length = 0;
  ui.selectedPaths.delete(pathId);
}

function setPathProp(pathId, key, value) {
  const props = state.pathProps.get(pathId) || {};
  const hadPrevious = key in props;
  const previous = props[key];
  const op = { target: "path_prop", scope: pathId, payload: lwwOp(clock.tick(), key, value, false) };
  applyOp(op);
  sendOps([op]);
  undoStack.push({ kind: "prop_set", pathId, key, previous, hadPrevious, forwardValue: value });
  redoStack.length = 0;
}

function addComment(pathId, text) {
  const id = "comment_" + rid();
  const op = { target: "comment", payload: lwwOp(clock.tick(), id, { path_id: pathId, point_index: 0, text, author: actorName }, false) };
  applyOp(op);
  sendOps([op]);
}

function removeComment(id) {
  const op = { target: "comment", payload: lwwOp(clock.tick(), id, null, true) };
  applyOp(op);
  sendOps([op]);
}

// -- dimension annotations (Phase 13) --------------------------------------------
// A dimension references its two anchors by (path_id, node_id) -- the
// stable RGA node id, not a point_index -- so it keeps resolving to the
// *correct* point even after a concurrent insert/delete elsewhere in the
// same path. Mirrors crdt_cad.crdt.document.DrawingDocument.add_dimension
// exactly; see that function's docstring for the full rationale.

function commitDimension(a, b, offset = 30) {
  const id = "dim_" + rid();
  const op = {
    target: "dimension",
    payload: lwwOp(clock.tick(), id, { a_path: a.pathId, a_node: a.nodeId, b_path: b.pathId, b_node: b.nodeId, offset }, false),
  };
  applyOp(op);
  sendOps([op]);
  renderAll();
}

function removeDimension(id) {
  const op = { target: "dimension", payload: lwwOp(clock.tick(), id, null, true) };
  applyOp(op);
  sendOps([op]);
}

// -- document settings (Phase 11: units, grid/snap) -----------------------------

function setSetting(key, value) {
  const op = { target: "setting", payload: lwwOp(clock.tick(), key, value, false) };
  applyOp(op);
  sendOps([op]);
}

function syncUnitsSelect() {
  const select = document.getElementById("unitsSelect");
  if (select && select.value !== currentUnits()) select.value = currentUnits();
}
document.getElementById("unitsSelect").onchange = (e) => setSetting("units", e.target.value);

// -- undo / redo: fresh inverted ops each time, not snapshots --------------------

function undo() {
  const entry = undoStack.pop();
  if (!entry) return;
  if (entry.kind === "point_move") {
    // Point moves (Phase 14: constraint solves, and the constrain
    // tool's own point-drag) mint a *new* RGA node id every time (RGA
    // values are immutable once inserted -- see movePathPoint), so
    // "undo" here means "move it back", not "restore the old id" --
    // this call itself mints yet another fresh id, same as every
    // other point move.
    const currentPos = livePosOf(entry.pathId, entry.nodeId);
    if (currentPos) {
      const newNodeId = movePathPoint(entry.pathId, entry.nodeId, entry.prevPos);
      redoStack.push({ kind: "point_move", pathId: entry.pathId, nodeId: newNodeId, prevPos: currentPos });
    }
    renderAll();
    return;
  }
  let op;
  if (entry.kind === "path_add") {
    op = { target: "path_index", payload: lwwOp(clock.tick(), entry.pathId, null, true) };
  } else if (entry.kind === "path_remove") {
    op = { target: "path_index", payload: lwwOp(clock.tick(), entry.pathId, true, false) };
  } else if (entry.kind === "prop_set") {
    op = { target: "path_prop", scope: entry.pathId, payload: entry.hadPrevious ? lwwOp(clock.tick(), entry.key, entry.previous, false) : lwwOp(clock.tick(), entry.key, null, true) };
  }
  applyOp(op);
  sendOps([op]);
  redoStack.push(entry);
  renderAll();
}

function redo() {
  const entry = redoStack.pop();
  if (!entry) return;
  if (entry.kind === "point_move") {
    const currentPos = livePosOf(entry.pathId, entry.nodeId);
    if (currentPos) {
      const newNodeId = movePathPoint(entry.pathId, entry.nodeId, entry.prevPos);
      undoStack.push({ kind: "point_move", pathId: entry.pathId, nodeId: newNodeId, prevPos: currentPos });
    }
    renderAll();
    return;
  }
  let op;
  if (entry.kind === "path_add") {
    op = { target: "path_index", payload: lwwOp(clock.tick(), entry.pathId, true, false) };
  } else if (entry.kind === "path_remove") {
    op = { target: "path_index", payload: lwwOp(clock.tick(), entry.pathId, null, true) };
  } else if (entry.kind === "prop_set") {
    op = { target: "path_prop", scope: entry.pathId, payload: lwwOp(clock.tick(), entry.key, entry.forwardValue, false) };
  }
  applyOp(op);
  sendOps([op]);
  undoStack.push(entry);
  renderAll();
}

document.getElementById("undoBtn").onclick = undo;
document.getElementById("redoBtn").onclick = redo;

// -- presence ---------------------------------------------------------------------

let lastPresenceSent = 0;
function sendPresence(x, y) {
  const now = performance.now();
  if (now - lastPresenceSent < 60) return;
  lastPresenceSent = now;
  const op = { target: "presence", payload: lwwOp(clock.tick(), actorId, { x, y, name: actorName, color: actorColor }, false) };
  applyOp(op);
  sendOps([op]);
}

// -- canvas & tools -------------------------------------------------------------

const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");
const canvasWrap = document.querySelector(".canvas-wrap");

function resizeCanvas() {
  const rect = canvasWrap.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  canvas.style.width = rect.width + "px";
  canvas.style.height = rect.height + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  render();
}
window.addEventListener("resize", resizeCanvas);

/** Raw canvas-relative screen pixels (unaffected by pan/zoom) -- used for
 * hit-test thresholds and pan-drag deltas, where a constant on-screen
 * distance is what actually feels right at any zoom level. */
function canvasPoint(e) {
  const rect = canvas.getBoundingClientRect();
  return [Math.round(e.clientX - rect.left), Math.round(e.clientY - rect.top)];
}

/** The world-space point under the cursor -- what actually gets stored/
 * sent as geometry, snapped to the current grid if snap-to-grid is on. */
function worldPoint(e) {
  const [sx, sy] = canvasPoint(e);
  return snapWorldPoint(screenToWorld(sx, sy));
}

let drawing = null;
let spacePressed = false;
let panState = null; // {startSx, startSy, panX0, panY0}

function isPanGesture(e) {
  return e.button === 1 || (e.button === 0 && spacePressed);
}

canvas.addEventListener("pointerdown", (e) => {
  if (isPanGesture(e)) {
    e.preventDefault();
    const [sx, sy] = canvasPoint(e);
    panState = { startSx: sx, startSy: sy, panX0: view.panX, panY0: view.panY };
    canvas.style.cursor = "grabbing";
    return;
  }
  if (viewerMode) return; // Phase 17: a read-only viewer can pan/zoom/inspect but never start an edit gesture
  if (isShapeTool(ui.tool)) {
    const { point: pt, glyph } = resolveSnapPoint(worldPoint(e), new Set());
    activeSnapGlyph = glyph;
    shapeDraft = { kind: ui.tool, anchor: pt, current: pt };
    render();
    renderShapeInputPanel();
    return;
  }
  if (ui.tool === "select") {
    beginSelectDrag(e);
    return;
  }
  if (ui.tool === "constrain") {
    // Only a real RGA point can be dragged this way (a circle's
    // shape_center anchor has no point to drag -- it moves only via a
    // solve result, see setCircleCenter). A plain click (no movement)
    // still falls through to the click handler's existing pick-for-a-
    // new-constraint logic below, unaffected by this.
    const hit = hitTestPoint(canvasPoint(e));
    if (hit) {
      constrainDrag = { pathId: hit.pathId, nodeId: hit.nodeId, startWorld: worldPoint(e), livePos: hit.pos, moved: false };
    }
    return;
  }
  if (ui.tool !== "pen") return;
  if (!ui.activeLayer) { ui.activeLayer = addLayer("Layer 1"); renderLayerList(); }
  const pt = worldPoint(e);
  const { id, lastPointId } = addPath(ui.activeLayer, [pt], actorColor, 2.5);
  drawing = { pathId: id, lastPointId, lastScreenPt: canvasPoint(e) };
  selectOnly(id);
  renderAll();
});

canvas.addEventListener("pointermove", (e) => {
  if (panState) {
    const [sx, sy] = canvasPoint(e);
    view.panX = panState.panX0 + (sx - panState.startSx);
    view.panY = panState.panY0 + (sy - panState.startSy);
    render();
    return;
  }
  const screenPt = canvasPoint(e);
  const pt = worldPoint(e);
  sendPresence(pt[0], pt[1]);
  lastMousePt = pt;
  updateCursorReadout(pt);
  if (selectDrag) {
    handleSelectDragMove(screenPt, pt);
    return;
  }
  if (constrainDrag) {
    if (Math.hypot(pt[0] - constrainDrag.startWorld[0], pt[1] - constrainDrag.startWorld[1]) > 1e-6) {
      constrainDrag.moved = true;
    }
    constrainDrag.livePos = pt;
    render();
    return;
  }
  if (shapeDraft) {
    const { point: snapped, glyph } = resolveSnapPoint(pt, new Set());
    shapeDraft.current = snapped;
    activeSnapGlyph = glyph;
    render();
    renderShapeInputPanel();
    return;
  }
  if (drawing) {
    // The "how far before adding a new point" feel should stay constant
    // on screen regardless of zoom, so this threshold compares screen
    // positions -- but the stored point itself is world-space.
    const dist = Math.hypot(screenPt[0] - drawing.lastScreenPt[0], screenPt[1] - drawing.lastScreenPt[1]);
    if (dist > 2.5) {
      drawing.lastPointId = appendPoint(drawing.pathId, drawing.lastPointId, pt);
      drawing.lastScreenPt = screenPt;
      render();
    }
  } else if (ui.tool === "polygon" && pendingPolygon.length) {
    render();
  }
});

let justPanned = false;
window.addEventListener("pointerup", () => {
  if (panState) {
    panState = null;
    justPanned = true;
    canvas.style.cursor = ui.tool === "select" ? "default" : "crosshair";
    return;
  }
  if (selectDrag) {
    finishSelectDrag();
    return;
  }
  if (constrainDrag) {
    if (constrainDrag.moved) {
      justDraggedConstraintPoint = true;
      commitConstrainedPointDrag(constrainDrag.pathId, constrainDrag.nodeId, constrainDrag.livePos);
      renderAll();
    }
    constrainDrag = null;
    return;
  }
  if (shapeDraft) {
    // A negligible drag (a plain click) leaves anchor === current --
    // skip committing a zero-size shape nobody meant to draw.
    const [ax, ay] = shapeDraft.anchor, [cx, cy] = shapeDraft.current;
    if (Math.hypot(cx - ax, cy - ay) > 1e-6) {
      commitShape(shapePropsFromDraft(shapeDraft.kind, shapeDraft.anchor, shapeDraft.current));
    }
    shapeDraft = null;
    activeSnapGlyph = null;
    renderShapeInputPanel();
    return;
  }
  drawing = null;
});

canvas.addEventListener("click", (e) => {
  // A drag-to-pan (middle-button, or Space+left-button) still fires a
  // `click` on release -- suppress exactly that one, not real clicks.
  if (justPanned) { justPanned = false; return; }
  const screenPt = canvasPoint(e);
  const pt = worldPoint(e);
  if (ui.tool === "polygon") {
    if (pendingPolygon.length >= 3 && Math.hypot(...subtract(worldToScreen(...pendingPolygon[0]), screenPt)) < 10) {
      finishPolygon();
    } else {
      pendingPolygon.push(pt);
      render();
      renderToolHint();
    }
  } else if (ui.tool === "constrain") {
    // A drag that actually moved a point was already handled (and
    // re-solved) in pointerup -- the click that still fires on release
    // must not *also* register as picking this point for a brand new
    // constraint.
    if (justDraggedConstraintPoint) { justDraggedConstraintPoint = false; return; }
    const hit = pickConstraintEntity(screenPt);
    if (!hit) return;
    const already = constraintSelection.findIndex((s) => constraintEntityEq(s, hit));
    if (already !== -1) {
      constraintSelection.splice(already, 1);
    } else if (constraintSelection.length >= 2) {
      constraintSelection = [hit];
    } else {
      constraintSelection.push(hit);
    }
    render();
    renderConstraintPanel();
  } else if (ui.tool === "measure") {
    if (measureMode === "area") {
      const hit = hitTestPath(screenPt);
      if (hit) computeAreaMeasurement(hit);
      render();
      renderMeasurePanel();
      return;
    }
    const hit = hitTestPoint(screenPt);
    if (!hit) return;
    const already = measureSelection.findIndex((s) => s.pathId === hit.pathId && idEq(s.nodeId, hit.nodeId));
    if (already !== -1) measureSelection.splice(already, 1);
    else if (measureSelection.length >= 2) measureSelection = [hit];
    else measureSelection.push(hit);
    measureResult = null;
    if (measureSelection.length === 2) {
      if (measureMode === "distance") computeDistanceMeasurement();
      else computeAngleMeasurement();
    }
    render();
    renderMeasurePanel();
  } else if (ui.tool === "dimension") {
    const hit = hitTestPoint(screenPt);
    if (!hit) return;
    const already = dimensionSelection.findIndex((s) => s.pathId === hit.pathId && idEq(s.nodeId, hit.nodeId));
    if (already !== -1) {
      dimensionSelection.splice(already, 1);
    } else if (dimensionSelection.length >= 2) {
      dimensionSelection = [hit];
    } else {
      dimensionSelection.push(hit);
    }
    if (dimensionSelection.length === 2) {
      commitDimension(dimensionSelection[0], dimensionSelection[1]);
      dimensionSelection = [];
    }
    render();
  } else if (ui.tool === "text") {
    commitText(pt);
  }
});

function subtract(a, b) {
  return [a[0] - b[0], a[1] - b[1]];
}

// -- select tool: click/shift-click, move-drag, marquee-select (Phase 12) ------
// A plain click on a path selects it (replacing the current selection);
// shift-click toggles it into/out of the current selection without
// starting a move. Dragging a path that's part of the (possibly
// multi-path) current selection moves the whole selection together,
// live-previewed locally and committed as one `transform` write per
// path on release (not per-mousemove, to avoid flooding ops). Dragging
// on empty canvas instead marquee-selects everything whose transformed
// bounding box intersects the drawn rectangle on release.

function beginSelectDrag(e) {
  const screenPt = canvasPoint(e);
  const hit = hitTestPath(screenPt);
  if (hit) {
    // Phase 15: "selecting any member selects the group" -- an
    // ungrouped path's own "group" is just itself, so this subsumes the
    // pre-Phase-15 single-path behavior unchanged.
    const members = groupMembersOf(hit);
    if (e.shiftKey) {
      // Shift-click only ever adjusts the selection -- it never starts
      // a move, so users can freely build up a multi-selection without
      // accidentally dragging the last-clicked path. Toggles the whole
      // group together, not just the clicked member.
      const allSelected = members.every((m) => ui.selectedPaths.has(m));
      for (const m of members) {
        if (allSelected) ui.selectedPaths.delete(m);
        else ui.selectedPaths.add(m);
      }
      renderAll();
      return;
    }
    if (!members.every((m) => ui.selectedPaths.has(m))) ui.selectedPaths = new Set(members);
    const primaryProps = state.pathProps.get(hit) || {};
    selectDrag = {
      mode: "move",
      startWorld: worldPoint(e),
      selection: new Set(ui.selectedPaths),
      primaryId: hit,
      primaryPivotWorld0: applyPathTransform(hit, primaryProps, pathBaseCenter(hit, primaryProps)),
      liveDelta: [0, 0],
      moved: false,
    };
    renderAll();
    return;
  }
  // No hit: a plain click clears the selection (a marquee that ends up
  // catching nothing should leave nothing selected); shift-click on
  // empty canvas leaves the existing selection untouched so the
  // marquee can only ever add to it.
  if (!e.shiftKey) selectOnly(null);
  selectDrag = { mode: "marquee", startScreen: screenPt, currentScreen: screenPt, additive: e.shiftKey };
  renderAll();
}

function handleSelectDragMove(screenPt, pt) {
  if (selectDrag.mode === "move") {
    const rawDelta = [pt[0] - selectDrag.startWorld[0], pt[1] - selectDrag.startWorld[1]];
    if (Math.hypot(rawDelta[0], rawDelta[1]) > 1e-6) selectDrag.moved = true;
    const candidatePivot = [
      selectDrag.primaryPivotWorld0[0] + rawDelta[0],
      selectDrag.primaryPivotWorld0[1] + rawDelta[1],
    ];
    const { point: snappedPivot, glyph } = resolveSnapPoint(candidatePivot, selectDrag.selection);
    activeSnapGlyph = glyph;
    selectDrag.liveDelta = [
      snappedPivot[0] - selectDrag.primaryPivotWorld0[0],
      snappedPivot[1] - selectDrag.primaryPivotWorld0[1],
    ];
    render();
  } else {
    selectDrag.currentScreen = screenPt;
    render();
  }
}

function finishSelectDrag() {
  if (selectDrag.mode === "move") {
    if (selectDrag.moved) {
      const [dx, dy] = selectDrag.liveDelta;
      for (const pathId of selectDrag.selection) nudgePathTransform(pathId, dx, dy);
    }
  } else {
    const [sx0, sy0] = selectDrag.startScreen;
    const [sx1, sy1] = selectDrag.currentScreen;
    const marquee = { minX: Math.min(sx0, sx1), maxX: Math.max(sx0, sx1), minY: Math.min(sy0, sy1), maxY: Math.max(sy0, sy1) };
    // A negligible drag is just a click on empty space -- the selection
    // was already handled (cleared, unless shift) in beginSelectDrag.
    if (marquee.maxX - marquee.minX > 3 || marquee.maxY - marquee.minY > 3) {
      const matched = [];
      for (const pathId of state.pathIndex) {
        const props = state.pathProps.get(pathId) || {};
        if (ui.hiddenLayers.has(props.layer_id)) continue;
        const bounds = pathWorldBounds(pathId, props);
        if (!bounds) continue;
        const [sMinX, sMinY] = worldToScreen(bounds.minX, bounds.minY);
        const [sMaxX, sMaxY] = worldToScreen(bounds.maxX, bounds.maxY);
        if (rectsIntersect(marquee, { minX: sMinX, minY: sMinY, maxX: sMaxX, maxY: sMaxY })) matched.push(pathId);
      }
      if (selectDrag.additive) for (const id of matched) ui.selectedPaths.add(id);
      else ui.selectedPaths = new Set(matched);
    }
  }
  selectDrag = null;
  activeSnapGlyph = null;
  renderAll();
}

canvas.addEventListener(
  "wheel",
  (e) => {
    e.preventDefault();
    const [sx, sy] = canvasPoint(e);
    const [wxBefore, wyBefore] = screenToWorld(sx, sy);
    const factor = Math.exp(-e.deltaY * 0.001);
    view.zoom = Math.max(0.05, Math.min(20, view.zoom * factor));
    // Re-anchor pan so the world point under the cursor doesn't jump --
    // "zoom centered on cursor" per the brief.
    view.panX = sx - wxBefore * view.zoom;
    view.panY = sy - wyBefore * view.zoom;
    render();
    updateZoomIndicator();
  },
  { passive: false }
);

window.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && ui.tool === "polygon") cancelPolygon();
  if (e.code === "Space" && !["INPUT", "TEXTAREA"].includes(e.target.tagName)) {
    spacePressed = true;
    canvas.style.cursor = "grab";
    e.preventDefault(); // don't let the page scroll on spacebar
  }
  if (["INPUT", "TEXTAREA"].includes(e.target.tagName)) return;
  const mod = e.ctrlKey || e.metaKey;
  if (mod && e.key.toLowerCase() === "d") {
    e.preventDefault();
    duplicateSelection();
  } else if (mod && e.key.toLowerCase() === "c") {
    if (ui.selectedPaths.size) { e.preventDefault(); copySelectionToClipboard(); }
  } else if (mod && e.key.toLowerCase() === "v") {
    e.preventDefault();
    pasteSelectionFromClipboard();
  } else if (e.key === "Delete" || e.key === "Backspace") {
    if (ui.selectedPaths.size) { e.preventDefault(); deleteSelection(); }
  } else if (e.key === "?") {
    toggleShortcutOverlay();
  }
});
window.addEventListener("keyup", (e) => {
  if (e.code === "Space") {
    spacePressed = false;
    canvas.style.cursor = ui.tool === "select" ? "default" : "crosshair";
  }
});

function finishPolygon() {
  if (pendingPolygon.length < 3) { cancelPolygon(); return; }
  if (!ui.activeLayer) ui.activeLayer = addLayer("Layer 1");
  const closed = [...pendingPolygon, pendingPolygon[0]];
  const { id } = addPath(ui.activeLayer, closed, "#ffd43b", 2.5, { strict: true });
  selectOnly(id);
  pendingPolygon = [];
  renderAll();
}

function cancelPolygon() {
  pendingPolygon = [];
  render();
  renderToolHint();
}

/** `screenPt` is in raw canvas pixels (canvasPoint(e)) -- stored path
 * points are world-space, so each candidate is projected to screen via
 * worldToScreen before comparing, keeping the hit-test radius a constant
 * on-screen size regardless of zoom. */
function hitTestPath(screenPt) {
  let best = null, bestDist = 8;
  for (const pathId of state.pathIndex) {
    const props = state.pathProps.get(pathId) || {};
    if (ui.hiddenLayers.has(props.layer_id)) continue;
    if (props.shape) {
      // Shape primitives (Phase 11) have no path_geom points to walk --
      // hitTestShape has its own dedicated per-kind boundary math. Test
      // against the *transformed* (Phase 12) shape, not the base one.
      if (hitTestShape(transformedShapeProps(pathId, props), screenPt)) return pathId;
      continue;
    }
    const pts = pathPoints(pathId).map((p) => worldToScreen(...applyPathTransform(pathId, props, p)));
    for (let i = 0; i < pts.length - 1; i++) {
      const d = distToSegment(screenPt, pts[i], pts[i + 1]);
      if (d < bestDist) { bestDist = d; best = pathId; }
    }
  }
  return best;
}

/** Finds the closest *individual point* (not segment) across every
 * visible path, within a small on-screen pixel radius -- the constrain
 * tool relates specific points, not whole paths. `screenPt` is raw
 * canvas pixels; `pos` on the returned hit is world-space. */
function hitTestPoint(screenPt) {
  let best = null, bestDist = 10;
  for (const pathId of state.pathIndex) {
    if (ui.hiddenLayers.has((state.pathProps.get(pathId) || {}).layer_id)) continue;
    for (const entry of liveEntries(state.pathNodes.get(pathId))) {
      const [sx, sy] = worldToScreen(entry.v[0], entry.v[1]);
      const d = Math.hypot(screenPt[0] - sx, screenPt[1] - sy);
      if (d < bestDist) { bestDist = d; best = { pathId, nodeId: entry.id, pos: entry.v }; }
    }
  }
  return best;
}

function livePosOf(pathId, nodeId) {
  const entry = liveEntries(state.pathNodes.get(pathId)).find((n) => idEq(n.id, nodeId));
  return entry ? entry.v : null;
}

/** Parallel/perpendicular constraints relate two *lines*, not two bare
 * points -- for a point the user actually clicked, its "line" is the
 * segment to whichever live neighbor it has (the next point if there is
 * one, else the previous one). Returns null for a single-point path,
 * which has no line to define. */
function findAdjacentPoint(pathId, nodeId) {
  const entries = liveEntries(state.pathNodes.get(pathId));
  const idx = entries.findIndex((e) => idEq(e.id, nodeId));
  if (idx === -1) return null;
  if (idx + 1 < entries.length) return { pathId, nodeId: entries[idx + 1].id, pos: entries[idx + 1].v };
  if (idx - 1 >= 0) return { pathId, nodeId: entries[idx - 1].id, pos: entries[idx - 1].v };
  return null;
}

/** Moves an existing path point to a new position. RGA values are
 * immutable once inserted (no in-place "set" the way MeshCRDT's vertex
 * LWWMap has), so this is CRDT-safe delete-then-reinsert at the same
 * slot -- the point's node id changes as a result. If a curve segment
 * (Phase 8) was attached to the old id, it's orphaned (harmless dead
 * weight in path_props, but the visual effect is that segment reverts
 * to a straight line) -- an accepted trade-off for the common case this
 * solver targets (straight-line CAD-style sketches), not attempted to
 * be avoided here. A dimension (Phase 13) or persisted constraint
 * (Phase 14) anchored to the old id is NOT left to orphan the same way,
 * though: "updates automatically when the geometry moves" is the
 * entire point of both features, so remapDimensionAnchor/
 * remapConstraintAnchors carry any matching anchor forward onto the new
 * node id as part of this same move. This is the RAW move primitive --
 * no undo bookkeeping (see movePathPointWithUndo for that), so undo()/
 * redo() themselves can call this directly without it recursively
 * pushing its own undo entry. Returns the new node id, or null if
 * `oldNodeId` isn't currently live (e.g. a concurrent delete). */
function movePathPoint(pathId, oldNodeId, newPos) {
  const entries = liveEntries(state.pathNodes.get(pathId));
  const idx = entries.findIndex((e) => idEq(e.id, oldNodeId));
  if (idx === -1) return null;
  const prevId = idx > 0 ? entries[idx - 1].id : null;
  const delOp = { target: "path_geom", scope: pathId, payload: rgaDeleteOp(oldNodeId, clock.tick()) };
  applyOp(delOp);
  const newNodeId = clock.tick();
  const insOp = { target: "path_geom", scope: pathId, payload: rgaInsertOp(newNodeId, prevId, newPos) };
  applyOp(insOp);
  sendOps([delOp, insOp]);
  remapDimensionAnchor(pathId, oldNodeId, newNodeId);
  remapConstraintAnchors(pathId, oldNodeId, newNodeId);
  return newNodeId;
}

/** Wraps movePathPoint with an undo-stack push (Phase 14: the brief
 * explicitly asks for constraint-driven moves to be undoable via "the
 * existing inverted-op machinery" -- movePathPoint itself stayed
 * undo-free so undo()/redo() can call it directly without a nested
 * push). Every constraint-application and constrain-tool point-drag
 * call site uses this wrapper, not the raw function, so those moves
 * are undoable the same way any other edit here is. */
function movePathPointWithUndo(pathId, oldNodeId, newPos) {
  const oldPos = livePosOf(pathId, oldNodeId);
  const newNodeId = movePathPoint(pathId, oldNodeId, newPos);
  if (newNodeId && oldPos) {
    undoStack.push({ kind: "point_move", pathId, nodeId: newNodeId, prevPos: oldPos });
    redoStack.length = 0;
  }
  return newNodeId;
}

/** Carries a dimension's anchor forward onto a point's new node id
 * after a CRDT-safe delete+reinsert move (see movePathPoint) -- without
 * this, every point move would silently break "updates automatically
 * when the geometry moves," the entire reason Phase 13 dimensions
 * reference geometry by id instead of copying coordinates. Rewrites the
 * whole dimension payload (one LWW value per dimension id, not
 * per-field) since nobody else should be concurrently editing the same
 * dimension's anchors while it's being moved. */
function remapDimensionAnchor(pathId, oldNodeId, newNodeId) {
  const ops = [];
  for (const [dimId, dim] of state.dimensions) {
    const updated = { ...dim };
    let changed = false;
    if (dim.a_path === pathId && idEq(dim.a_node, oldNodeId)) { updated.a_node = newNodeId; changed = true; }
    if (dim.b_path === pathId && idEq(dim.b_node, oldNodeId)) { updated.b_node = newNodeId; changed = true; }
    if (changed) {
      const op = { target: "dimension", payload: lwwOp(clock.tick(), dimId, updated, false) };
      applyOp(op);
      ops.push(op);
    }
  }
  if (ops.length) sendOps(ops);
}

/** Same idea as remapDimensionAnchor, for persisted constraints (Phase
 * 14) -- a constraint's "point" anchors reference an RGA node id, same
 * as a dimension's, so a point move needs the same carry-forward or the
 * constraint would silently stop applying to the point the user
 * actually meant. `shape_center` anchors (a circle's own center, for
 * `tangent`) never need this -- see setCircleCenter, which updates the
 * circle's cx/cy props directly rather than an RGA node id. */
function remapConstraintAnchors(pathId, oldNodeId, newNodeId) {
  const ops = [];
  for (const [constraintId, spec] of state.constraints) {
    const updatedAnchors = { ...spec.anchors };
    let changed = false;
    for (const [name, anchor] of Object.entries(spec.anchors)) {
      if (anchor.type === "point" && anchor.path_id === pathId && idEq(anchor.node_id, oldNodeId)) {
        updatedAnchors[name] = { ...anchor, node_id: newNodeId };
        changed = true;
      }
    }
    if (changed) {
      const op = { target: "constraint", payload: lwwOp(clock.tick(), constraintId, { ...spec, anchors: updatedAnchors }, false) };
      applyOp(op);
      ops.push(op);
    }
  }
  if (ops.length) sendOps(ops);
}

/** Reads the live position of a constraint-selection entry (see
 * pickConstraintEntity) regardless of whether it's a plain RGA point or
 * a circle's shape_center -- lets every kind's ref-building logic below
 * stay agnostic to which one it got. */
function liveConstraintSelectionPos(sel) {
  return sel.isShapeCenter ? liveShapeCenterOf(sel.pathId) : livePosOf(sel.pathId, sel.nodeId);
}

/** Applies a solved position back to whatever a ref actually is: a
 * circle's cx/cy (setCircleCenter) for a shape_center, or an ordinary
 * undoable point move otherwise. */
function applySolvedRefPosition(ref, newPos) {
  if (ref.isShapeCenter) setCircleCenter(ref.pathId, newPos);
  else movePathPointWithUndo(ref.pathId, ref.nodeId, newPos);
}

/** Converts a constraint-selection ref into its durable anchor shape
 * (see the `constraints` component's docstring in document.py) for
 * persisting via addConstraint. */
function refToAnchor(ref) {
  return ref.isShapeCenter
    ? { type: "shape_center", path_id: ref.pathId }
    : { type: "point", path_id: ref.pathId, node_id: ref.nodeId };
}

function addConstraint(kind, refs, param) {
  const anchors = {};
  for (const [name, ref] of Object.entries(refs)) anchors[name] = refToAnchor(ref);
  const id = "constraint_" + rid();
  const op = { target: "constraint", payload: lwwOp(clock.tick(), id, { kind, anchors, param: param || 0 }, false) };
  applyOp(op);
  sendOps([op]);
}

function removeConstraint(id) {
  const op = { target: "constraint", payload: lwwOp(clock.tick(), id, null, true) };
  applyOp(op);
  sendOps([op]);
}

// -- groups (Phase 15) ------------------------------------------------------------
// `group_id` is an ordinary path_prop field -- grouping itself needs no
// new CRDT machinery, it merges field-wise exactly like color/width
// already do. `groups` only tracks which group ids currently exist,
// mirroring `layers`.

function addGroup() {
  const id = "group_" + rid();
  const op = { target: "group", payload: lwwOp(clock.tick(), id, true, false) };
  applyOp(op);
  sendOps([op]);
  return id;
}

/** Tags every path in `pathIds` with a fresh group id -- selecting any
 * one of them afterward selects the whole group (see beginSelectDrag's
 * hit-handling). */
function groupPaths(pathIds) {
  if (pathIds.length < 2) return;
  const gid = addGroup();
  for (const pathId of pathIds) setPathProp(pathId, "group_id", gid);
  renderAll();
}

/** Clears `group_id` from every current member of the given path's
 * group (not just the clicked path) -- ungrouping is a whole-group
 * action, matching "selecting any member selects the group." The group
 * id's own existence record is removed too, once nothing references it. */
function ungroupPath(pathId) {
  const gid = (state.pathProps.get(pathId) || {}).group_id;
  if (!gid) return;
  for (const [pid, props] of state.pathProps) {
    if (props.group_id === gid) setPathProp(pid, "group_id", null);
  }
  const op = { target: "group", payload: lwwOp(clock.tick(), gid, null, true) };
  applyOp(op);
  sendOps([op]);
  renderAll();
}

/** Every path currently tagged with the same group_id as `pathId` --
 * used so clicking any one member selects the whole group. Returns
 * just `[pathId]` if it isn't grouped. */
function groupMembersOf(pathId) {
  const gid = (state.pathProps.get(pathId) || {}).group_id;
  if (!gid) return [pathId];
  return [...state.pathIndex].filter((pid) => (state.pathProps.get(pid) || {}).group_id === gid);
}

/** Solves the chosen constraint against the two currently-selected
 * entities (see the "constrain" tool's click handler) via the existing,
 * already-tested `/api/solve` endpoint, moves whichever points it
 * returned a changed position for, and -- Phase 14 -- persists the
 * constraint so it renders as a badge, can be selected + deleted, and
 * is automatically re-solved the next time one of its points is
 * dragged (see resolveAndApplyConstraints). Parallel/perpendicular need
 * two *lines*, inferred from each selected point's neighbor (see
 * findAdjacentPoint); tangent needs one circle (a shape_center, no RGA
 * point of its own) and one line, same neighbor-inference for the line
 * half; coincident/fixed_distance work directly on the two selected
 * points. */
async function applyConstraint(kind, param) {
  if (constraintSelection.length !== 2) return;
  const [sel1, sel2] = constraintSelection;
  const points = {};
  const refs = {};
  let pointIds;
  if (kind === "tangent") {
    const circleSel = sel1.isShapeCenter ? sel1 : sel2.isShapeCenter ? sel2 : null;
    const lineSel = sel1.isShapeCenter ? sel2 : sel2.isShapeCenter ? sel1 : null;
    if (!circleSel || lineSel.isShapeCenter) {
      showToast("Tangent needs exactly one circle and one point on a line", "error");
      return;
    }
    const adj = findAdjacentPoint(lineSel.pathId, lineSel.nodeId);
    if (!adj) {
      showToast("The selected point needs a neighboring point to define a line", "error");
      return;
    }
    refs.circle = circleSel; refs.line_a = lineSel; refs.line_b = adj;
    for (const [key, ref] of Object.entries(refs)) points[key] = liveConstraintSelectionPos(ref);
    pointIds = ["circle", null, "line_a", "line_b"];
  } else if (kind === "parallel" || kind === "perpendicular") {
    const adj1 = findAdjacentPoint(sel1.pathId, sel1.nodeId);
    const adj2 = findAdjacentPoint(sel2.pathId, sel2.nodeId);
    if (!adj1 || !adj2) {
      showToast("Both selected points need a neighboring point to define a line", "error");
      return;
    }
    refs.p1a = sel1; refs.p1b = adj1; refs.p2a = sel2; refs.p2b = adj2;
    for (const [key, ref] of Object.entries(refs)) points[key] = liveConstraintSelectionPos(ref);
    pointIds = ["p1a", "p1b", "p2a", "p2b"];
  } else {
    refs.p1 = sel1; refs.p2 = sel2;
    for (const [key, ref] of Object.entries(refs)) points[key] = liveConstraintSelectionPos(ref);
    pointIds = ["p1", "p2"];
  }
  if (Object.values(points).some((p) => !p)) {
    showToast("A selected entity no longer exists (concurrent edit) -- try again", "error");
    constraintSelection = [];
    renderAll();
    return;
  }

  let resp;
  try {
    resp = await fetch("/api/solve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ points, constraints: [{ kind, point_ids: pointIds, param: param || 0 }] }),
    });
  } catch {
    showToast("Could not reach the solver", "error");
    return;
  }
  if (!resp.ok) { showToast("Solve request failed", "error"); return; }
  const result = await resp.json();
  if (!result.converged) {
    showToast("Solver did not converge -- no changes applied", "error");
    return;
  }
  for (const [key, ref] of Object.entries(refs)) {
    const newPos = result.positions[key];
    const oldPos = points[key];
    if (Math.abs(newPos[0] - oldPos[0]) > 1e-6 || Math.abs(newPos[1] - oldPos[1]) > 1e-6) {
      applySolvedRefPosition(ref, newPos);
    }
  }
  addConstraint(kind, refs, param);
  constraintSelection = [];
  renderAll();
  showToast("Constraint applied", "success");
}

/** Picks an entity for the Constrain tool: an ordinary RGA point via
 * the existing hitTestPoint, or -- Phase 14, for tangent -- a circle
 * shape's own center, tried only if no point was hit. A circle has no
 * RGA points of its own, so it needs this separate shape-only pick;
 * boundary-only hit-testing matches every other shape interaction in
 * this file (shapes are unfilled outlines). */
function pickConstraintEntity(screenPt) {
  const pointHit = hitTestPoint(screenPt);
  if (pointHit) return { pathId: pointHit.pathId, nodeId: pointHit.nodeId, pos: pointHit.pos, isShapeCenter: false };
  for (const pathId of state.pathIndex) {
    const props = state.pathProps.get(pathId) || {};
    if (props.shape !== "circle" || ui.hiddenLayers.has(props.layer_id)) continue;
    if (hitTestShape(transformedShapeProps(pathId, props), screenPt)) {
      return { pathId, nodeId: null, pos: liveShapeCenterOf(pathId), isShapeCenter: true };
    }
  }
  return null;
}

/** Whether two constraint-selection entries refer to the same pick --
 * idEq alone isn't enough since a shape_center entry has no nodeId. */
function constraintEntityEq(a, b) {
  if (a.pathId !== b.pathId || a.isShapeCenter !== b.isShapeCenter) return false;
  return a.isShapeCenter || idEq(a.nodeId, b.nodeId);
}

function renderConstraintPanel() {
  const panel = document.getElementById("constraintPanel");
  if (!panel) return;
  if (constraintSelection.length < 2) {
    const remaining = 2 - constraintSelection.length;
    panel.innerHTML = `<div class="empty-hint">Constrain tool: click ${remaining} more point(s) or circle(s) to relate them. Dragging an already-constrained point re-solves automatically on release.</div>`;
    return;
  }
  const [a, b] = constraintSelection;
  const shapeCenterCount = (a.isShapeCenter ? 1 : 0) + (b.isShapeCenter ? 1 : 0);
  if (shapeCenterCount === 2) {
    panel.innerHTML = '<div class="empty-hint">Pick one circle and one point on a line for Tangent -- two circles aren\'t a supported combination.</div>';
    return;
  }
  if (shapeCenterCount === 1) {
    const circleSel = a.isShapeCenter ? a : b;
    const props = state.pathProps.get(circleSel.pathId) || {};
    const radius = (getTransform(props).scale || 1) * (props.r || 0);
    panel.innerHTML = `
      <div class="field-row"><label>Radius</label><input id="constraintRadius" type="number" step="0.1" value="${radius.toFixed(2)}" style="width:70px"/></div>
      <button id="constrainTangent" style="width:100%">Tangent</button>
    `;
    document.getElementById("constrainTangent").onclick = () =>
      applyConstraint("tangent", parseFloat(document.getElementById("constraintRadius").value) || radius);
    return;
  }
  const dist = Math.hypot(a.pos[0] - b.pos[0], a.pos[1] - b.pos[1]);
  panel.innerHTML = `
    <div class="field-row"><label>Distance</label><input id="constraintDistance" type="number" step="0.1" value="${dist.toFixed(2)}" style="width:70px"/></div>
    <button id="constrainCoincident" style="width:100%;margin-bottom:4px">Coincident</button>
    <button id="constrainParallel" style="width:100%;margin-bottom:4px">Parallel</button>
    <button id="constrainPerpendicular" style="width:100%;margin-bottom:4px">Perpendicular</button>
    <button id="constrainFixedDistance" style="width:100%">Fixed distance</button>
  `;
  document.getElementById("constrainCoincident").onclick = () => applyConstraint("coincident");
  document.getElementById("constrainParallel").onclick = () => applyConstraint("parallel");
  document.getElementById("constrainPerpendicular").onclick = () => applyConstraint("perpendicular");
  document.getElementById("constrainFixedDistance").onclick = () =>
    applyConstraint("fixed_distance", parseFloat(document.getElementById("constraintDistance").value) || 0);
}

// -- re-solve on drag + persisted constraint badges/list (Phase 14) -------------

const CONSTRAINT_POINT_ORDER = {
  coincident: ["p1", "p2"],
  fixed_distance: ["p1", "p2"],
  parallel: ["p1a", "p1b", "p2a", "p2b"],
  perpendicular: ["p1a", "p1b", "p2a", "p2b"],
  tangent: ["circle", null, "line_a", "line_b"],
};

const CONSTRAINT_GLYPHS = {
  coincident: "≡", parallel: "∥", perpendicular: "⊥", fixed_distance: "↔", tangent: "⊙",
};

function resolveConstraintAnchorPos(anchor) {
  return anchor.type === "shape_center" ? liveShapeCenterOf(anchor.path_id) : livePosOf(anchor.path_id, anchor.node_id);
}

/** Batch-resolves and re-applies every given persisted constraint in
 * one /api/solve call (constraints can share points, so solving them
 * together -- not one at a time -- is what actually keeps a sketch
 * consistent). Called after a constrain-tool point drag commits (see
 * commitConstrainedPointDrag) against every constraint touching the
 * dragged path -- "re-solve automatically when a constrained point is
 * dragged" per the brief, on release, not per-frame. */
async function resolveAndApplyConstraints(constraintEntries) {
  const points = {};
  const keyToAnchor = {};
  const solverConstraints = [];
  for (const [, spec] of constraintEntries) {
    const order = CONSTRAINT_POINT_ORDER[spec.kind];
    if (!order) continue;
    const pointIds = [];
    let anyUnresolved = false;
    for (const name of order) {
      if (name === null) { pointIds.push(null); continue; }
      const anchor = spec.anchors[name];
      const key = `${anchor.path_id}:${anchor.type}:${anchor.type === "point" ? opIdKey(anchor.node_id) : "c"}`;
      const pos = resolveConstraintAnchorPos(anchor);
      if (!pos) { anyUnresolved = true; break; }
      points[key] = pos;
      keyToAnchor[key] = anchor;
      pointIds.push(key);
    }
    if (anyUnresolved) continue; // a concurrent delete -- skip this one constraint, not the whole batch
    solverConstraints.push({ kind: spec.kind, point_ids: pointIds, param: spec.param || 0 });
  }
  if (!solverConstraints.length) return;

  let resp;
  try {
    resp = await fetch("/api/solve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ points, constraints: solverConstraints }),
    });
  } catch {
    showToast("Could not reach the solver", "error");
    return;
  }
  if (!resp.ok) { showToast("Solve request failed", "error"); return; }
  const result = await resp.json();
  if (!result.converged) { showToast("Solver did not converge -- constraints left as-is", "error"); return; }
  for (const [key, anchor] of Object.entries(keyToAnchor)) {
    const newPos = result.positions && result.positions[key];
    const oldPos = points[key];
    if (!newPos || !oldPos) continue;
    if (Math.abs(newPos[0] - oldPos[0]) > 1e-6 || Math.abs(newPos[1] - oldPos[1]) > 1e-6) {
      if (anchor.type === "shape_center") setCircleCenter(anchor.path_id, newPos);
      else movePathPointWithUndo(anchor.path_id, anchor.node_id, newPos);
    }
  }
}

/** Commits a constrain-tool point drag (a real, undoable move, exactly
 * like any other point move here) and then re-solves every persisted
 * constraint touching that path in one batch. A point with no
 * constraints yet is just an ordinary move -- no wasted solve request. */
function commitConstrainedPointDrag(pathId, oldNodeId, newPos) {
  movePathPointWithUndo(pathId, oldNodeId, newPos);
  const relevant = [...state.constraints.entries()].filter(([, spec]) =>
    Object.values(spec.anchors).some((a) => a.type === "point" && a.path_id === pathId)
  );
  if (relevant.length) resolveAndApplyConstraints(relevant);
}

function renderConstraintsListPanel() {
  const list = document.getElementById("constraintsList");
  if (!list) return;
  if (state.constraints.size === 0) {
    list.innerHTML = '<div class="empty-hint">No persisted constraints yet.</div>';
    return;
  }
  list.innerHTML = "";
  for (const [cId, spec] of state.constraints) {
    const row = document.createElement("div");
    row.className = "path-row";
    const label = `${CONSTRAINT_GLYPHS[spec.kind] || "?"} ${spec.kind}`;
    row.innerHTML = `<span class="name">${escapeHtml(label)}</span><button class="ghost-btn" data-act="del" title="Delete" aria-label="Delete">${iconHtml("x")}</button>`;
    row.querySelector('[data-act="del"]').onclick = () => { removeConstraint(cId); renderAll(); };
    list.appendChild(row);
  }
}

/** Small glyph near a persisted constraint's (live-resolved) anchors,
 * at their centroid -- square=endpoint-style badges felt right for
 * object-snap (Phase 12) but a constraint relates *entities*, not
 * points, so a symbol per kind (≡ coincident, ∥ parallel, ⊥
 * perpendicular, ↔ fixed-distance, ⊙ tangent) reads better at a
 * glance. Silently skipped if any anchor no longer resolves. */
function renderConstraintBadge(spec) {
  const positions = Object.values(spec.anchors).map(resolveConstraintAnchorPos).filter(Boolean);
  if (positions.length < 2) return;
  const cx = positions.reduce((s, p) => s + p[0], 0) / positions.length;
  const cy = positions.reduce((s, p) => s + p[1], 0) / positions.length;
  const [sx, sy] = worldToScreen(cx, cy);
  ctx.save();
  ctx.fillStyle = "#ffd43b";
  ctx.font = "13px sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(CONSTRAINT_GLYPHS[spec.kind] || "?", sx, sy);
  ctx.restore();
}

// -- measure tool (Phase 13) -----------------------------------------------------
// Read-only, client-local, no CRDT ops ever sent -- purely a computed
// readout over already-live geometry. Distance/Angle pick points the same
// way Constrain does (including reusing findAdjacentPoint for Angle);
// Area/Perimeter instead picks one whole path/shape directly.

function shoelaceArea(pts) {
  let sum = 0;
  for (let i = 0; i < pts.length; i++) {
    const [x1, y1] = pts[i], [x2, y2] = pts[(i + 1) % pts.length];
    sum += x1 * y2 - x2 * y1;
  }
  return Math.abs(sum) / 2;
}

function polygonPerimeter(pts) {
  let sum = 0;
  for (let i = 0; i < pts.length; i++) {
    const [x1, y1] = pts[i], [x2, y2] = pts[(i + 1) % pts.length];
    sum += Math.hypot(x2 - x1, y2 - y1);
  }
  return sum;
}

function computeDistanceMeasurement() {
  const [a, b] = measureSelection;
  const dist = Math.hypot(a.pos[0] - b.pos[0], a.pos[1] - b.pos[1]);
  measureResult = { text: `Distance: ${toDisplayUnits(dist).toFixed(2)}${unitSuffix() ? " " + unitSuffix() : ""}` };
}

/** Angle between the two *lines* each selected point defines (inferred
 * from its live neighbor, same as Constrain's parallel/perpendicular) --
 * not just the angle at a single vertex. */
function computeAngleMeasurement() {
  const [a, b] = measureSelection;
  const adjA = findAdjacentPoint(a.pathId, a.nodeId);
  const adjB = findAdjacentPoint(b.pathId, b.nodeId);
  if (!adjA || !adjB) {
    measureResult = { text: "Both points need a neighboring point to define a line." };
    return;
  }
  const v1 = [adjA.pos[0] - a.pos[0], adjA.pos[1] - a.pos[1]];
  const v2 = [adjB.pos[0] - b.pos[0], adjB.pos[1] - b.pos[1]];
  const dot = v1[0] * v2[0] + v1[1] * v2[1];
  const mag = Math.hypot(...v1) * Math.hypot(...v2);
  if (mag < 1e-9) { measureResult = { text: "One of the lines has zero length." }; return; }
  const angle = (Math.acos(Math.max(-1, Math.min(1, dot / mag))) * 180) / Math.PI;
  measureResult = { text: `Angle: ${angle.toFixed(2)}°` };
}

/** Area/perimeter for Rect/Circle/Ellipse comes from their own exact
 * formulas; a freehand/polygon path uses the shoelace formula and a
 * summed segment length, implicitly treating it as closed either way
 * (a "closed path" measurement doesn't mean much otherwise) -- Line/Arc
 * have no enclosed area and are explicitly called out as such rather
 * than showing a meaningless number. */
function computeAreaMeasurement(pathId) {
  const props = state.pathProps.get(pathId) || {};
  const fmt = (v) => `${toDisplayUnits(v).toFixed(2)}${unitSuffix() ? " " + unitSuffix() : ""}`;
  const fmtArea = (v) => `${toDisplayUnitsArea(v).toFixed(2)} ${unitSuffix() || "px"}²`;
  if (props.shape === "rect") {
    measureResult = { text: `Area: ${fmtArea(props.w * props.h)} · Perimeter: ${fmt(2 * (props.w + props.h))}` };
  } else if (props.shape === "circle") {
    measureResult = { text: `Area: ${fmtArea(Math.PI * props.r * props.r)} · Perimeter: ${fmt(2 * Math.PI * props.r)}` };
  } else if (props.shape === "ellipse") {
    const { rx, ry } = props;
    // Ramanujan's approximation -- exact for a circle (rx===ry), close
    // enough for any other ellipse for a measurement readout.
    const h = ((rx - ry) * (rx - ry)) / ((rx + ry) * (rx + ry));
    const perim = Math.PI * (rx + ry) * (1 + (3 * h) / (10 + Math.sqrt(4 - 3 * h)));
    measureResult = { text: `Area: ${fmtArea(Math.PI * rx * ry)} · Perimeter: ${fmt(perim)}` };
  } else if (props.shape) {
    measureResult = { text: `${props.shape[0].toUpperCase()}${props.shape.slice(1)} has no enclosed area to measure.` };
  } else {
    const pts = pathPoints(pathId);
    if (pts.length < 3) { measureResult = { text: "Needs at least 3 points to measure an area." }; return; }
    measureResult = { text: `Area: ${fmtArea(shoelaceArea(pts))} · Perimeter: ${fmt(polygonPerimeter(pts))}` };
  }
}

function renderMeasurePanel() {
  const panel = document.getElementById("measurePanel");
  if (!panel) return;
  if (ui.tool !== "measure") { panel.innerHTML = ""; return; }
  const modes = [["distance", "Distance"], ["angle", "Angle"], ["area", "Area/Perim."]];
  const modeButtons = modes
    .map(([m, label]) => `<button data-mode="${m}" class="${measureMode === m ? "active" : ""}" style="flex:1">${label}</button>`)
    .join("");
  let hint;
  if (measureMode === "area") {
    hint = '<div class="empty-hint">Click a closed path or shape to measure.</div>';
  } else {
    const remaining = 2 - measureSelection.length;
    hint = remaining > 0 ? `<div class="empty-hint">Click ${remaining} more point(s).</div>` : "";
  }
  const resultHtml = measureResult ? `<div class="field-row"><b>${escapeHtml(measureResult.text)}</b></div>` : "";
  panel.innerHTML = `<div class="tool-row" style="margin-top:6px">${modeButtons}</div>${hint}${resultHtml}`;
  for (const [m] of modes) {
    panel.querySelector(`[data-mode="${m}"]`).onclick = () => {
      measureMode = m;
      measureSelection = [];
      measureResult = null;
      renderMeasurePanel();
      render();
    };
  }
}

function renderDimensionPanel() {
  const list = document.getElementById("dimensionList");
  if (!list) return;
  if (state.dimensions.size === 0) {
    list.innerHTML = '<div class="empty-hint">Dimension tool: click two points to add a persistent, auto-updating measurement.</div>';
    return;
  }
  list.innerHTML = "";
  for (const [dimId, dim] of state.dimensions) {
    const a = livePosOf(dim.a_path, dim.a_node);
    const b = livePosOf(dim.b_path, dim.b_node);
    const label = a && b
      ? `${toDisplayUnits(Math.hypot(b[0] - a[0], b[1] - a[1])).toFixed(2)}${unitSuffix() ? " " + unitSuffix() : ""}`
      : "(geometry deleted)";
    const row = document.createElement("div");
    row.className = "path-row";
    row.innerHTML = `<span class="name">${escapeHtml(label)}</span><button class="ghost-btn" data-act="del" title="Delete" aria-label="Delete">${iconHtml("x")}</button>`;
    row.querySelector('[data-act="del"]').onclick = () => { removeDimension(dimId); renderAll(); };
    list.appendChild(row);
  }
}

/** Draws one dimension's extension lines, offset dimension line, and
 * value label -- called from inside render()'s world-space transform
 * (so it pans/zooms with everything else), with line width/font size
 * divided by view.zoom for a constant on-screen size, the same
 * convention the polygon/shape drag previews already use. Anchors are
 * resolved *live* every frame via livePosOf -- exactly why a dimension
 * updates automatically as its referenced geometry moves, and silently
 * stops drawing if either anchor point no longer exists. */
function renderDimension(dim) {
  const a = livePosOf(dim.a_path, dim.a_node);
  const b = livePosOf(dim.b_path, dim.b_node);
  if (!a || !b) return;
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const length = Math.hypot(dx, dy);
  if (length < 1e-6) return;
  const nx = -dy / length, ny = dx / length;
  const offset = dim.offset || 30;
  const la = [a[0] + nx * offset, a[1] + ny * offset];
  const lb = [b[0] + nx * offset, b[1] + ny * offset];
  ctx.save();
  ctx.strokeStyle = "#4dabf7";
  ctx.lineWidth = 1 / view.zoom;
  ctx.beginPath();
  ctx.moveTo(a[0], a[1]); ctx.lineTo(la[0], la[1]);
  ctx.moveTo(b[0], b[1]); ctx.lineTo(lb[0], lb[1]);
  ctx.moveTo(la[0], la[1]); ctx.lineTo(lb[0], lb[1]);
  ctx.stroke();
  const mx = (la[0] + lb[0]) / 2, my = (la[1] + lb[1]) / 2;
  const label = `${toDisplayUnits(length).toFixed(2)}${unitSuffix() ? " " + unitSuffix() : ""}`;
  ctx.font = `${14 / view.zoom}px sans-serif`;
  ctx.fillStyle = "#4dabf7";
  ctx.textAlign = "center";
  ctx.fillText(label, mx, my - 4 / view.zoom);
  ctx.restore();
}

// -- shape primitives (Phase 11) -----------------------------------------------
// Representation: the parametric definition lives entirely in path_props
// (e.g. {"shape": "circle", "cx":, "cy":, "r":}) -- the path's RGA point
// list stays empty. Because path_props is an LWWMap, two users
// concurrently editing (say) a circle's radius and its color merge
// field-wise for free, with zero new CRDT code -- exactly the brief's
// rationale for this representation. Rendering, hit-testing, and export
// all derive the actual shape from these fields; freehand/polygon paths
// are completely unaffected (they still use path_geom exclusively).

function isShapeTool(tool) {
  return tool === "line" || tool === "rect" || tool === "circle" || tool === "ellipse" || tool === "arc";
}

const SHAPE_FIELD_DEFS = {
  line: [["length", "Length"], ["angle", "Angle (°)"]],
  rect: [["w", "Width"], ["h", "Height"]],
  circle: [["r", "Radius"]],
  ellipse: [["rx", "Radius X"], ["ry", "Radius Y"]],
  arc: [["r", "Radius"], ["start_angle", "Start (°)"], ["end_angle", "End (°)"]],
};

/** Derives full shape props from a click-drag gesture (anchor = where
 * the drag started, current = live/final mouse position). */
function shapePropsFromDraft(kind, anchor, current) {
  const [ax, ay] = anchor, [cx, cy] = current;
  if (kind === "line") return { shape: "line", x1: ax, y1: ay, x2: cx, y2: cy };
  if (kind === "rect") {
    return {
      shape: "rect",
      x: Math.min(ax, cx), y: Math.min(ay, cy),
      w: Math.abs(cx - ax), h: Math.abs(cy - ay),
    };
  }
  if (kind === "circle") return { shape: "circle", cx: ax, cy: ay, r: Math.hypot(cx - ax, cy - ay) };
  if (kind === "ellipse") return { shape: "ellipse", cx: ax, cy: ay, rx: Math.abs(cx - ax), ry: Math.abs(cy - ay) };
  if (kind === "arc") {
    const r = Math.hypot(cx - ax, cy - ay);
    const startAngle = (Math.atan2(cy - ay, cx - ax) * 180) / Math.PI;
    // The drag alone only determines radius + start angle -- a fixed
    // 90-degree default sweep keeps the gesture simple; Start/End angle
    // are both still freely editable via the numeric panel afterward.
    return { shape: "arc", cx: ax, cy: ay, r, start_angle: startAngle, end_angle: startAngle + 90 };
  }
  return null;
}

/** A sensible starting shape at the center of the current view -- used
 * both to seed the numeric panel before any drag has happened, and as
 * the anchor for the panel's standalone "type dimensions, no drag"
 * creation path. */
function defaultShapeProps(kind) {
  const rect = canvasWrap.getBoundingClientRect();
  const [wx, wy] = screenToWorld(rect.width / 2, rect.height / 2);
  if (kind === "line") return { shape: "line", x1: wx - 50, y1: wy, x2: wx + 50, y2: wy };
  if (kind === "rect") return { shape: "rect", x: wx - 50, y: wy - 25, w: 100, h: 50 };
  if (kind === "circle") return { shape: "circle", cx: wx, cy: wy, r: 50 };
  if (kind === "ellipse") return { shape: "ellipse", cx: wx, cy: wy, rx: 60, ry: 35 };
  if (kind === "arc") return { shape: "arc", cx: wx, cy: wy, r: 50, start_angle: 0, end_angle: 90 };
  return null;
}

function shapeAnchorOf(kind, props) {
  if (kind === "line") return [props.x1, props.y1];
  if (kind === "rect") return [props.x, props.y];
  return [props.cx, props.cy];
}

/** Converts a shape's stored (raw world px) props into the numeric
 * panel's per-field *display* values -- in the current document unit
 * (Phase 11), and derived (length/angle) for Line specifically since
 * that's a more natural way to type a line than two endpoints. */
function shapeDisplayFields(kind, props) {
  if (kind === "line") {
    const len = Math.hypot(props.x2 - props.x1, props.y2 - props.y1);
    const angle = (Math.atan2(props.y2 - props.y1, props.x2 - props.x1) * 180) / Math.PI;
    return { length: toDisplayUnits(len), angle };
  }
  if (kind === "rect") return { w: toDisplayUnits(props.w), h: toDisplayUnits(props.h) };
  if (kind === "circle") return { r: toDisplayUnits(props.r) };
  if (kind === "ellipse") return { rx: toDisplayUnits(props.rx), ry: toDisplayUnits(props.ry) };
  if (kind === "arc") return { r: toDisplayUnits(props.r), start_angle: props.start_angle, end_angle: props.end_angle };
  return {};
}

/** The inverse of shapeDisplayFields -- typed panel values (+ a fixed
 * anchor point) back into full, storable (raw world px) shape props. */
function shapePropsFromFields(kind, anchor, fields) {
  const [ax, ay] = anchor;
  if (kind === "line") {
    const len = fromDisplayUnits(fields.length);
    const rad = (fields.angle * Math.PI) / 180;
    return { shape: "line", x1: ax, y1: ay, x2: ax + len * Math.cos(rad), y2: ay + len * Math.sin(rad) };
  }
  if (kind === "rect") return { shape: "rect", x: ax, y: ay, w: fromDisplayUnits(fields.w), h: fromDisplayUnits(fields.h) };
  if (kind === "circle") return { shape: "circle", cx: ax, cy: ay, r: fromDisplayUnits(fields.r) };
  if (kind === "ellipse") {
    return { shape: "ellipse", cx: ax, cy: ay, rx: fromDisplayUnits(fields.rx), ry: fromDisplayUnits(fields.ry) };
  }
  if (kind === "arc") {
    return { shape: "arc", cx: ax, cy: ay, r: fromDisplayUnits(fields.r), start_angle: fields.start_angle, end_angle: fields.end_angle };
  }
  return null;
}

function commitShape(props) {
  if (!props) return;
  if (!ui.activeLayer) { ui.activeLayer = addLayer("Layer 1"); renderLayerList(); }
  const { id } = addPath(ui.activeLayer, [], actorColor, 2.5, props);
  selectOnly(id);
  renderAll();
  renderShapeInputPanel();
  return id;
}

/** Text tool (Phase 15): a single click places a text object with
 * sensible defaults at that point -- like every other shape here, its
 * whole definition lives in path_props (no RGA points), so concurrent
 * edits to *different* fields (content vs. font_size vs. color) merge
 * field-wise for free. Editing the placed text (content, font size) is
 * done afterward via the single-selection panel, the same "create with
 * defaults, edit via panel" pattern the shape tools already use --
 * there's no inline inline text-entry UI at the click point. */
function commitText(pt) {
  if (!ui.activeLayer) { ui.activeLayer = addLayer("Layer 1"); renderLayerList(); }
  const props = { shape: "text", x: pt[0], y: pt[1], content: "Text", font_size: 16 };
  const { id } = addPath(ui.activeLayer, [], actorColor, 2.5, props);
  selectOnly(id);
  renderAll();
  return id;
}

/** While dragging, shows the live-computed dimensions read-only (the
 * drag itself is what's sizing the shape); otherwise, the fields are
 * freely editable and Enter/"Create" commits a new shape at the current
 * view's center using exactly the typed values -- Tab cycling between
 * fields is just the browser's normal focus order, nothing extra needed. */
function renderShapeInputPanel() {
  const panel = document.getElementById("shapeInputPanel");
  if (!panel) return;
  if (!isShapeTool(ui.tool)) {
    panel.innerHTML = "";
    return;
  }
  const kind = ui.tool;
  const dragging = !!shapeDraft;
  const props = dragging ? shapePropsFromDraft(kind, shapeDraft.anchor, shapeDraft.current) : defaultShapeProps(kind);
  const display = shapeDisplayFields(kind, props);
  const rows = SHAPE_FIELD_DEFS[kind]
    .map(
      ([key, label]) => `
    <div class="field-row">
      <label>${label}</label>
      <input class="shapeField" data-key="${key}" type="number" step="0.1"
        value="${(display[key] || 0).toFixed(2)}" ${dragging ? "readonly" : ""} style="width:80px"/>
    </div>`
    )
    .join("");
  panel.innerHTML = dragging
    ? `${rows}<div class="empty-hint">Release to place.</div>`
    : `${rows}<button id="shapeCommitBtn" style="width:100%;margin-top:4px">Create</button>`;
  if (dragging) return;
  const inputs = [...panel.querySelectorAll(".shapeField")];
  const commit = () => {
    const fields = {};
    for (const inp of inputs) fields[inp.dataset.key] = parseFloat(inp.value) || 0;
    const anchor = shapeAnchorOf(kind, defaultShapeProps(kind));
    commitShape(shapePropsFromFields(kind, anchor, fields));
  };
  document.getElementById("shapeCommitBtn").onclick = commit;
  for (const inp of inputs) {
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); commit(); }
    });
  }
}

/** Hit-tests a shape primitive against a *screen*-space point (same
 * constant-on-screen-size rationale as hitTestPath/hitTestPoint) --
 * returns true if within a small threshold of the shape's boundary. */
function hitTestShape(shape, screenPt) {
  const threshold = 8;
  // Phase 15: once a shape has a real fill, clicking anywhere in its
  // filled interior should select it -- boundary-only hit-testing was
  // correct for an unfilled outline (Phase 11), but would feel broken
  // for something that now visibly looks like solid content. Line/Arc
  // are never fillable (see applyFillIfSet), so they keep the original
  // boundary-only behavior unconditionally.
  const filled = shape.fill && shape.fill !== "none";
  if (shape.shape === "text") {
    const b = textBounds(shape);
    const [sx0, sy0] = worldToScreen(b.x, b.y);
    const [sx1, sy1] = worldToScreen(b.x + b.w, b.y + b.h);
    return screenPt[0] >= sx0 && screenPt[0] <= sx1 && screenPt[1] >= sy0 && screenPt[1] <= sy1;
  }
  if (shape.shape === "line") {
    const [a, b] = [worldToScreen(shape.x1, shape.y1), worldToScreen(shape.x2, shape.y2)];
    return distToSegment(screenPt, a, b) < threshold;
  }
  if (shape.shape === "rect") {
    if (filled) {
      const [wx, wy] = screenToWorld(screenPt[0], screenPt[1]);
      if (wx >= shape.x && wx <= shape.x + shape.w && wy >= shape.y && wy <= shape.y + shape.h) return true;
    }
    const corners = [
      [shape.x, shape.y], [shape.x + shape.w, shape.y],
      [shape.x + shape.w, shape.y + shape.h], [shape.x, shape.y + shape.h],
    ].map(([wx, wy]) => worldToScreen(wx, wy));
    for (let i = 0; i < 4; i++) {
      if (distToSegment(screenPt, corners[i], corners[(i + 1) % 4]) < threshold) return true;
    }
    return false;
  }
  if (shape.shape === "circle" || shape.shape === "ellipse" || shape.shape === "arc") {
    const [scx, scy] = worldToScreen(shape.cx, shape.cy);
    const rx = (shape.shape === "ellipse" ? shape.rx : shape.r) * view.zoom;
    const ry = (shape.shape === "ellipse" ? shape.ry : shape.r) * view.zoom;
    const dx = screenPt[0] - scx, dy = screenPt[1] - scy;
    // Normalize into a unit circle (handles ellipse's independent radii)
    // and compare the boundary distance in that normalized space, scaled
    // back by the smaller radius for a reasonable screen-pixel threshold.
    const normDist = Math.hypot(dx / rx, dy / ry);
    if (filled && shape.shape !== "arc" && normDist <= 1) return true;
    if (Math.abs(normDist - 1) * Math.min(rx, ry) > threshold) return false;
    if (shape.shape !== "arc") return true;
    let angle = (Math.atan2(dy, dx) * 180) / Math.PI;
    let start = shape.start_angle, end = shape.end_angle;
    const sweep = ((end - start) % 360 + 360) % 360;
    let rel = ((angle - start) % 360 + 360) % 360;
    return rel <= sweep;
  }
  return false;
}

// -- whole-path transform: move/rotate/scale (Phase 12) -----------------------
// A `transform` field ({tx, ty, rotation (degrees), scale}) on path_props
// -- per the brief, deliberately *not* rewriting the underlying RGA points
// or shape parametric fields: an LWW field write merges cleanly against a
// concurrent point-append to the same path (or a concurrent color/width
// edit), which rewriting every point would not. Absent == the identity
// transform, so every path that existed before this feature does (and
// every path with no transform applied) renders exactly as before --
// nothing is baked into stored coordinates until export.

function getTransform(props) {
  return (props && props.transform) || { tx: 0, ty: 0, rotation: 0, scale: 1 };
}

function isIdentityTransform(t) {
  return t.tx === 0 && t.ty === 0 && t.rotation === 0 && t.scale === 1;
}

/** The pivot rotation/scale happens around -- the shape's own natural
 * center for a shape primitive, or the bounding-box center of a
 * freehand/polygon path's *base* (untransformed) points. Computed fresh
 * from base geometry every time, so it never drifts as the transform
 * itself changes. */
function pathBaseCenter(pathId, props) {
  if (props.shape === "line") return [(props.x1 + props.x2) / 2, (props.y1 + props.y2) / 2];
  if (props.shape === "rect") return [props.x + props.w / 2, props.y + props.h / 2];
  if (props.shape === "circle" || props.shape === "ellipse" || props.shape === "arc") return [props.cx, props.cy];
  const pts = pathPoints(pathId);
  if (!pts.length) return [0, 0];
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  return [(Math.min(...xs) + Math.max(...xs)) / 2, (Math.min(...ys) + Math.max(...ys)) / 2];
}

/** Forward-transforms one base-space point into world space -- used by
 * hit-testing (which doesn't go through canvas's own transform stack the
 * way rendering does) and by duplicate/align/distribute. */
function applyPathTransform(pathId, props, [x, y]) {
  const t = getTransform(props);
  if (isIdentityTransform(t)) return [x, y];
  const pivot = pathBaseCenter(pathId, props);
  const dx = (x - pivot[0]) * t.scale, dy = (y - pivot[1]) * t.scale;
  const rad = (t.rotation * Math.PI) / 180;
  const cos = Math.cos(rad), sin = Math.sin(rad);
  return [pivot[0] + dx * cos - dy * sin + t.tx, pivot[1] + dx * sin + dy * cos + t.ty];
}

/** Inverse of applyPathTransform -- one *world*-space point back into
 * the path's own base space. Needed by setCircleCenter (Phase 14: a
 * tangent-constraint solve returns a circle's new *world* center, but
 * `cx`/`cy` are stored in base space -- applying the raw world result
 * directly would double-apply the transform on the next render if the
 * circle has one). */
function inversePathTransform(pathId, props, [x, y]) {
  const t = getTransform(props);
  if (isIdentityTransform(t)) return [x, y];
  const pivot = pathBaseCenter(pathId, props);
  const u = x - t.tx - pivot[0], v = y - t.ty - pivot[1];
  const rad = (t.rotation * Math.PI) / 180;
  const cos = Math.cos(rad), sin = Math.sin(rad);
  return [pivot[0] + (u * cos + v * sin) / t.scale, pivot[1] + (-u * sin + v * cos) / t.scale];
}

/** A circle shape's own live (transformed) center -- the "point" a
 * tangent constraint's `circle` anchor refers to, even though a circle
 * has no RGA point of its own to anchor to (see the `constraints`
 * component's docstring in document.py for the shape_center anchor
 * type this exists for). */
function liveShapeCenterOf(pathId) {
  const props = state.pathProps.get(pathId) || {};
  if (props.cx === undefined || props.cy === undefined) return null;
  return applyPathTransform(pathId, props, [props.cx, props.cy]);
}

/** Writes a solved world-space center back onto a circle shape's own
 * cx/cy props (inverse-transformed first -- see inversePathTransform). */
function setCircleCenter(pathId, worldPos) {
  const props = state.pathProps.get(pathId) || {};
  const [bx, by] = inversePathTransform(pathId, props, worldPos);
  setPathProp(pathId, "cx", bx);
  setPathProp(pathId, "cy", by);
}

/** Same shape-prop dict, with every coordinate field forward-transformed
 * -- lets hit-testing reuse hitTestShape's existing per-kind math
 * unchanged against the *transformed* shape, rather than needing a
 * transform-aware rewrite of every hit-test branch. Rotated Rect/
 * Ellipse/Arc hit-testing is an accepted approximation (see
 * hitTestShape): it still uses axis-aligned x/y/w/h or rx/ry math,
 * which is exact for tx/ty/scale-only transforms and only approximate
 * once rotation is non-zero -- rendering (the canvas's own nested
 * transform, see beginPathTransform) and export baking (the Python
 * `bake_path_transform`, which flattens a rotated rect/ellipse to its
 * exact rotated boundary) are both fully exact regardless of rotation;
 * only this in-app click-target hitbox is a deliberately cheaper
 * approximation, since a slightly-off click radius on a rotated shape
 * is a minor UX nit, not a data-correctness problem. */
function transformedShapeProps(pathId, props) {
  const t = getTransform(props);
  if (isIdentityTransform(t)) return props;
  const out = { ...props };
  if (props.shape === "line") {
    [out.x1, out.y1] = applyPathTransform(pathId, props, [props.x1, props.y1]);
    [out.x2, out.y2] = applyPathTransform(pathId, props, [props.x2, props.y2]);
  } else if (props.shape === "rect") {
    [out.x, out.y] = applyPathTransform(pathId, props, [props.x, props.y]);
    out.w = props.w * t.scale;
    out.h = props.h * t.scale;
  } else {
    [out.cx, out.cy] = applyPathTransform(pathId, props, [props.cx, props.cy]);
    if ("r" in props) out.r = props.r * t.scale;
    if ("rx" in props) out.rx = props.rx * t.scale;
    if ("ry" in props) out.ry = props.ry * t.scale;
    if ("start_angle" in props) out.start_angle = props.start_angle + t.rotation;
    if ("end_angle" in props) out.end_angle = props.end_angle + t.rotation;
  }
  return out;
}

/** Wraps the given path's drawing in canvas's own nested transform
 * stack (pivot-translate, rotate, scale, then translate back) if it has
 * a non-identity transform -- or is being live move-dragged right now
 * (see the "select tool" section) -- leaving a ctx.save() pushed for
 * the caller to ctx.restore() when done. Returns false (nothing
 * pushed) for the common identity case, so every path that predates
 * this feature renders exactly as before. Reuses the exact same
 * drawing code for every path regardless of transform -- much simpler
 * than manually transforming every point/curve-control/shape-field. */
function beginPathTransform(pathId, props) {
  let t = getTransform(props);
  if (selectDrag && selectDrag.mode === "move" && selectDrag.selection.has(pathId)) {
    t = { ...t, tx: t.tx + selectDrag.liveDelta[0], ty: t.ty + selectDrag.liveDelta[1] };
  }
  if (isIdentityTransform(t)) return false;
  const pivot = pathBaseCenter(pathId, props);
  ctx.save();
  ctx.translate(pivot[0] + t.tx, pivot[1] + t.ty);
  ctx.rotate((t.rotation * Math.PI) / 180);
  ctx.scale(t.scale, t.scale);
  ctx.translate(-pivot[0], -pivot[1]);
  return true;
}

// -- object snapping (Phase 12) -------------------------------------------------
// Client-side input assistance only -- no CRDT changes, per the brief.
// While dragging a selection or drawing a new shape, the cursor snaps to
// endpoints/midpoints/centers of *other* nearby (transformed) geometry,
// with a small glyph showing what it snapped to (square=endpoint,
// triangle=midpoint, circle=center).

function snapCandidatesForPath(pathId, props) {
  const out = [];
  if (props.shape === "line") {
    const p1 = applyPathTransform(pathId, props, [props.x1, props.y1]);
    const p2 = applyPathTransform(pathId, props, [props.x2, props.y2]);
    out.push({ pt: p1, kind: "endpoint" }, { pt: p2, kind: "endpoint" });
    out.push({ pt: [(p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2], kind: "midpoint" });
  } else if (props.shape === "rect") {
    const corners = [
      [props.x, props.y], [props.x + props.w, props.y],
      [props.x + props.w, props.y + props.h], [props.x, props.y + props.h],
    ].map((p) => applyPathTransform(pathId, props, p));
    for (const c of corners) out.push({ pt: c, kind: "endpoint" });
    for (let i = 0; i < 4; i++) {
      const a = corners[i], b = corners[(i + 1) % 4];
      out.push({ pt: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2], kind: "midpoint" });
    }
    out.push({ pt: applyPathTransform(pathId, props, [props.x + props.w / 2, props.y + props.h / 2]), kind: "center" });
  } else if (props.shape === "circle" || props.shape === "ellipse" || props.shape === "arc") {
    out.push({ pt: applyPathTransform(pathId, props, [props.cx, props.cy]), kind: "center" });
  } else {
    const pts = pathPoints(pathId).map((p) => applyPathTransform(pathId, props, p));
    for (const p of pts) out.push({ pt: p, kind: "endpoint" });
    for (let i = 0; i < pts.length - 1; i++) {
      out.push({ pt: [(pts[i][0] + pts[i + 1][0]) / 2, (pts[i][1] + pts[i + 1][1]) / 2], kind: "midpoint" });
    }
  }
  return out;
}

function collectSnapCandidates(excludePathIds) {
  const out = [];
  for (const pathId of state.pathIndex) {
    if (excludePathIds.has(pathId)) continue;
    const props = state.pathProps.get(pathId) || {};
    if (ui.hiddenLayers.has(props.layer_id)) continue;
    out.push(...snapCandidatesForPath(pathId, props));
  }
  return out;
}

/** Snaps `rawPt` (world coords) to the nearest candidate within a small
 * constant on-screen radius, if any. Returns `{point, glyph}` --
 * `glyph` is null (and `point` is `rawPt` unchanged) when nothing is
 * close enough. `excludePathIds` keeps geometry currently being
 * moved/drawn from snapping to itself. */
function resolveSnapPoint(rawPt, excludePathIds) {
  const thresholdWorld = 10 / view.zoom;
  let best = null, bestDist = thresholdWorld;
  for (const c of collectSnapCandidates(excludePathIds)) {
    const d = Math.hypot(rawPt[0] - c.pt[0], rawPt[1] - c.pt[1]);
    if (d < bestDist) { bestDist = d; best = c; }
  }
  return best ? { point: best.pt, glyph: best } : { point: rawPt, glyph: null };
}

function drawSnapGlyph(glyph) {
  if (!glyph) return;
  const [sx, sy] = worldToScreen(glyph.pt[0], glyph.pt[1]);
  const s = 6;
  ctx.save();
  ctx.strokeStyle = "#51cf66";
  ctx.lineWidth = 1.5;
  if (glyph.kind === "endpoint") {
    ctx.strokeRect(sx - s, sy - s, s * 2, s * 2);
  } else if (glyph.kind === "midpoint") {
    ctx.beginPath();
    ctx.moveTo(sx, sy - s);
    ctx.lineTo(sx + s, sy + s);
    ctx.lineTo(sx - s, sy + s);
    ctx.closePath();
    ctx.stroke();
  } else {
    ctx.beginPath();
    ctx.arc(sx, sy, s, 0, Math.PI * 2);
    ctx.stroke();
  }
  ctx.restore();
}

// -- selection bounds / align / distribute (Phase 12) ---------------------------

/** World-space bounding box of a path's *actual* (transformed) geometry
 * -- marquee-select and align/distribute must both act on what's on
 * screen, not a path's untransformed base data. */
function pathWorldBounds(pathId, props) {
  let pts;
  if (props.shape) {
    const t = transformedShapeProps(pathId, props);
    if (t.shape === "line") pts = [[t.x1, t.y1], [t.x2, t.y2]];
    else if (t.shape === "rect") pts = [[t.x, t.y], [t.x + t.w, t.y + t.h]];
    else if (t.shape === "ellipse") pts = [[t.cx - t.rx, t.cy - t.ry], [t.cx + t.rx, t.cy + t.ry]];
    else pts = [[t.cx - t.r, t.cy - t.r], [t.cx + t.r, t.cy + t.r]]; // circle/arc: conservative full-circle bound
  } else {
    pts = pathPoints(pathId).map((p) => applyPathTransform(pathId, props, p));
  }
  if (!pts.length) return null;
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  return { minX: Math.min(...xs), minY: Math.min(...ys), maxX: Math.max(...xs), maxY: Math.max(...ys) };
}

function rectsIntersect(a, b) {
  return a.minX <= b.maxX && a.maxX >= b.minX && a.minY <= b.maxY && a.maxY >= b.minY;
}

/** Nudges a path's transform by a world-space delta -- shared by
 * move-drag commit, and align/distribute, which both just need to
 * reposition a path without touching its base geometry. */
function nudgePathTransform(pathId, dx, dy) {
  const props = state.pathProps.get(pathId) || {};
  const t = getTransform(props);
  setPathProp(pathId, "transform", { ...t, tx: t.tx + dx, ty: t.ty + dy });
}

function alignSelection(mode) {
  const ids = [...ui.selectedPaths];
  const bounds = new Map(ids.map((id) => [id, pathWorldBounds(id, state.pathProps.get(id) || {})]));
  const valid = ids.filter((id) => bounds.get(id));
  if (valid.length < 2) return;
  const minX = Math.min(...valid.map((id) => bounds.get(id).minX));
  const maxX = Math.max(...valid.map((id) => bounds.get(id).maxX));
  const minY = Math.min(...valid.map((id) => bounds.get(id).minY));
  const maxY = Math.max(...valid.map((id) => bounds.get(id).maxY));
  for (const id of valid) {
    const b = bounds.get(id);
    if (mode === "left") nudgePathTransform(id, minX - b.minX, 0);
    else if (mode === "right") nudgePathTransform(id, maxX - b.maxX, 0);
    else if (mode === "hcenter") nudgePathTransform(id, (minX + maxX) / 2 - (b.minX + b.maxX) / 2, 0);
    else if (mode === "top") nudgePathTransform(id, 0, minY - b.minY);
    else if (mode === "bottom") nudgePathTransform(id, 0, maxY - b.maxY);
    else if (mode === "vcenter") nudgePathTransform(id, 0, (minY + maxY) / 2 - (b.minY + b.maxY) / 2);
  }
  renderAll();
}

function distributeSelection(axis) {
  const entries = [...ui.selectedPaths]
    .map((id) => ({ id, b: pathWorldBounds(id, state.pathProps.get(id) || {}) }))
    .filter((e) => e.b);
  if (entries.length < 3) return;
  const center = (b) => (axis === "h" ? (b.minX + b.maxX) / 2 : (b.minY + b.maxY) / 2);
  entries.sort((a, b) => center(a.b) - center(b.b));
  const first = center(entries[0].b), last = center(entries[entries.length - 1].b);
  const step = (last - first) / (entries.length - 1);
  for (let i = 1; i < entries.length - 1; i++) {
    const delta = first + step * i - center(entries[i].b);
    if (axis === "h") nudgePathTransform(entries[i].id, delta, 0);
    else nudgePathTransform(entries[i].id, 0, delta);
  }
  renderAll();
}

// -- duplicate / copy-paste / delete (Phase 12) ----------------------------------

const DUPLICATE_OFFSET = 20; // world px -- keeps a copy visibly distinct, never sitting exactly on top of the original

/** Deep-copies one path: fresh id, fresh RGA node ids for a freehand/
 * polygon path's points, full path_props (transform, shape fields,
 * color/width) offset by (dx, dy) via `transform` -- base geometry is
 * never rewritten, consistent with how every other move works here.
 * Curve segments (Phase 8) are keyed by their anchor's *old* node id, so
 * they're explicitly remapped onto the new points' ids afterward --
 * without this the copy would silently lose its curves, since none of
 * its fresh node ids would match any `curve:` key carried over as-is. */
function clonePath(pathId, dx, dy) {
  const props = state.pathProps.get(pathId) || {};
  const isFreehand = !props.shape;
  const oldEntries = isFreehand ? liveEntries(state.pathNodes.get(pathId)) : [];
  const pts = oldEntries.map((e) => e.v);
  const extraProps = {};
  for (const [k, v] of Object.entries(props)) {
    if (k === "layer_id" || k === "color" || k === "stroke_width" || k.startsWith("curve:")) continue;
    extraProps[k] = v;
  }
  const t = getTransform(props);
  extraProps.transform = { ...t, tx: t.tx + dx, ty: t.ty + dy };
  const { id, pointIds } = addPath(props.layer_id || ui.activeLayer, pts, props.color || actorColor, props.stroke_width || 2.5, extraProps);
  for (let i = 0; i < oldEntries.length; i++) {
    const seg = props[curvePropKey(oldEntries[i].id)];
    if (seg) setPathProp(id, curvePropKey(pointIds[i]), seg);
  }
  return id;
}

function duplicateSelection() {
  const ids = [...ui.selectedPaths];
  if (!ids.length) return;
  const newIds = ids.map((id) => clonePath(id, DUPLICATE_OFFSET, DUPLICATE_OFFSET));
  ui.selectedPaths = new Set(newIds);
  renderAll();
}

function deleteSelection() {
  const ids = [...ui.selectedPaths];
  if (!ids.length) return;
  for (const id of ids) removePath(id);
  renderAll();
}

/** Serializes the selection to JSON on the clipboard (plain data, no
 * room-specific ids referenced) -- paste works across rooms/tabs since
 * it only ever creates fresh ids, exactly like duplicate. */
async function copySelectionToClipboard() {
  const ids = [...ui.selectedPaths];
  if (!ids.length) return;
  const items = ids.map((id) => {
    const props = state.pathProps.get(id) || {};
    const entries = props.shape ? [] : liveEntries(state.pathNodes.get(id));
    // Raw [counter, actor] ids (not opIdKey's stringified form) -- these
    // round-trip through JSON as plain arrays and pass straight back
    // into curvePropKey unchanged on paste, exactly like clonePath.
    return { props, points: entries.map((e) => e.v), pointIds: entries.map((e) => e.id) };
  });
  try {
    await navigator.clipboard.writeText(JSON.stringify({ crdtCadShapes: items }));
    showToast(`Copied ${items.length} path(s)`, "success");
  } catch {
    showToast("Clipboard write failed", "error");
  }
}

async function pasteSelectionFromClipboard() {
  let text;
  try {
    text = await navigator.clipboard.readText();
  } catch {
    showToast("Clipboard read failed", "error");
    return;
  }
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    return; // clipboard doesn't hold our format -- silently ignore, not an error
  }
  if (!data || !Array.isArray(data.crdtCadShapes)) return;
  if (!ui.activeLayer) { ui.activeLayer = addLayer("Layer 1"); renderLayerList(); }
  const newIds = [];
  for (const item of data.crdtCadShapes) {
    const props = item.props || {};
    const extraProps = {};
    for (const [k, v] of Object.entries(props)) {
      if (k === "layer_id" || k === "color" || k === "stroke_width" || k.startsWith("curve:")) continue;
      extraProps[k] = v;
    }
    const t = getTransform(props);
    extraProps.transform = { ...t, tx: t.tx + DUPLICATE_OFFSET, ty: t.ty + DUPLICATE_OFFSET };
    const { id, pointIds } = addPath(ui.activeLayer, item.points || [], props.color || actorColor, props.stroke_width || 2.5, extraProps);
    const oldIds = item.pointIds || [];
    for (let i = 0; i < oldIds.length; i++) {
      const seg = props[curvePropKey(oldIds[i])];
      if (seg) setPathProp(id, curvePropKey(pointIds[i]), seg);
    }
    newIds.push(id);
  }
  if (newIds.length) {
    ui.selectedPaths = new Set(newIds);
    renderAll();
    showToast(`Pasted ${newIds.length} path(s)`, "success");
  }
}

// -- keyboard shortcut overlay (Phase 12) ----------------------------------------

function toggleShortcutOverlay() {
  if (shortcutOverlayEl) {
    shortcutOverlayEl.remove();
    shortcutOverlayEl = null;
    return;
  }
  const rows = [
    ["Space + drag / middle-drag", "Pan"],
    ["Scroll wheel", "Zoom (centered on cursor)"],
    ["Click", "Select a path"],
    ["Shift + click", "Add/remove a path from the selection"],
    ["Drag on empty canvas", "Marquee-select"],
    ["Drag a selected path", "Move the selection"],
    ["Ctrl/Cmd + D", "Duplicate selection"],
    ["Ctrl/Cmd + C / V", "Copy / paste selection"],
    ["Delete / Backspace", "Delete selection"],
    ["Esc", "Cancel the in-progress polygon"],
    ["Measure tool", "Read-only distance/angle/area readout, never synced"],
    ["Dimension tool", "Click two points for a persistent, auto-updating measurement"],
    ["?", "Toggle this overlay"],
  ];
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.style.display = "flex";
  const box = document.createElement("div");
  box.className = "modal";
  box.style.cssText = "padding:20px 24px;max-width:420px;font-size:13px;";
  box.innerHTML =
    '<h3 style="margin:0 0 10px;font-size:15px;">Keyboard shortcuts</h3>' +
    '<div style="display:grid;grid-template-columns:auto 1fr;gap:6px 14px;">' +
    rows
      .map(
        ([k, v]) =>
          `<div style="color:var(--accent);font-weight:600;white-space:nowrap;font-family:var(--font-mono);">${escapeHtml(k)}</div>` +
          `<div style="color:var(--text-secondary);">${escapeHtml(v)}</div>`
      )
      .join("") +
    "</div>";
  overlay.appendChild(box);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) toggleShortcutOverlay(); });
  document.body.appendChild(overlay);
  shortcutOverlayEl = overlay;
}

function distToSegment(p, a, b) {
  const [px, py] = p, [ax, ay] = a, [bx, by] = b;
  const dx = bx - ax, dy = by - ay;
  const lenSq = dx * dx + dy * dy;
  let t = lenSq === 0 ? 0 : ((px - ax) * dx + (py - ay) * dy) / lenSq;
  t = Math.max(0, Math.min(1, t));
  const cx = ax + t * dx, cy = ay + t * dy;
  return Math.hypot(px - cx, py - cy);
}

document.getElementById("toolPen").onclick = () => setTool("pen");
document.getElementById("toolSelect").onclick = () => setTool("select");
document.getElementById("toolPolygon").onclick = () => setTool("polygon");
document.getElementById("toolConstrain").onclick = () => setTool("constrain");
document.getElementById("toolLine").onclick = () => setTool("line");
document.getElementById("toolRect").onclick = () => setTool("rect");
document.getElementById("toolCircle").onclick = () => setTool("circle");
document.getElementById("toolEllipse").onclick = () => setTool("ellipse");
document.getElementById("toolArc").onclick = () => setTool("arc");
document.getElementById("toolText").onclick = () => setTool("text");
document.getElementById("toolMeasure").onclick = () => setTool("measure");
document.getElementById("toolDimension").onclick = () => setTool("dimension");
const TOOL_BUTTON_IDS = {
  pen: "toolPen", select: "toolSelect", polygon: "toolPolygon", constrain: "toolConstrain",
  line: "toolLine", rect: "toolRect", circle: "toolCircle", ellipse: "toolEllipse", arc: "toolArc",
  text: "toolText", measure: "toolMeasure", dimension: "toolDimension",
};
function setTool(tool) {
  if (ui.tool === "polygon" && tool !== "polygon") cancelPolygon();
  if (ui.tool === "constrain" && tool !== "constrain") { constraintSelection = []; constrainDrag = null; renderConstraintPanel(); }
  if (isShapeTool(ui.tool) && tool !== ui.tool) { shapeDraft = null; }
  if (ui.tool === "select" && tool !== "select") selectDrag = null;
  if (ui.tool === "measure" && tool !== "measure") { measureSelection = []; measureResult = null; }
  if (ui.tool === "dimension" && tool !== "dimension") { dimensionSelection = []; }
  activeSnapGlyph = null;
  ui.tool = tool;
  for (const [t, id] of Object.entries(TOOL_BUTTON_IDS)) {
    document.getElementById(id).classList.toggle("active", tool === t);
  }
  canvas.style.cursor = tool === "select" ? "default" : "crosshair";
  render();
  renderToolHint();
  renderShapeInputPanel();
  renderMeasurePanel();
}

function renderToolHint() {
  const hint = document.getElementById("toolHint");
  if (!hint) return;
  if (ui.tool === "polygon") {
    hint.textContent = pendingPolygon.length
      ? `${pendingPolygon.length} vertex(es) -- click the first vertex again to close (rejects self-intersecting shapes), or Esc to cancel.`
      : "Click to place vertices of a strict polygon. Self-intersecting or zero-length edges are rejected by the server.";
  } else if (ui.tool === "pen") {
    hint.textContent = "Drag to draw a freehand stroke.";
  } else if (ui.tool === "constrain") {
    hint.textContent = "Click two points (any paths) to relate them -- coincident, parallel, perpendicular, or a fixed distance apart.";
  } else if (ui.tool === "measure") {
    hint.textContent = "Read-only measurement -- pick a mode below, then click points (or a shape, for Area/Perim.). Nothing is sent to collaborators.";
  } else if (ui.tool === "dimension") {
    hint.textContent = "Click two points to add a persistent dimension that stays accurate as the geometry moves.";
  } else if (ui.tool === "text") {
    hint.textContent = "Click to place a text object -- edit its content and font size afterward in the Selection panel.";
  } else if (isShapeTool(ui.tool)) {
    hint.textContent = "Drag to size and place, or type exact dimensions below.";
  } else if (ui.tool === "select") {
    hint.textContent = "Click to select, shift-click to add/remove, drag empty space to marquee-select, drag a selected path to move it. Press ? for all shortcuts.";
  } else {
    hint.textContent = "Click a path to select it.";
  }
}

// -- view controls: zoom indicator, cursor readout, fit-to-content, snap ----------

function updateZoomIndicator() {
  document.getElementById("zoomIndicator").textContent = `${Math.round(view.zoom * 100)}%`;
}

function updateCursorReadout([wx, wy]) {
  const units = currentUnits();
  const dx = toDisplayUnits(wx), dy = toDisplayUnits(wy);
  const suffix = units === "px" ? "" : units;
  document.getElementById("cursorCoords").textContent = `${dx.toFixed(units === "px" ? 1 : 2)}, ${dy.toFixed(units === "px" ? 1 : 2)}${suffix ? " " + suffix : ""}`;
}

/** Fits all visible (non-hidden-layer) geometry into view with some
 * padding -- an empty document resets to the identity view (zoom 1,
 * centered on the world origin) rather than leaving a stale pan/zoom
 * from before everything was deleted. */
function fitToContent() {
  const allPts = [];
  for (const pathId of state.pathIndex) {
    if (ui.hiddenLayers.has((state.pathProps.get(pathId) || {}).layer_id)) continue;
    allPts.push(...pathPoints(pathId));
  }
  const rect = canvasWrap.getBoundingClientRect();
  if (allPts.length === 0) {
    view.zoom = 1;
    view.panX = 0;
    view.panY = 0;
    render();
    updateZoomIndicator();
    return;
  }
  const xs = allPts.map((p) => p[0]), ys = allPts.map((p) => p[1]);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  const pad = 40;
  const contentW = Math.max(maxX - minX, 1e-6), contentH = Math.max(maxY - minY, 1e-6);
  const zoomX = (rect.width - pad * 2) / contentW;
  const zoomY = (rect.height - pad * 2) / contentH;
  view.zoom = Math.max(0.05, Math.min(20, Math.min(zoomX, zoomY)));
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  view.panX = rect.width / 2 - cx * view.zoom;
  view.panY = rect.height / 2 - cy * view.zoom;
  render();
  updateZoomIndicator();
}
document.getElementById("fitToContentBtn").onclick = fitToContent;

document.getElementById("snapToggleBtn").onclick = (e) => {
  snapToGridEnabled = !snapToGridEnabled;
  e.target.classList.toggle("active", snapToGridEnabled);
};

document.getElementById("addLayerBtn").onclick = () => {
  const id = addLayer(`Layer ${state.layerOrder.length + 1}`);
  ui.activeLayer = id;
  renderAll();
};

// -- rendering ------------------------------------------------------------------

/** Draws an adaptive grid: a minor line every `pickGridStep(zoom)` world
 * units (faded out as its on-screen spacing compresses below a readable
 * threshold -- "fade minor lines out as they compress" per the brief),
 * and a major line every 5x that. Screen-space (drawn before the world
 * transform is applied), computed from the world-space viewport bounds. */
function drawGrid(rect) {
  const step = pickGridStep(view.zoom);
  const majorStep = step * 5;
  const onScreenMinorSpacing = step * view.zoom;
  const minorAlpha = Math.max(0, Math.min(1, (onScreenMinorSpacing - 8) / (40 - 8)));

  const [wx0, wy0] = screenToWorld(0, 0);
  const [wx1, wy1] = screenToWorld(rect.width, rect.height);

  // Grid lines are drawn in a theme-adaptive gray (canvasColor reads
  // --border from whichever theme is active) rather than a hardcoded
  // dark hex -- otherwise a dark grid would be nearly invisible against
  // a light-theme canvas background. Major/minor hierarchy comes from
  // alpha alone, not two different base colors.
  const gridColor = canvasColor("--border");
  ctx.save();
  if (minorAlpha > 0.02) {
    ctx.strokeStyle = gridColor;
    ctx.globalAlpha = minorAlpha * 0.7;
    ctx.lineWidth = 1;
    for (let wx = Math.floor(wx0 / step) * step; wx <= wx1; wx += step) {
      const [sx] = worldToScreen(wx, 0);
      ctx.beginPath(); ctx.moveTo(sx, 0); ctx.lineTo(sx, rect.height); ctx.stroke();
    }
    for (let wy = Math.floor(wy0 / step) * step; wy <= wy1; wy += step) {
      const [, sy] = worldToScreen(0, wy);
      ctx.beginPath(); ctx.moveTo(0, sy); ctx.lineTo(rect.width, sy); ctx.stroke();
    }
  }
  ctx.globalAlpha = 1;
  ctx.strokeStyle = gridColor;
  ctx.lineWidth = 1;
  for (let wx = Math.floor(wx0 / majorStep) * majorStep; wx <= wx1; wx += majorStep) {
    const [sx] = worldToScreen(wx, 0);
    ctx.beginPath(); ctx.moveTo(sx, 0); ctx.lineTo(sx, rect.height); ctx.stroke();
  }
  for (let wy = Math.floor(wy0 / majorStep) * majorStep; wy <= wy1; wy += majorStep) {
    const [, sy] = worldToScreen(0, wy);
    ctx.beginPath(); ctx.moveTo(0, sy); ctx.lineTo(rect.width, sy); ctx.stroke();
  }
  ctx.restore();
}

/** Traces + (usually) strokes a shape primitive (Phase 11) natively --
 * ctx.rect/ctx.arc/ctx.ellipse -- rather than faceting to a polyline,
 * called from inside render()'s world-space transform so it can use raw
 * world coordinates directly. `isDraftPreview` skips applying the
 * shape's own color/width (the caller has already set a dashed preview
 * style) and skips the selection glow -- used for the live in-progress
 * drag preview, which isn't a committed, selectable path yet. */
/** Bounding box for a text object (Phase 15) -- (x, y) is its top-left
 * corner (canvas textBaseline="top", matching svg_io's
 * dominant-baseline="hanging" so both renderers agree on what the
 * anchor point means). No real font metrics available for width, so
 * it's the same rough per-character estimate `_shape_bounds` uses
 * server-side -- good enough for hit-testing/selection outline, not
 * meant to be exact. */
function textBounds(props) {
  const fontSize = props.font_size || 16;
  const width = (props.content || "").length * fontSize * 0.6;
  return { x: props.x, y: props.y, w: width, h: fontSize * 1.2 };
}

/** `dash` (Phase 15: "solid"|"dashed"|"dotted") as a canvas line dash
 * pattern -- mirrors svg_io._dash_attr's sizing exactly (proportional
 * to stroke_width) so the two renderers agree. "dotted" relies on the
 * round linecap already set by every stroke call here to actually
 * render as dots, not a hand-drawn dot loop. */
function applyDashStyle(props) {
  const dash = props.dash || "solid";
  const sw = props.stroke_width || 2.5;
  if (dash === "dashed") ctx.setLineDash([sw * 4, sw * 3]);
  else if (dash === "dotted") ctx.setLineDash([0.01, sw * 2.5]);
  else ctx.setLineDash([]);
}

/** Fills the *current* path (Phase 15: `fill`/`fill_opacity` props) if
 * set -- shared by shapes (drawShapePath) and freehand/polygon paths
 * (render()'s main loop), called after ctx.beginPath()/the shape
 * outline but before ctx.stroke(), so the fill sits under the stroke
 * the way every vector tool already draws it. */
function applyFillIfSet(props) {
  if (!props.fill || props.fill === "none") return;
  ctx.save();
  ctx.fillStyle = props.fill;
  ctx.globalAlpha = props.fill_opacity ?? 1;
  ctx.fill();
  ctx.restore();
}

function drawShapePath(props, isSelected, isDraftPreview = false) {
  if (props.shape === "text") {
    ctx.save();
    ctx.font = `${props.font_size || 16}px sans-serif`;
    ctx.textBaseline = "top";
    ctx.fillStyle = props.color || "#e7e9ee";
    ctx.globalAlpha = isDraftPreview ? 0.6 : 1;
    ctx.fillText(props.content || "", props.x, props.y);
    ctx.restore();
    if (isSelected) {
      const b = textBounds(props);
      ctx.save();
      ctx.strokeStyle = "#4dabf7";
      ctx.lineWidth = 1 / view.zoom;
      ctx.setLineDash([4 / view.zoom, 3 / view.zoom]);
      ctx.strokeRect(b.x, b.y, b.w, b.h);
      ctx.restore();
    }
    return;
  }
  ctx.beginPath();
  if (props.shape === "line") {
    ctx.moveTo(props.x1, props.y1);
    ctx.lineTo(props.x2, props.y2);
  } else if (props.shape === "rect") {
    ctx.rect(props.x, props.y, props.w, props.h);
  } else if (props.shape === "circle") {
    ctx.arc(props.cx, props.cy, props.r, 0, Math.PI * 2);
  } else if (props.shape === "ellipse") {
    ctx.ellipse(props.cx, props.cy, props.rx, props.ry, 0, 0, Math.PI * 2);
  } else if (props.shape === "arc") {
    ctx.arc(props.cx, props.cy, props.r, (props.start_angle * Math.PI) / 180, (props.end_angle * Math.PI) / 180);
  }
  if (isDraftPreview) {
    ctx.stroke();
    return;
  }
  // Line/Arc have no meaningful enclosed area (same judgment call the
  // Measure tool's Area/Perimeter mode and both server-side exporters
  // already make) -- fill is a no-op for them regardless of the prop.
  if (props.shape !== "line" && props.shape !== "arc") applyFillIfSet(props);
  ctx.strokeStyle = props.color || "#e7e9ee";
  ctx.lineWidth = props.stroke_width || 2.5;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  applyDashStyle(props);
  ctx.stroke();
  ctx.setLineDash([]);
  if (isSelected) {
    ctx.save();
    ctx.strokeStyle = "#4dabf7";
    ctx.lineWidth = (props.stroke_width || 2.5) + 4;
    ctx.globalAlpha = 0.25;
    ctx.stroke();
    ctx.restore();
  }
}

/** Paths sorted by layer order, then creation order (Phase 15: fills
 * need correct z-order to composite right -- an unfilled outline
 * mostly doesn't reveal z-order bugs, an overlapping filled shape
 * does). `state.pathIndex` iterates in true creation order already
 * (mirrors the server-side fix in DrawingDocument.path_list); Array.sort
 * is stable, so sorting by layer index alone only reorders *across*
 * layers, never within one -- exactly "layer order, then creation
 * order," matching svg_io._z_ordered/dxf_io._z_ordered server-side. */
function zOrderedPathIds() {
  return [...state.pathIndex].sort((a, b) => {
    const layerA = state.layerOrder.indexOf((state.pathProps.get(a) || {}).layer_id);
    const layerB = state.layerOrder.indexOf((state.pathProps.get(b) || {}).layer_id);
    return layerA - layerB;
  });
}

function render() {
  const rect = canvasWrap.getBoundingClientRect();
  ctx.clearRect(0, 0, rect.width, rect.height);
  drawGrid(rect);

  // Everything from here down is drawn directly in world coordinates --
  // the canvas transform (not per-point math) handles the screen mapping,
  // so a path's geometry, and its stroke_width, correctly scale with zoom
  // the same way real-world ink would.
  ctx.save();
  ctx.translate(view.panX, view.panY);
  ctx.scale(view.zoom, view.zoom);

  for (const pathId of zOrderedPathIds()) {
    const props = state.pathProps.get(pathId) || {};
    if (ui.hiddenLayers.has(props.layer_id)) continue;
    // Phase 12: wraps this path's own drawing in canvas's nested transform
    // stack (around its pivot) when it has a move/rotate/scale applied --
    // see beginPathTransform's docstring. Everything below keeps using raw,
    // untransformed coordinates either way.
    const wrapped = beginPathTransform(pathId, props);
    if (props.shape) {
      drawShapePath(props, ui.selectedPaths.has(pathId));
      if (wrapped) ctx.restore();
      continue;
    }
    const entries = liveEntries(state.pathNodes.get(pathId));
    // Phase 14: a constrain-tool point drag is previewed live here (not
    // written to state until pointerup) by substituting its in-progress
    // world position for whichever node id is currently being dragged.
    const pts = entries.map((n) =>
      constrainDrag && constrainDrag.pathId === pathId && idEq(n.id, constrainDrag.nodeId) ? constrainDrag.livePos : n.v
    );
    if (pts.length === 0) {
      if (wrapped) ctx.restore();
      continue;
    }
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) {
      // A curve segment (Phase 8) is stored as a path_prop keyed by the
      // arriving anchor's own node id -- see curve_prop_key's docstring
      // in document.py. Absent (every point in a path created before
      // this existed, or any point reached via a plain L) means a
      // straight line, same as always.
      const seg = props[curvePropKey(entries[i].id)];
      if (seg && seg.kind === "cubic") {
        ctx.bezierCurveTo(seg.c1[0], seg.c1[1], seg.c2[0], seg.c2[1], pts[i][0], pts[i][1]);
      } else if (seg && seg.kind === "quad") {
        ctx.quadraticCurveTo(seg.c[0], seg.c[1], pts[i][0], pts[i][1]);
      } else {
        ctx.lineTo(pts[i][0], pts[i][1]);
      }
    }
    // A freehand/polygon path is only meaningfully fillable when it's
    // actually closed (first point == last point, e.g. the strict
    // Polygon tool) -- an open stroke has no well-defined interior,
    // matching both server-side exporters' identical judgment call.
    const isClosed = pts.length > 2 && Math.hypot(pts[0][0] - pts[pts.length - 1][0], pts[0][1] - pts[pts.length - 1][1]) < 1e-6;
    if (isClosed) applyFillIfSet(props);
    ctx.strokeStyle = props.color || "#e7e9ee";
    ctx.lineWidth = props.stroke_width || 2.5;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    applyDashStyle(props);
    ctx.stroke();
    ctx.setLineDash([]);
    if (ui.selectedPaths.has(pathId)) {
      ctx.save();
      ctx.strokeStyle = "#4dabf7";
      ctx.lineWidth = (props.stroke_width || 2.5) + 4;
      ctx.globalAlpha = 0.25;
      ctx.stroke();
      ctx.restore();
    }
    if (pts.length === 1) {
      ctx.beginPath();
      ctx.arc(pts[0][0], pts[0][1], (props.stroke_width || 2.5), 0, Math.PI * 2);
      ctx.fillStyle = props.color || "#e7e9ee";
      ctx.fill();
    }
    if (wrapped) ctx.restore();
  }

  if (ui.tool === "polygon" && pendingPolygon.length) {
    ctx.save();
    ctx.strokeStyle = "#ffd43b";
    ctx.lineWidth = 2 / view.zoom; // constant on-screen thickness for this transient preview
    ctx.setLineDash([6 / view.zoom, 4 / view.zoom]);
    ctx.beginPath();
    ctx.moveTo(pendingPolygon[0][0], pendingPolygon[0][1]);
    for (let i = 1; i < pendingPolygon.length; i++) ctx.lineTo(pendingPolygon[i][0], pendingPolygon[i][1]);
    if (lastMousePt) ctx.lineTo(lastMousePt[0], lastMousePt[1]);
    ctx.stroke();
    ctx.restore();
  }

  if (shapeDraft) {
    ctx.save();
    ctx.strokeStyle = "#ffd43b";
    ctx.lineWidth = 2 / view.zoom;
    ctx.setLineDash([6 / view.zoom, 4 / view.zoom]);
    drawShapePath(shapePropsFromDraft(shapeDraft.kind, shapeDraft.anchor, shapeDraft.current), false, true);
    ctx.restore();
  }

  for (const dim of state.dimensions.values()) renderDimension(dim);

  ctx.restore();

  // Screen-space overlays from here down: constant on-screen size
  // regardless of zoom, which is what actually feels right for
  // selection/vertex handles (drawn after ctx.restore(), so no
  // world transform is active).
  if (ui.tool === "polygon" && pendingPolygon.length) {
    ctx.save();
    for (const [i, pt] of pendingPolygon.entries()) {
      const [sx, sy] = worldToScreen(pt[0], pt[1]);
      ctx.beginPath();
      ctx.arc(sx, sy, i === 0 ? 6 : 4, 0, Math.PI * 2);
      ctx.fillStyle = i === 0 ? "#ffd43b" : "#ffe89b";
      ctx.fill();
    }
    ctx.restore();
  }

  if (ui.tool === "constrain" && constraintSelection.length) {
    ctx.save();
    ctx.strokeStyle = "#ffd43b";
    ctx.lineWidth = 2;
    for (const sel of constraintSelection) {
      // A shape_center pick (Phase 14, for tangent) has no nodeId to
      // resolve via livePosOf -- liveConstraintSelectionPos handles both.
      const pos = liveConstraintSelectionPos(sel);
      if (!pos) continue;
      const [sx, sy] = worldToScreen(pos[0], pos[1]);
      ctx.beginPath();
      ctx.arc(sx, sy, sel.isShapeCenter ? 10 : 7, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.restore();
  }

  for (const spec of state.constraints.values()) renderConstraintBadge(spec);

  if (ui.tool === "measure" && measureMode !== "area" && measureSelection.length) {
    ctx.save();
    ctx.strokeStyle = "#51cf66";
    ctx.lineWidth = 2;
    for (const sel of measureSelection) {
      const pos = livePosOf(sel.pathId, sel.nodeId);
      if (!pos) continue;
      const [sx, sy] = worldToScreen(pos[0], pos[1]);
      ctx.beginPath();
      ctx.arc(sx, sy, 7, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.restore();
  }

  if (ui.tool === "dimension" && dimensionSelection.length) {
    ctx.save();
    ctx.strokeStyle = "#4dabf7";
    ctx.lineWidth = 2;
    for (const sel of dimensionSelection) {
      const pos = livePosOf(sel.pathId, sel.nodeId);
      if (!pos) continue;
      const [sx, sy] = worldToScreen(pos[0], pos[1]);
      ctx.beginPath();
      ctx.arc(sx, sy, 7, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.restore();
  }

  if (selectDrag && selectDrag.mode === "marquee") {
    const [sx0, sy0] = selectDrag.startScreen;
    const [sx1, sy1] = selectDrag.currentScreen;
    ctx.save();
    ctx.strokeStyle = "#4dabf7";
    ctx.fillStyle = "rgba(77,171,247,0.12)";
    ctx.lineWidth = 1;
    const x = Math.min(sx0, sx1), y = Math.min(sy0, sy1);
    const w = Math.abs(sx1 - sx0), h = Math.abs(sy1 - sy0);
    ctx.fillRect(x, y, w, h);
    ctx.strokeRect(x, y, w, h);
    ctx.restore();
  }

  drawSnapGlyph(activeSnapGlyph);

  renderPresence();
}

function renderPresence() {
  const layer = document.getElementById("cursorLayer");
  layer.innerHTML = "";
  for (const [actor, p] of state.presence) {
    if (actor === actorId || !p) continue;
    const el = document.createElement("div");
    el.className = "cursor-label";
    // Presence positions are stored/sent in world coordinates (Phase 10)
    // -- this DOM overlay isn't inside the canvas's transform, so it
    // needs its own worldToScreen conversion to land in the right place.
    const [sx, sy] = worldToScreen(p.x, p.y);
    el.style.left = sx + "px";
    el.style.top = sy + "px";
    el.style.background = p.color || "#4dabf7";
    el.textContent = p.name || actor;
    layer.appendChild(el);
  }
}

function renderLayerList() {
  const list = document.getElementById("layerList");
  list.innerHTML = "";
  for (const lid of state.layerOrder) {
    const props = state.layers.get(lid) || {};
    const row = document.createElement("div");
    row.className = "layer-row" + (lid === ui.activeLayer ? " active" : "");
    row.innerHTML = `
      <span class="layer-swatch" style="background:${ui.hiddenLayers.has(lid) ? 'var(--text-disabled)' : 'var(--accent)'}"></span>
      <span class="name">${escapeHtml(props.name || lid)}</span>
      <button class="ghost-btn" data-act="vis" title="${ui.hiddenLayers.has(lid) ? "Show layer" : "Hide layer"}" aria-label="${ui.hiddenLayers.has(lid) ? "Show layer" : "Hide layer"}">${ui.hiddenLayers.has(lid) ? iconHtml("eye-off") : iconHtml("eye")}</button>
    `;
    row.querySelector(".name").onclick = () => { ui.activeLayer = lid; renderLayerList(); };
    row.querySelector('[data-act="vis"]').onclick = (e) => {
      e.stopPropagation();
      if (ui.hiddenLayers.has(lid)) ui.hiddenLayers.delete(lid); else ui.hiddenLayers.add(lid);
      renderLayerList(); render();
    };
    list.appendChild(row);
  }
}

function renderPathList() {
  const list = document.getElementById("pathList");
  list.innerHTML = "";
  if (state.pathIndex.size === 0) {
    list.innerHTML = '<div class="empty-hint">Draw with the pen tool to create your first path.</div>';
    return;
  }
  for (const pathId of state.pathIndex) {
    const props = state.pathProps.get(pathId) || {};
    const row = document.createElement("div");
    row.className = "path-row" + (ui.selectedPaths.has(pathId) ? " active" : "");
    const label = props.shape ? `${props.shape}` : `${pathPoints(pathId).length} pts`;
    row.innerHTML = `
      <span class="path-swatch" style="background:${props.color || "#eee"}"></span>
      <span class="name">${label} · ${escapeHtml((state.layers.get(props.layer_id) || {}).name || "?")}</span>
      <button class="ghost-btn" data-act="del" title="Delete" aria-label="Delete">${iconHtml("x")}</button>
    `;
    row.querySelector(".name").onclick = (e) => {
      if (e.shiftKey) toggleSelection(pathId);
      else selectOnly(pathId);
      renderAll();
    };
    row.querySelector('[data-act="del"]').onclick = (e) => { e.stopPropagation(); removePath(pathId); renderAll(); };
    list.appendChild(row);
  }
}

/** Bulk-action panel shown when more than one path is selected --
 * per-path fields (color/width/rotation) don't make sense for a mixed
 * group, so this offers group-level operations instead: duplicate,
 * delete, align, and (3+ paths) distribute. */
function renderBulkSelectionPanel(panel) {
  const count = ui.selectedPaths.size;
  const selectedIds = [...ui.selectedPaths];
  const groupIds = new Set(selectedIds.map((id) => (state.pathProps.get(id) || {}).group_id).filter(Boolean));
  // "Group" only makes sense for paths not already sharing one single
  // group; "Ungroup" only makes sense when the whole selection already
  // is exactly one existing group (selecting any member selects all of
  // it, so this is really just "is the selection currently a group").
  const isExactlyOneGroup = groupIds.size === 1 && [...groupIds][0] && groupMembersOf(selectedIds[0]).length === count;
  panel.innerHTML = `
    <div class="empty-hint">${count} paths selected.</div>
    <button style="width:100%;margin-top:6px" id="bulkDuplicate">Duplicate (Ctrl/Cmd+D)</button>
    ${isExactlyOneGroup
      ? '<button style="width:100%;margin-top:6px" id="bulkUngroup">Ungroup</button>'
      : '<button style="width:100%;margin-top:6px" id="bulkGroup">Group</button>'}
    <div class="field-row" style="margin-top:6px"><label>Align</label></div>
    <div class="tool-row">
      <button id="alignLeft" title="Align left" aria-label="Align left">${iconHtml("align-left")}</button>
      <button id="alignHCenter" title="Align center" aria-label="Align center">${iconHtml("align-h-center")}</button>
      <button id="alignRight" title="Align right" aria-label="Align right">${iconHtml("align-right")}</button>
    </div>
    <div class="tool-row" style="margin-top:4px">
      <button id="alignTop" title="Align top" aria-label="Align top">${iconHtml("align-top")}</button>
      <button id="alignVCenter" title="Align middle" aria-label="Align middle">${iconHtml("align-v-center")}</button>
      <button id="alignBottom" title="Align bottom" aria-label="Align bottom">${iconHtml("align-bottom")}</button>
    </div>
    <div class="field-row" style="margin-top:6px"><label>Distribute (3+)</label></div>
    <div class="tool-row">
      <button id="distH" ${count < 3 ? "disabled" : ""}>Horiz.</button>
      <button id="distV" ${count < 3 ? "disabled" : ""}>Vert.</button>
    </div>
    <button class="danger" id="bulkDelete" style="width:100%;margin-top:8px">Delete ${count} paths</button>
  `;
  document.getElementById("bulkDuplicate").onclick = duplicateSelection;
  document.getElementById("bulkDelete").onclick = deleteSelection;
  if (isExactlyOneGroup) {
    document.getElementById("bulkUngroup").onclick = () => { ungroupPath(selectedIds[0]); renderAll(); };
  } else {
    document.getElementById("bulkGroup").onclick = () => { groupPaths(selectedIds); renderAll(); };
  }
  document.getElementById("alignLeft").onclick = () => alignSelection("left");
  document.getElementById("alignHCenter").onclick = () => alignSelection("hcenter");
  document.getElementById("alignRight").onclick = () => alignSelection("right");
  document.getElementById("alignTop").onclick = () => alignSelection("top");
  document.getElementById("alignVCenter").onclick = () => alignSelection("vcenter");
  document.getElementById("alignBottom").onclick = () => alignSelection("bottom");
  document.getElementById("distH").onclick = () => distributeSelection("h");
  document.getElementById("distV").onclick = () => distributeSelection("v");
}

function renderSelectionPanel() {
  const panel = document.getElementById("selectionPanel");
  const commentPanel = document.getElementById("commentList");
  if (ui.selectedPaths.size === 0) {
    panel.innerHTML = '<div class="empty-hint">Select a path to edit its color, stroke width, or leave a comment.</div>';
    commentPanel.innerHTML = '<div class="empty-hint">No path selected.</div>';
    return;
  }
  if (ui.selectedPaths.size > 1) {
    renderBulkSelectionPanel(panel);
    commentPanel.innerHTML = '<div class="empty-hint">Comments need exactly one selected path.</div>';
    return;
  }
  const pathId = primarySelectedPath();
  if (!pathId || !state.pathProps.has(pathId)) {
    panel.innerHTML = '<div class="empty-hint">Select a path to edit its color, stroke width, or leave a comment.</div>';
    commentPanel.innerHTML = '<div class="empty-hint">No path selected.</div>';
    return;
  }
  const props = state.pathProps.get(pathId) || {};
  const t = getTransform(props);
  const isText = props.shape === "text";
  const typeSpecificFields = isText
    ? `
    <div class="field-row"><label>Content</label><input id="selContent" type="text" value="${escapeHtml(props.content || "")}" style="width:120px"/></div>
    <div class="field-row"><label>Font size</label><input id="selFontSize" type="number" min="4" step="1" value="${props.font_size || 16}" style="width:70px"/></div>`
    : `
    <div class="field-row"><label>Width</label><input id="selWidth" type="number" min="1" max="20" step="0.5" value="${props.stroke_width || 2.5}" style="width:70px"/></div>
    <div class="field-row"><label>Dash</label>
      <select id="selDash" style="width:90px">
        <option value="solid" ${!props.dash || props.dash === "solid" ? "selected" : ""}>Solid</option>
        <option value="dashed" ${props.dash === "dashed" ? "selected" : ""}>Dashed</option>
        <option value="dotted" ${props.dash === "dotted" ? "selected" : ""}>Dotted</option>
      </select>
    </div>
    <div class="field-row"><label>Fill</label><input id="selFill" type="text" placeholder="none" value="${escapeHtml(props.fill || "")}" style="width:90px"/></div>
    <div class="field-row"><label>Fill opacity</label><input id="selFillOpacity" type="number" min="0" max="1" step="0.05" value="${props.fill_opacity ?? 1}" style="width:70px"/></div>`;
  const ungroupButton = props.group_id
    ? '<button style="width:100%;margin-top:4px" id="selUngroup">Ungroup</button>'
    : "";
  panel.innerHTML = `
    <div class="field-row"><label>Color</label><input id="selColor" type="text" value="${escapeHtml(props.color || "#ffffff")}" style="width:90px"/></div>
    ${typeSpecificFields}
    <div class="field-row"><label>Rotation (°)</label><input id="selRotation" type="number" step="1" value="${t.rotation}" style="width:70px"/></div>
    <div class="field-row"><label>Scale</label><input id="selScale" type="number" min="0.01" step="0.1" value="${t.scale}" style="width:70px"/></div>
    <button style="width:100%;margin-top:4px" id="selDuplicate">Duplicate (Ctrl/Cmd+D)</button>
    ${ungroupButton}
    <button class="danger" id="selDelete" style="width:100%;margin-top:6px">Delete path</button>
  `;
  document.getElementById("selColor").onchange = (e) => setPathProp(pathId, "color", e.target.value);
  if (isText) {
    document.getElementById("selContent").onchange = (e) => setPathProp(pathId, "content", e.target.value);
    document.getElementById("selFontSize").onchange = (e) => setPathProp(pathId, "font_size", parseFloat(e.target.value) || 16);
  } else {
    document.getElementById("selWidth").onchange = (e) => setPathProp(pathId, "stroke_width", parseFloat(e.target.value));
    document.getElementById("selDash").onchange = (e) => setPathProp(pathId, "dash", e.target.value);
    document.getElementById("selFill").onchange = (e) => setPathProp(pathId, "fill", e.target.value.trim() || null);
    document.getElementById("selFillOpacity").onchange = (e) => setPathProp(pathId, "fill_opacity", parseFloat(e.target.value));
  }
  document.getElementById("selRotation").onchange = (e) =>
    setPathProp(pathId, "transform", { ...getTransform(state.pathProps.get(pathId) || {}), rotation: parseFloat(e.target.value) || 0 });
  document.getElementById("selScale").onchange = (e) =>
    setPathProp(pathId, "transform", { ...getTransform(state.pathProps.get(pathId) || {}), scale: parseFloat(e.target.value) || 1 });
  document.getElementById("selDuplicate").onclick = duplicateSelection;
  if (props.group_id) document.getElementById("selUngroup").onclick = () => { ungroupPath(pathId); renderAll(); };
  document.getElementById("selDelete").onclick = () => { removePath(pathId); renderAll(); };

  commentPanel.innerHTML = "";
  const commentsForPath = [...state.comments.entries()].filter(([, c]) => c && c.path_id === pathId);
  if (commentsForPath.length === 0) {
    commentPanel.innerHTML = '<div class="empty-hint">No comments yet.</div>';
  } else {
    for (const [cid, c] of commentsForPath) {
      const row = document.createElement("div");
      row.className = "comment-row";
      row.innerHTML = `<div style="flex:1"><b>${escapeHtml(c.author)}</b>: ${escapeHtml(c.text)}</div><button class="ghost-btn" data-act="del" title="Delete" aria-label="Delete">${iconHtml("x")}</button>`;
      row.querySelector('[data-act="del"]').onclick = () => { removeComment(cid); renderAll(); };
      commentPanel.appendChild(row);
    }
  }
  const addRow = document.createElement("div");
  addRow.style.marginTop = "8px";
  addRow.innerHTML = `<textarea id="commentText" rows="2" placeholder="Add a comment…"></textarea><button id="commentAdd" style="width:100%;margin-top:4px">Comment</button>`;
  commentPanel.appendChild(addRow);
  document.getElementById("commentAdd").onclick = () => {
    const text = document.getElementById("commentText").value.trim();
    if (text) { addComment(pathId, text); renderAll(); }
  };
}

function renderPresenceList() {
  const list = document.getElementById("presenceList");
  list.innerHTML = "";
  const others = [...state.presence.entries()].filter(([a]) => a !== actorId);
  document.getElementById("presenceCount").textContent = others.length + 1;
  const me = document.createElement("div");
  me.className = "presence-row";
  me.innerHTML = `<span class="path-swatch" style="background:${actorColor}"></span><span class="name">${escapeHtml(actorName)} (you)</span>`;
  list.appendChild(me);
  for (const [, p] of others) {
    if (!p) continue;
    const row = document.createElement("div");
    row.className = "presence-row";
    row.innerHTML = `<span class="path-swatch" style="background:${p.color || "#4dabf7"}"></span><span class="name">${escapeHtml(p.name || "?")}</span>`;
    list.appendChild(row);
  }
  renderAvatarStack(actorName, actorColor, others);
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderAll() {
  render();
  renderLayerList();
  renderPathList();
  renderSelectionPanel();
  renderConstraintPanel();
  renderConstraintsListPanel();
  renderShapeInputPanel();
  renderMeasurePanel();
  renderDimensionPanel();
  renderPresenceList();
}

setInterval(() => {
  document.getElementById("opsCounter").textContent = `${ui.opsCount} ops relayed`;
  document.getElementById("offlineCounter").textContent = conn.outbox.length ? `${conn.outbox.length} queued offline` : "";
}, 400);

resizeCanvas();
setTool("pen");
